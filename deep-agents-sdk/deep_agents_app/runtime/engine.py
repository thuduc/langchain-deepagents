import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from starlette.concurrency import run_in_threadpool

from deep_agents_app.domain import RunContext
from deep_agents_app.services import workspace as service_state
from deep_agents_app.services.artifacts import (
    prepare_response_artifacts,
    register_run_artifacts,
    unique_artifact_path,
)
from deep_agents_app.services.sessions import (
    add_chat_message,
    cleanup_run_work,
    finish_task_run,
    session_thread_id,
    update_task_run_activity,
)
from deep_agents_app.services.workspace import (
    bounded_env_int,
    ensure_child_path,
    get_app_settings,
    get_project,
    get_project_root,
    logger,
    project_artifact_context,
    project_filesystem_permission_specs,
    project_skills_source,
    scan_project_skills,
)


def message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "\n\n".join(p for p in parts if p.strip())
    return str(content)


def python_binary() -> str:
    """Return the interpreter used to run generated Python.

    The server's own interpreter is the correct one: requirements.txt installs
    pandas, matplotlib, and openpyxl into whichever environment runs the
    application. Deriving the path from the source tree instead assumes the
    virtual environment sits beside the code, which is false for any deployment
    that builds it elsewhere — the container image creates it at /opt/venv, so
    the previous lookup could never succeed there and every generated-code tool
    call failed.
    """
    if not sys.executable:
        raise RuntimeError("No Python interpreter is available to run generated code")
    return sys.executable


ProjectTreeSnapshot = Dict[str, tuple[str, int, int]]
_project_execution_locks_guard = threading.Lock()
_project_execution_locks: Dict[str, threading.RLock] = {}


def project_execution_lock(project_root: Path) -> threading.RLock:
    key = str(project_root.resolve())
    with _project_execution_locks_guard:
        return _project_execution_locks.setdefault(key, threading.RLock())


def project_tree_snapshot(project_root: Path) -> ProjectTreeSnapshot:
    snapshot: ProjectTreeSnapshot = {}
    for directory, directory_names, file_names in os.walk(
        project_root, topdown=True, followlinks=False
    ):
        directory_path = Path(directory)
        for name in list(directory_names):
            path = directory_path / name
            relative = path.relative_to(project_root).as_posix()
            metadata = path.lstat()
            kind = "symlink" if path.is_symlink() else "directory"
            snapshot[relative] = (kind, metadata.st_size, metadata.st_mtime_ns)
            if path.is_symlink():
                directory_names.remove(name)
        for name in file_names:
            path = directory_path / name
            relative = path.relative_to(project_root).as_posix()
            metadata = path.lstat()
            kind = "symlink" if path.is_symlink() else "file"
            snapshot[relative] = (kind, metadata.st_size, metadata.st_mtime_ns)
    return snapshot


