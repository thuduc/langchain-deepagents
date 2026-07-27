"""The agent runtime: graph construction, execution and event streaming.

One Deep Agents graph is built and cached per project. The supervisor plans and
delegates to a subagent per project skill; those subagents get two tools,
`execute_python` and `get_project_context`. Generated Python never runs here — it
goes to the sandbox backend selected by DEEP_AGENTS_SANDBOX.

Conversation memory lives in a LangGraph SQLite checkpointer, keyed by a thread
id that includes the user, so two users in one project never share history.
"""

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from deep_agents_app.domain import RunContext
from deep_agents_app.runtime import sandbox_runs
from deep_agents_app.runtime.sandbox import SandboxError, describe
from deep_agents_app.services import workspace as service_state
from deep_agents_app.services.workspace import (
    add_chat_message,
    bounded_env_int,
    ensure_child_path,
    finish_task_run,
    get_app_settings,
    get_project,
    get_project_root,
    logger,
    prepare_response_artifacts,
    project_filesystem_permission_specs,
    project_skills_source,
    register_run_artifacts,
    scan_project_skills,
    session_thread_id,
    update_task_run_activity,
)


def message_content_to_text(content: Any) -> str:
    """Flatten a message's content, which may be a string or a list of parts."""
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


def execute_python_code(code: str, project_root: Path) -> str:
    """Run agent-generated Python in this run's sandbox.

    The sandbox presents project data at data/ and collects anything written to
    out/. Which backend serves that contract is chosen by DEEP_AGENTS_SANDBOX.
    """
    context = service_state.CURRENT_RUN_CONTEXT.get()
    if context is None:
        return "Error executing code: no active user run context."

    project_root = ensure_child_path(service_state.PROJECTS_ROOT, project_root)
    expected_project_root = get_project_root(context.project_id).resolve()
    if project_root != expected_project_root:
        return "Error executing code: active run context does not match the selected project."

    try:
        project = get_project(context.project_id)
        session = sandbox_runs.session_for_run(
            context,
            project_slug=project["slug"],
            project_data_dir=project_root / "data",
            timeout_seconds=bounded_env_int("PYTHON_EXECUTION_TIMEOUT_SECONDS", 30, 1, 3600),
            content_revision=project["content_revision"],
        )
        result = session.execute(code)
        session.collect_artifacts(context.artifact_directory)
    except SandboxError as exc:
        logger.exception("Sandbox failed for run %s", context.run_id)
        return f"Error executing code: {exc}"
    except Exception:
        # Anything else is infrastructure rather than the model's code: an
        # expired credential, a throttled S3 call, a full disk. Returning it as
        # a tool result lets the agent retry or take another route, where
        # raising would fail the whole run on a condition that is often
        # transient. The detail stays in the logs; the model gets none of it.
        logger.exception("Sandbox execution failed for run %s", context.run_id)
        return (
            "Error executing code: the sandbox was unavailable for this attempt. "
            "The failure is recorded in the server logs."
        )

    return describe(result)


def load_skill_prompt(project_id: str, skill_name: str) -> str:
    """Build a skill subagent's system prompt from its SKILL.md and references.

    Reference files are inlined so the specialist has the schema and definitions
    it needs without spending tool calls rediscovering them.
    """
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
        "The execute_python tool runs in an isolated sandbox with no network access. Read project data from "
        "the relative path 'data/<filename>'. Write every file you generate into the relative path "
        "'out/<filename>'; nothing written anywhere else is kept. Use relative paths only, never absolute ones. "
        "Create each requested deliverable once; if you revise a chart or export, "
        "overwrite the same filename rather than creating alternate versions. Do not include generated filenames, "
        "directory names, absolute paths, empty artifact placeholder sections, or separate file-path listings "
        "in the final answer. Describe the visualization's analytical meaning in prose instead. The server collects "
        "everything in out/ and presents it in a private authenticated Generated artifacts section automatically."
    )
    return prompt


def get_supervisor_system_prompt(project_id: str) -> str:
    """The coordinator prompt: plan, delegate to one skill, synthesise."""
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
    """Build the chat model, routed through the Portkey gateway."""
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
    """Turn each discovered project skill into a specialist subagent."""
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
    """Per-run graph config, carrying the conversation's checkpoint thread id."""
    return {
        "configurable": {
            "thread_id": session_thread_id(user_id, project_id, session_id)
        },
        "recursion_limit": 100,
    }