def recover_new_project_files(
    project_root: Path,
    before: ProjectTreeSnapshot,
    after: ProjectTreeSnapshot,
    artifact_directory: Path,
) -> List[str]:
    """Quarantine files unexpectedly created in a shared project by generated code."""
    recovered: List[str] = []
    new_entries = sorted(set(after) - set(before))
    for relative_name in new_entries:
        kind = after[relative_name][0]
        source = project_root / relative_name
        if kind != "file" or not source.is_file() or source.is_symlink():
            continue
        relative_path = Path("recovered-project-writes") / relative_name
        target = unique_artifact_path(artifact_directory, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        recovered.append(relative_name)

    for relative_name in sorted(new_entries, key=lambda value: value.count("/"), reverse=True):
        source = project_root / relative_name
        try:
            if source.is_symlink():
                source.unlink()
            elif source.is_dir():
                source.rmdir()
        except OSError:
            pass
    return recovered


def shared_project_mutations(
    before: ProjectTreeSnapshot, after: ProjectTreeSnapshot
) -> List[str]:
    mutations: List[str] = []
    for relative_name in sorted(set(before) & set(after)):
        old_kind, old_size, old_mtime = before[relative_name]
        new_kind, new_size, new_mtime = after[relative_name]
        if old_kind == "directory" and new_kind == "directory":
            continue
        if (old_kind, old_size, old_mtime) != (new_kind, new_size, new_mtime):
            mutations.append(f"modified {relative_name}")
    for relative_name in sorted(set(before) - set(after)):
        if before[relative_name][0] != "directory":
            mutations.append(f"deleted {relative_name}")
    return mutations


def prepare_python_workspace(context: RunContext, project_root: Path) -> None:
    context.work_directory.mkdir(parents=True, exist_ok=True)
    context.artifact_directory.mkdir(parents=True, exist_ok=True)
    data_directory = project_root / "data"
    data_link = context.work_directory / "data"
    if data_directory.is_dir() and not data_link.exists():
        data_link.symlink_to(data_directory, target_is_directory=True)


def execute_python_code(code: str, project_root: Path) -> str:
    context = service_state.CURRENT_RUN_CONTEXT.get()
    if context is None:
        return "Error executing code: no active user run context."
    project_root = ensure_child_path(service_state.PROJECTS_ROOT, project_root)
    expected_project_root = get_project_root(context.project_id).resolve()
    if project_root != expected_project_root:
        return "Error executing code: active run context does not match the selected project."
    prepare_python_workspace(context, project_root)
    temp_path = context.work_directory / "generated.py"
    timeout_seconds = bounded_env_int("PYTHON_EXECUTION_TIMEOUT_SECONDS", 30, 1, 3600)
    with project_execution_lock(project_root):
        before = project_tree_snapshot(project_root)
        try:
            temp_path.write_text(code, encoding="utf-8")
            execution_env = {
                "PATH": os.environ.get("PATH", ""),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "MPLBACKEND": "Agg",
                "MPLCONFIGDIR": str(context.work_directory / ".matplotlib"),
                "PYTHONPYCACHEPREFIX": str(context.work_directory / ".python-cache"),
                "TMPDIR": str(context.work_directory),
                "DEEP_AGENTS_PROJECT_DIR": str(project_root),
                "DEEP_AGENTS_RUN_ARTIFACT_DIR": str(context.artifact_directory),
            }
            result = subprocess.run(
                [python_binary(), str(temp_path)],
                cwd=str(context.work_directory),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=execution_env,
            )
            output = ""
            if result.stdout:
                output += f"Output:\n{result.stdout[:1_000_000]}\n"
            if result.stderr:
                output += f"Errors:\n{result.stderr[:1_000_000]}\n"
            if result.returncode and not result.stderr:
                output += f"Errors:\nPython exited with status {result.returncode}.\n"
        except subprocess.TimeoutExpired:
            output = f"Error: Code execution timed out after {timeout_seconds} seconds."
        except Exception as exc:
            output = f"Error executing code: {str(exc)}"

        after = project_tree_snapshot(project_root)
        recovered = recover_new_project_files(
            project_root, before, after, context.artifact_directory
        )
        mutations = shared_project_mutations(before, after)
        if recovered:
            logger.warning(
                "Run %s wrote into shared project %s; recovered files: %s",
                context.run_id,
                context.project_id,
                ", ".join(recovered),
            )
        if mutations:
            mutation_summary = ", ".join(mutations[:20])
            logger.error(
                "Run %s mutated shared project %s: %s",
                context.run_id,
                context.project_id,
                mutation_summary,
            )
            output += (
                "\nError: generated Python changed shared project source content: "
                f"{mutation_summary}. Generated Python execution is not sandboxed."
            )
        return output or "Execution completed successfully with no output."


def load_skill_prompt(project_id: str, skill_name: str) -> str:
    project = get_project(project_id)
    project_root = Path(project["path"])
    skill_path = project_root / "skills" / skill_name / "SKILL.md"
    references_dir = project_root / "skills" / skill_name / "references"

    prompt = (
        f"You are an advanced specialist worker executing tasks for the skill '{skill_name}'.\n"
        f"The selected project is '{project['name']}'. Its shared source root is: {project_root}.\n"
        "Use Deep Agents filesystem tools with absolute virtual paths such as '/data/file.csv' when reading or writing project files.\n"
    )

    if skill_path.exists():
        prompt += f"\n--- Skill Guidelines ---\n{skill_path.read_text(encoding='utf-8')}\n"

    if references_dir.exists():
        for ref_path in sorted(references_dir.iterdir()):
            if ref_path.is_file() and ref_path.suffix.lower() in {".json", ".md", ".txt", ".csv"}:
                try:
                    prompt += (
                        f"\n--- Reference: {ref_path.relative_to(project_root).as_posix()} ---\n"
                        f"{ref_path.read_text(encoding='utf-8')}\n"
                    )
                except UnicodeDecodeError:
                    continue

    prompt += (
        "\nCore Objective: Analyze the task instructions, formulate python code to explore data or plot charts, "
        "run the code using the execute_python tool, and present the final answer to the user. "
        "The execute_python tool runs in a private per-user, per-session workspace where 'data/<filename>' "
        "is a read-through link to the selected project's shared data. Never write generated content into the "
        "shared project root. Before creating any generated file, call get_project_context and save the file "
        "inside the returned artifact_directory. Relative files created by execute_python are also collected "
        "into that run's artifact directory. Create each requested deliverable once; if you revise a chart or export, "
        "overwrite the same artifact rather than creating alternate versions. Do not include generated filenames, "
        "artifact_directory values, absolute paths, empty artifact placeholder sections, or separate file-path listings "
        "in the final answer. Describe the visualization's analytical meaning in prose instead. The server will register generated files and "
        "present them in a private authenticated Generated artifacts section automatically."
    )
    return prompt


def get_supervisor_system_prompt(project_id: str) -> str:
    project = get_project(project_id)
    return (
        "You are a supervisor coordinator. Your goal is to analyze the user prompt and coordinate the plan.\n"
        "Use the Deep Agents skills library and task tool for the currently selected project to decide which specialist "
        f"capability applies. The selected project is '{project['name']}'.\n\n"
        "Core Objective: Analyze the user's request. Create a plan, delegate substantive analysis to the best "
        "project-specific subagent using the task tool, synthesize the subagent result, and present the final answer. "
        "Delegate each requested deliverable once and tell the specialist to return analytical findings, not only a file path. "
        "Never repeat generated filenames, artifact directories, absolute paths, or path-only visualization placeholders "
        "from a specialist response. Describe the visualization's meaning in prose; the server attaches every retained "
        "download in the authenticated Generated artifacts section."
    )


def get_llm_instance():
    from langchain_openai import ChatOpenAI
    from portkey_ai import PORTKEY_GATEWAY_URL, createHeaders

    portkey_api_key = os.environ.get("PORTKEY_API_KEY")
    provider_slug = os.environ.get("PORTKEY_PROVIDER_SLUG", "google-ai-studio")
    model_name = get_app_settings()["default_model"]

    temp_env = os.environ.get("TEMPERATURE")
    if temp_env is not None:
        try:
            temperature = float(temp_env)
        except ValueError:
            temperature = 0.0
    else:
        temperature = 1.0 if "gpt-5" in model_name else 0.0

    headers = createHeaders(api_key=portkey_api_key, provider=provider_slug)
    return ChatOpenAI(
        model=f"@{provider_slug}/{model_name}" if not model_name.startswith("@") else model_name,
        temperature=temperature,
        base_url=PORTKEY_GATEWAY_URL,
        default_headers=headers,
        api_key=portkey_api_key,
    )


def build_skill_subagents(
    project_id: str,
    skills_source: List[Any],
    tools: List[Any],
) -> List[Dict[str, Any]]:
    subagents: List[Dict[str, Any]] = []
    for skill in scan_project_skills(project_id):
        skill_name = skill["name"]
        subagents.append(
            {
                "name": skill_name,
                "description": skill["description"],
                "system_prompt": load_skill_prompt(project_id, skill_name),
                "tools": tools,
                "skills": skills_source,
            }
        )
    return subagents


def agent_run_config(user_id: str, project_id: str, session_id: str) -> Dict[str, Any]:
    return {
        "configurable": {
            "thread_id": session_thread_id(user_id, project_id, session_id)
        },
        "recursion_limit": 100,
    }


def response_text_from_agent_result(result: Dict[str, Any]) -> str:
    messages = result.get("messages", [])
    if messages:
        last_msg = messages[-1]
        if hasattr(last_msg, "content"):
            response_text = message_content_to_text(last_msg.content)
        elif isinstance(last_msg, dict):
            response_text = message_content_to_text(last_msg.get("content", ""))
        else:
            response_text = str(last_msg)
        if response_text.strip():
            return response_text

    return "The model returned an empty response for this request. Please try again or rephrase your prompt."


@dataclass
class AgentRuntime:
    graph: Any
    checkpointer: Any
    connection: Any


_agent_graphs: Dict[str, AgentRuntime] = {}
_agent_graph_lock = threading.RLock()


def close_checkpoint_connection(connection: Any) -> None:
    """Close an aiosqlite connection from either a sync or async caller.

    Its worker thread is not a daemon, so an abandoned connection would keep the
    process from exiting. aiosqlite resolves each operation on the loop that
    issued it, so closing from a fresh loop is safe even though the connection
    was opened on a different one.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    try:
        if running is not None:
            running.create_task(connection.close())
        else:
            asyncio.run(connection.close())
    except Exception as exc:
        logger.warning("Could not close checkpoint connection: %s", exc)


def invalidate_project_agent(project_id: str) -> None:
    with _agent_graph_lock:
        runtime = _agent_graphs.pop(project_id, None)
    if runtime:
        close_checkpoint_connection(runtime.connection)


def invalidate_all_agents() -> None:
    for project_id in list(_agent_graphs):
        invalidate_project_agent(project_id)


async def aclose_all_agents() -> None:
    """Await every checkpoint connection closed, for orderly application shutdown."""
    for project_id in list(_agent_graphs):
        with _agent_graph_lock:
            runtime = _agent_graphs.pop(project_id, None)
        if not runtime:
            continue
        try:
            await runtime.connection.close()
        except Exception as exc:
            logger.warning("Could not close checkpoint connection for %s: %s", project_id, exc)


async def create_checkpointer():
    """Open the async checkpoint store.

    ``astream`` and ``ainvoke`` require an async checkpointer; the synchronous
    ``SqliteSaver`` raises rather than falling back to a thread.
    """
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    service_state.AGENT_CHECKPOINT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(service_state.AGENT_CHECKPOINT_DB_PATH)
    await connection.execute("PRAGMA journal_mode = WAL;")
    await connection.execute("PRAGMA busy_timeout = 5000;")
    checkpointer = AsyncSqliteSaver(connection)
    await checkpointer.setup()
    return checkpointer, connection


def build_project_graph(project_id: str, checkpointer: Any):
    """Assemble a project's agent graph. Synchronous and slow; call off the loop."""
    from langchain_core.tools import tool
    from deepagents import FilesystemPermission, create_deep_agent
    from deepagents.backends import FilesystemBackend

    project = get_project(project_id)
    project_root = Path(project["path"])
    backend = FilesystemBackend(root_dir=project_root, virtual_mode=True)
    skills = project_skills_source(project)
    permissions = [
        FilesystemPermission(**permission_spec)
        for permission_spec in project_filesystem_permission_specs()
    ]
    llm = get_llm_instance()

    @tool
    def execute_python(code: str) -> str:
        """Execute Python in this run's private workspace with project data linked at data/."""
        return execute_python_code(code, project_root)

    @tool
    def get_project_context() -> Dict[str, str]:
        """Return the selected project root and private output locations for this run."""
        context = service_state.CURRENT_RUN_CONTEXT.get()
        if context is None or context.project_id != project_id:
            raise RuntimeError("No active run context for the selected project")
        return {
            "project_name": project["name"],
            "project_root": str(project_root),
            **project_artifact_context(context),
        }

    specialist_tools = [execute_python, get_project_context]
    subagents = build_skill_subagents(project_id, skills, specialist_tools)

    return create_deep_agent(
        model=llm,
        tools=[],
        skills=skills,
        backend=backend,
        permissions=permissions,
        subagents=subagents,
        system_prompt=get_supervisor_system_prompt(project_id),
        checkpointer=checkpointer,
    )


async def get_agent_graph(project_id: str):
    with _agent_graph_lock:
        cached = _agent_graphs.get(project_id)
        if cached:
            return cached.graph

    checkpointer, connection = await create_checkpointer()
    try:
        graph = await run_in_threadpool(build_project_graph, project_id, checkpointer)
    except Exception:
        await connection.close()
        raise

    runtime = AgentRuntime(graph=graph, checkpointer=checkpointer, connection=connection)
    with _agent_graph_lock:
        existing = _agent_graphs.get(project_id)
        if existing is None:
            _agent_graphs[project_id] = runtime
    if existing is not None:
        # Another request built this project's graph first; discard the loser.
        await connection.close()
        return existing.graph
    return graph


async def run_agent(context: RunContext, prompt: str) -> str:
    """Run one prompt to completion without occupying a worker thread while waiting.

    The run context is set on the current asyncio context. LangGraph copies that
    context into the executor it uses for synchronous tools, so ``execute_python``
    and ``get_project_context`` still observe the active run.
    """
    context_token = service_state.CURRENT_RUN_CONTEXT.set(context)
    try:
        if not os.environ.get("PORTKEY_API_KEY"):
            return await run_in_threadpool(simulate_agent_response, context, prompt)

        agent = await get_agent_graph(context.project_id)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=agent_run_config(
                context.user_id, context.project_id, context.session_id
            ),
        )
        return response_text_from_agent_result(result)
    finally:
        service_state.CURRENT_RUN_CONTEXT.reset(context_token)


def sse_event(event_type: str, data: Dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def chunk_parts(chunk: Any) -> tuple[tuple[str, ...], str, Any]:
    if isinstance(chunk, tuple) and len(chunk) == 3:
        namespace, mode, data = chunk
        return tuple(namespace or ()), str(mode), data
    if isinstance(chunk, tuple) and len(chunk) == 2:
        mode, data = chunk
        return (), str(mode), data
    if isinstance(chunk, dict) and "type" in chunk:
        return tuple(chunk.get("ns") or ()), str(chunk["type"]), chunk.get("data")
    return (), "unknown", chunk


def task_status_from_event(namespace: tuple[str, ...], data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None

    name = str(data.get("name", ""))
    if not name:
        return None

    is_subagent = bool(namespace)
    if "input" in data and name == "model":
        return "A project specialist is analyzing the request…" if is_subagent else "Analyzing the request…"
    if "input" in data and name == "tools":
        return "A project specialist is working with the data…" if is_subagent else "Working with project data…"
    if "input" in data and name == "task":
        return "Delegating to a project specialist…"
    if "result" in data and name == "task":
        return "Reviewing the specialist’s results…"
    return None


def delta_from_message_event(namespace: tuple[str, ...], data: Any) -> str:
    if namespace or not isinstance(data, tuple) or len(data) != 2:
        return ""

    message, metadata = data
    if isinstance(metadata, dict) and metadata.get("langgraph_node") != "model":
        return ""

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


def final_text_from_update(namespace: tuple[str, ...], data: Any) -> str:
    if namespace or not isinstance(data, dict):
        return ""

    for update in data.values():
        if not isinstance(update, dict):
            continue
        messages = update.get("messages")
        if not messages:
            continue
        text = message_content_to_text(messages[-1].content if hasattr(messages[-1], "content") else messages[-1])
        if text.strip():
            return text
    return ""


async def run_activity_event(context: RunContext, message: str) -> str:
    await run_in_threadpool(update_task_run_activity, context.run_id, message)
    return sse_event(
        "status",
        {
            "message": message,
            "project_id": context.project_id,
            "session_id": context.session_id,
            "run_id": context.run_id,
        },
    )


async def stream_agent_events(context: RunContext, prompt: str) -> AsyncIterator[str]:
    started_at = time.monotonic()
    final_text = ""
    delta_parts: List[str] = []
    emitted_delta = False
    initial_status = "Preparing the agent workspace…"
    # An async generator runs in its caller's context, and Starlette drives this
    # one from a single request task, so the run context set here stays visible
    # for the whole run, including inside synchronous agent tools.
    context_token = service_state.CURRENT_RUN_CONTEXT.set(context)
    try:
        yield sse_event(
            "run",
            {
                "project_id": context.project_id,
                "session_id": context.session_id,
                "run_id": context.run_id,
                "status": initial_status,
            },
        )
        if not os.environ.get("PORTKEY_API_KEY"):
            yield await run_activity_event(context, "Preparing a local response…")
            final_text = await run_in_threadpool(simulate_agent_response, context, prompt)
        else:
            agent = await get_agent_graph(context.project_id)
            yield await run_activity_event(context, initial_status)
            async for chunk in agent.astream(
                {"messages": [{"role": "user", "content": prompt}]},
                config=agent_run_config(
                    context.user_id, context.project_id, context.session_id
                ),
                stream_mode=["updates", "messages", "tasks"],
                subgraphs=True,
            ):
                namespace, mode, data = chunk_parts(chunk)

                if mode == "tasks":
                    status = task_status_from_event(namespace, data)
                    if status:
                        yield await run_activity_event(context, status)
                    continue

                if mode == "messages":
                    delta = delta_from_message_event(namespace, data)
                    if delta:
                        emitted_delta = True
                        delta_parts.append(delta)
                        # Model tokens can contain internal artifact paths before
                        # the files have been registered. Keep the user-facing
                        # activity stream live, but wait for the sanitized final
                        # response before exposing model text.
                    continue

                if mode == "updates":
                    update_text = final_text_from_update(namespace, data)
                    if update_text:
                        final_text = update_text

        if not final_text and delta_parts:
            final_text = "".join(delta_parts)
        if not final_text:
            final_text = "The model returned an empty response for this request. Please try again or rephrase your prompt."

        yield await run_activity_event(context, "Preparing generated files…")
        artifacts = await run_in_threadpool(register_run_artifacts, context)
        yield await run_activity_event(context, "Finalizing the response…")
        response_text = await run_in_threadpool(
            prepare_response_artifacts,
            context.user_id,
            context.project_id,
            final_text,
            artifacts,
        )
        await run_in_threadpool(
            add_chat_message,
            context.user_id,
            context.project_id,
            context.session_id,
            "assistant",
            response_text,
            context.run_id,
        )
        finish_task_run(context.run_id, "completed")
        if not emitted_delta:
            yield sse_event("delta", {"text": response_text})
        yield sse_event(
            "final",
            {
                "response": response_text,
                "project_id": context.project_id,
                "session_id": context.session_id,
                "run_id": context.run_id,
                "duration_seconds": round(time.monotonic() - started_at, 3),
            },
        )
    except (asyncio.CancelledError, GeneratorExit):
        # Terminal bookkeeping stays synchronous on this path: awaiting while the
        # task is being cancelled re-raises immediately and would skip the update.
        finish_task_run(context.run_id, "cancelled")
        raise
    except Exception as exc:
        logger.exception("Agent run %s failed", context.run_id)
        error_text = "The agent run failed. Check the server logs using the run ID and try again."
        finish_task_run(context.run_id, "failed", str(exc)[:1000])
        add_chat_message(
            context.user_id,
            context.project_id,
            context.session_id,
            "assistant",
            error_text,
            context.run_id,
        )
        yield sse_event("error", {"message": error_text})
    finally:
        service_state.CURRENT_RUN_CONTEXT.reset(context_token)
        cleanup_run_work(context)


def simulate_agent_response(context: RunContext, prompt: str) -> str:
    project = get_project(context.project_id)
    project_root = Path(project["path"])
    p_lower = prompt.lower()

    if project["id"] == "hpi-analytics" and "california" in p_lower and "growth" in p_lower:
        code = """
import pandas as pd
df = pd.read_csv('data/hpi_master.csv', dtype={'note': str, 'place_id': str}, low_memory=False)
ca = df[(df['level'] == 'State') & (df['place_id'] == 'CA') & (df['hpi_type'] == 'traditional') & (df['frequency'] == 'quarterly')]
v1 = ca[(ca['yr'] == 2010) & (ca['period'] == 1)]['index_nsa'].values[0]
v2 = ca[(ca['yr'] == 2020) & (ca['period'] == 1)]['index_nsa'].values[0]
print(f"VALS: {v1}, {v2}, {((v2-v1)/v1)*100:.2f}%")
"""
        out = execute_python_code(code, project_root)
        return (
            "Based on the HPI dataset, California (CA) experienced a growth of approximately "
            f"**79.40%** between Q1 2010 and Q1 2020.\n\n```\n{out}\n```"
        )

    if project["id"] == "hpi-analytics" and ("plot" in p_lower or "chart" in p_lower):
        chart_id = uuid.uuid4().hex[:6]
        chart_path = context.artifact_directory / f"hpi_{chart_id}.png"
        code = f"""
import pandas as pd
import matplotlib.pyplot as plt
df = pd.read_csv('data/hpi_master.csv', dtype={{'note': str, 'place_id': str}}, low_memory=False)
plt.figure(figsize=(8, 4))
plt.grid(True, linestyle='--', alpha=0.5)
for s in ['CA', 'NY', 'TX']:
    sdf = df[(df['level'] == 'State') & (df['place_id'] == s) & (df['frequency'] == 'quarterly') & (df['yr'] >= 2015)].sort_values(['yr', 'period'])
    plt.plot(sdf['yr'] + (sdf['period']-1)/4.0, sdf['index_nsa'], label=s, linewidth=2)
plt.title("HPI Comparison (2015 - Present)")
plt.legend()
plt.tight_layout()
plt.savefig(r'{chart_path}', dpi=150)
"""
        execute_python_code(code, project_root)
        return (
            "Here is the housing price index comparison chart for CA, NY, and TX from 2015 to present:\n\n"
            f"![HPI Comparison Chart]({chart_path})"
        )

    return (
        f"I received your request for project '{project['name']}': '{prompt}'. "
        "LLM API key is not set, so this is a local test response."
    )