def response_text_from_agent_result(result: Dict[str, Any]) -> str:
    """Extract the final answer text from a finished graph result."""
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
    """A cached graph together with the checkpoint connection it owns."""
    graph: Any
    checkpointer: Any
    connection: sqlite3.Connection


_agent_graphs: Dict[str, AgentRuntime] = {}
_agent_graph_lock = threading.RLock()


def invalidate_project_agent(project_id: str) -> None:
    """Drop a project's cached graph, so the next prompt rebuilds it.

    Needed after a skill or name change, both of which are baked into prompts.
    """
    with _agent_graph_lock:
        runtime = _agent_graphs.pop(project_id, None)
    if runtime:
        try:
            runtime.connection.close()
        except Exception as exc:
            logger.warning("Could not close checkpoint connection for %s: %s", project_id, exc)


def invalidate_all_agents() -> None:
    """Drop every cached graph and close its checkpoint connection."""
    for project_id in list(_agent_graphs):
        invalidate_project_agent(project_id)


def delete_checkpoint_thread(thread_id: str) -> None:
    """Erase one conversation's stored memory when its session is deleted."""
    if not service_state.AGENT_CHECKPOINT_DB_PATH.exists():
        return
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(service_state.AGENT_CHECKPOINT_DB_PATH, timeout=10, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        saver = SqliteSaver(conn)
        saver.setup()
        saver.delete_thread(thread_id)
    finally:
        conn.close()


def get_agent_graph(project_id: str):
    """Build or return the cached Deep Agents graph for a project.

    Double-checked under a lock: two concurrent first prompts would otherwise
    each build a graph and open a checkpoint connection, and one would leak.
    """
    with _agent_graph_lock:
        cached = _agent_graphs.get(project_id)
        if cached:
            return cached.graph

    from langchain_core.tools import tool
    from deepagents import FilesystemPermission, create_deep_agent
    from deepagents.backends import FilesystemBackend
    from langgraph.checkpoint.sqlite import SqliteSaver

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
        """Execute Python in an isolated sandbox holding project data at data/."""
        return execute_python_code(code, project_root)

    @tool
    def get_project_context() -> Dict[str, str]:
        """Return the sandbox paths for reading project data and writing outputs."""
        context = service_state.CURRENT_RUN_CONTEXT.get()
        if context is None or context.project_id != project_id:
            raise RuntimeError("No active run context for the selected project")
        # Server-side paths are deliberately withheld: they mean nothing inside the
        # sandbox, and the prompts forbid repeating them back to the user anyway.
        return {
            "project_name": project["name"],
            "data_directory": "data",
            "artifact_directory": "out",
            "usage": (
                "Read inputs from data/ and write every generated file into out/, "
                "using relative paths. The server collects out/ after each execution "
                "and presents the files as authenticated downloads."
            ),
        }

    specialist_tools = [execute_python, get_project_context]
    subagents = build_skill_subagents(project_id, skills, specialist_tools)

    service_state.AGENT_CHECKPOINT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(service_state.AGENT_CHECKPOINT_DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    checkpointer = SqliteSaver(conn)

    graph = create_deep_agent(
        model=llm,
        tools=[],
        skills=skills,
        backend=backend,
        permissions=permissions,
        subagents=subagents,
        system_prompt=get_supervisor_system_prompt(project_id),
        checkpointer=checkpointer,
    )
    runtime = AgentRuntime(graph=graph, checkpointer=checkpointer, connection=conn)
    with _agent_graph_lock:
        existing = _agent_graphs.get(project_id)
        if existing:
            conn.close()
            return existing.graph
        _agent_graphs[project_id] = runtime
    return graph


def run_agent(context: RunContext, prompt: str) -> str:
    """Run a prompt to completion and return the answer.

    Falls back to a canned response when no gateway key is configured, so the
    app remains usable offline for development.
    """
    if not os.environ.get("PORTKEY_API_KEY"):
        context_token = service_state.CURRENT_RUN_CONTEXT.set(context)
        try:
            return simulate_agent_response(context, prompt)
        finally:
            service_state.CURRENT_RUN_CONTEXT.reset(context_token)

    agent = get_agent_graph(context.project_id)
    context_token = service_state.CURRENT_RUN_CONTEXT.set(context)
    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=agent_run_config(
                context.user_id, context.project_id, context.session_id
            ),
        )
        return response_text_from_agent_result(result)
    finally:
        service_state.CURRENT_RUN_CONTEXT.reset(context_token)


def sse_event(event_type: str, data: Dict[str, Any]) -> str:
    """Format one server-sent event."""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def chunk_parts(chunk: Any) -> tuple[tuple[str, ...], str, Any]:
    """Unpack a LangGraph stream chunk into namespace, mode and payload."""
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
    """Translate a raw graph event into a status line fit for a user.

    A non-empty namespace means the event came from a subagent rather than the
    supervisor, which is what distinguishes 'a specialist is working' from
    'analysing the request'.
    """
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
    """Extract streamable answer text, ignoring subagent chatter."""
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
    """Recover the final answer from a state update when no delta was streamed."""
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


def run_activity_event(context: RunContext, message: str) -> str:
    """Record progress and format it as an event for the browser."""
    update_task_run_activity(context.run_id, message)
    return sse_event(
        "status",
        {
            "message": message,
            "project_id": context.project_id,
            "session_id": context.session_id,
            "run_id": context.run_id,
        },
    )


def stream_agent_events(context: RunContext, prompt: str) -> Iterator[str]:
    """Run a prompt, yielding events as it progresses.

    Emits `run`, then `status` lines while the agent works, then `final`. The
    finally block releases the sandbox session on every exit path, including the
    GeneratorExit raised when a user navigates away mid-run.
    """
    started_at = time.monotonic()
    final_text = ""
    delta_parts: List[str] = []
    emitted_delta = False
    initial_status = "Preparing the agent workspace…"
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
            yield run_activity_event(context, "Preparing a local response…")
            context_token = service_state.CURRENT_RUN_CONTEXT.set(context)
            try:
                final_text = simulate_agent_response(context, prompt)
            finally:
                service_state.CURRENT_RUN_CONTEXT.reset(context_token)
        else:
            agent = get_agent_graph(context.project_id)
            yield run_activity_event(context, initial_status)
            agent_events = iter(
                agent.stream(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config=agent_run_config(
                        context.user_id, context.project_id, context.session_id
                    ),
                    stream_mode=["updates", "messages", "tasks"],
                    subgraphs=True,
                )
            )
            while True:
                context_token = service_state.CURRENT_RUN_CONTEXT.set(context)
                try:
                    chunk = next(agent_events)
                except StopIteration:
                    break
                finally:
                    service_state.CURRENT_RUN_CONTEXT.reset(context_token)
                namespace, mode, data = chunk_parts(chunk)

                if mode == "tasks":
                    status = task_status_from_event(namespace, data)
                    if status:
                        yield run_activity_event(context, status)
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

        yield run_activity_event(context, "Preparing generated files…")
        artifacts = register_run_artifacts(context)
        yield run_activity_event(context, "Finalizing the response…")
        response_text = prepare_response_artifacts(
            context.user_id, context.project_id, final_text, artifacts
        )
        add_chat_message(
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
    except GeneratorExit:
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
        sandbox_runs.close_run_session(context)


def simulate_agent_response(context: RunContext, prompt: str) -> str:
    """Canned answers used when no model gateway is configured."""
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
        # Writes into out/ with a relative path, exactly as a real run must: the
        # sandbox has no path to the server's artifact directory, and the file is
        # picked up by the collector afterwards like any other deliverable.
        code = """
import pandas as pd
import matplotlib.pyplot as plt
df = pd.read_csv('data/hpi_master.csv', dtype={'note': str, 'place_id': str}, low_memory=False)
plt.figure(figsize=(8, 4))
plt.grid(True, linestyle='--', alpha=0.5)
for s in ['CA', 'NY', 'TX']:
    sdf = df[(df['level'] == 'State') & (df['place_id'] == s) & (df['frequency'] == 'quarterly') & (df['yr'] >= 2015)].sort_values(['yr', 'period'])
    plt.plot(sdf['yr'] + (sdf['period']-1)/4.0, sdf['index_nsa'], label=s, linewidth=2)
plt.title("HPI Comparison (2015 - Present)")
plt.legend()
plt.tight_layout()
plt.savefig('out/hpi_comparison.png', dpi=150)
"""
        execute_python_code(code, project_root)
        # No link here: the server appends the registered artifact after the run.
        return (
            "Here is the housing price index comparison for CA, NY, and TX "
            "from 2015 to present."
        )

    return (
        f"I received your request for project '{project['name']}': '{prompt}'. "
        "LLM API key is not set, so this is a local test response."
    )
