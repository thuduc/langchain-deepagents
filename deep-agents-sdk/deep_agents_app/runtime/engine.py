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
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from deep_agents_app.domain import ProjectContext, RunContext
from deep_agents_app.runtime import (
    agent_client,
    agent_protocol,
    artifact_transfer,
    cancellation,
    checkpointer,
    sandbox_runs,
)
from deep_agents_app.runtime.cancellation import RunCancelled
from deep_agents_app.runtime.checkpointer import Checkpointer
from deep_agents_app.runtime.sandbox import SandboxError, describe
from deep_agents_app.services import workspace as service_state
from deep_agents_app.services.workspace import (
    add_chat_message,
    bounded_env_int,
    finish_task_run,
    logger,
    prepare_response_artifacts,
    project_filesystem_permission_specs,
    register_run_artifacts,
    scan_skills,
    session_thread_id,
    start_run_heartbeat,
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


def execute_python_code(code: str, project: ProjectContext) -> str:
    """Run agent-generated Python in this run's sandbox.

    The sandbox presents project data at data/ and collects anything written to
    out/. Which backend serves that contract is chosen by DEEP_AGENTS_SANDBOX.
    """
    context = service_state.CURRENT_RUN_CONTEXT.get()
    if context is None:
        return "Error executing code: no active user run context."

    if context.project_id != project.id:
        return "Error executing code: active run context does not match the selected project."

    # Refuse before opening a sandbox session nobody is waiting for. Measured on
    # a live run, this roughly halves the compute a cancelled run goes on to
    # consume; it does not stop it. LangGraph's ToolNode catches whatever a tool
    # raises and hands it back to the model as an error message, so this cannot
    # abort the graph -- it only stops the expensive half. The graph itself runs
    # on until its plan is exhausted.
    if cancellation.is_cancelled(context.run_id):
        raise RunCancelled(context.run_id)

    try:
        session = sandbox_runs.session_for_run(
            context,
            project_slug=project.slug,
            project_root=project.root,
            timeout_seconds=bounded_env_int("PYTHON_EXECUTION_TIMEOUT_SECONDS", 30, 1, 3600),
            content_revision=project.content_revision,
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


def load_skill_prompt(project: ProjectContext, skill_name: str) -> str:
    """Build a skill subagent's system prompt from its SKILL.md and references.

    Reference files are inlined so the specialist has the schema and definitions
    it needs without spending tool calls rediscovering them. They are read from
    the project root the caller supplied, which is a real directory here and a
    directory hydrated from object storage inside a Runtime microVM.
    """
    project_root = project.root
    skill_path = project.skills_dir / skill_name / "SKILL.md"
    references_dir = project.skills_dir / skill_name / "references"

    prompt = (
        f"You are an advanced specialist worker executing tasks for the skill '{skill_name}'.\n"
        f"The selected project is '{project.name}'. Its shared source root is: {project_root}.\n"
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


def get_supervisor_system_prompt(project: ProjectContext) -> str:
    """The coordinator prompt: plan, delegate to one skill, synthesise."""
    return (
        "You are a supervisor coordinator. Your goal is to analyze the user prompt and coordinate the plan.\n"
        "Use the Deep Agents skills library and task tool for the currently selected project to decide which specialist "
        f"capability applies. The selected project is '{project.name}'.\n\n"
        "Core Objective: Analyze the user's request. Create a plan, delegate substantive analysis to the best "
        "project-specific subagent using the task tool, synthesize the subagent result, and present the final answer. "
        "Delegate each requested deliverable once and tell the specialist to return analytical findings, not only a file path. "
        "Never repeat generated filenames, artifact directories, absolute paths, or path-only visualization placeholders "
        "from a specialist response. Describe the visualization's meaning in prose; the server attaches every retained "
        "download in the authenticated Generated artifacts section."
    )


def portkey_provider_slug() -> str:
    """The gateway provider that serves the configured models.

    Required rather than defaulted. It used to fall back to a provider left over
    from an earlier setup, so a missing value did not fail -- it silently built
    model names against the wrong provider and was rejected by the gateway on
    someone's first prompt, a long way from the setting that caused it.
    """
    slug = os.environ.get("PORTKEY_PROVIDER_SLUG", "").strip()
    if not slug:
        raise RuntimeError(
            "PORTKEY_PROVIDER_SLUG is required whenever a model gateway is "
            "configured; it names the Portkey provider serving the models in "
            "AVAILABLE_MODELS."
        )
    return slug


def gateway_secret_arn() -> str:
    """The secret holding the gateway key, when the key is not passed directly."""
    return os.environ.get("PORTKEY_API_KEY_SECRET_ARN", "").strip()


def model_gateway_configured() -> bool:
    """Whether there is a gateway to call at all.

    False is a supported state, not a misconfiguration: without one the
    application answers from simulate_agent_response, which is what makes it
    possible to develop everything around the model with no credentials.
    """
    return bool(os.environ.get("PORTKEY_API_KEY", "").strip() or gateway_secret_arn())


def portkey_api_key() -> str:
    """The gateway key, from the environment or from Secrets Manager.

    Two sources, one contract, exactly as the sandbox and checkpointer have. The
    plain variable is for development, where the alternative would be needing AWS
    to run anything. Anywhere the value would sit in a deployed configuration it
    should be an ARN instead, because a runtime's environment is readable through
    its control plane by anyone with read access, and passing a key as a stack
    parameter also leaves it in shell history and CI logs. An ARN there is worth
    nothing on its own, every read of the secret is a CloudTrail event, and the
    key can be rotated without a deployment.
    """
    direct = os.environ.get("PORTKEY_API_KEY", "").strip()
    if direct:
        return direct

    arn = gateway_secret_arn()
    if not arn:
        raise RuntimeError(
            "No model gateway key is configured. Set PORTKEY_API_KEY for local "
            "development, or PORTKEY_API_KEY_SECRET_ARN where the value must not "
            "appear in the environment."
        )
    return _secret_value(arn)


# The fetched key, and when. Re-read periodically rather than cached for the life
# of the process, so that rotating the secret takes effect on its own -- the point
# of holding a reference rather than a value is lost if a running container has to
# be restarted to notice.
_SECRET_CACHE_SECONDS = 300
_secret_cache: Dict[str, tuple] = {}
_secret_lock = threading.Lock()


def _secret_value(arn: str) -> str:
    """Read the secret, remembering it briefly so every run does not fetch it."""
    with _secret_lock:
        cached = _secret_cache.get(arn)
        if cached and time.monotonic() - cached[1] < _SECRET_CACHE_SECONDS:
            return cached[0]

    import boto3

    region = os.environ.get("AGENTCORE_REGION") or None
    try:
        response = boto3.client("secretsmanager", region_name=region).get_secret_value(
            SecretId=arn
        )
    except Exception as exc:
        # Deliberately not reported with the ARN's contents or the boto error's
        # full text in the message the caller may surface; the detail is logged.
        logger.exception("Could not read the model gateway key from %s", arn)
        raise RuntimeError(
            "The model gateway key could not be read from Secrets Manager. "
            "Check PORTKEY_API_KEY_SECRET_ARN and that this role may call "
            "secretsmanager:GetSecretValue on it."
        ) from exc

    secret = (response.get("SecretString") or "").strip()
    if not secret:
        raise RuntimeError(f"The secret at {arn} holds no string value.")

    # A JSON secret is the Secrets Manager default shape, so accept either that
    # or a bare string rather than making the operator care which they created.
    if secret.startswith("{"):
        try:
            fields = json.loads(secret)
        except json.JSONDecodeError:
            fields = {}
        if isinstance(fields, dict):
            for key in ("PORTKEY_API_KEY", "api_key", "apiKey", "key", "value"):
                candidate = str(fields.get(key, "")).strip()
                if candidate:
                    secret = candidate
                    break

    with _secret_lock:
        _secret_cache[arn] = (secret, time.monotonic())
    return secret


def reset_gateway_key_cache() -> None:
    """Forget any fetched key. Intended for tests and configuration reloads."""
    with _secret_lock:
        _secret_cache.clear()


def preflight_model_gateway() -> None:
    """Prove the gateway settings work, at startup rather than on first prompt.

    Fetches the key as well as checking the provider, because a missing secret or
    a denied GetSecretValue is a deployment mistake, and finding it here costs a
    failed deploy rather than a failed prompt after the agent has done the work.
    """
    if model_gateway_configured():
        portkey_provider_slug()
        portkey_api_key()


def get_llm_instance(model_name: str):
    """Build the chat model, routed through the Portkey gateway.

    The model is passed in rather than read from settings: the agent may be
    running where the settings table is not.
    """
    from langchain_openai import ChatOpenAI
    from portkey_ai import PORTKEY_GATEWAY_URL, createHeaders

    gateway_key = portkey_api_key()
    provider_slug = portkey_provider_slug()

    temp_env = os.environ.get("TEMPERATURE")
    if temp_env is not None:
        try:
            temperature = float(temp_env)
        except ValueError:
            temperature = 0.0
    else:
        temperature = 1.0 if "gpt-5" in model_name else 0.0

    headers = createHeaders(api_key=gateway_key, provider=provider_slug)
    return ChatOpenAI(
        model=f"@{provider_slug}/{model_name}" if not model_name.startswith("@") else model_name,
        temperature=temperature,
        base_url=PORTKEY_GATEWAY_URL,
        default_headers=headers,
        api_key=gateway_key,
    )


def build_skill_subagents(
    project: ProjectContext,
    skills_source: List[Any],
    tools: List[Any],
) -> List[Dict[str, Any]]:
    """Turn each discovered project skill into a specialist subagent."""
    subagents: List[Dict[str, Any]] = []
    for skill in scan_skills(project.skills_dir):
        skill_name = skill["name"]
        subagents.append(
            {
                "name": skill_name,
                "description": skill["description"],
                "system_prompt": load_skill_prompt(project, skill_name),
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


@dataclass
class AgentRuntime:
    """A cached graph, the checkpointer it holds open, and what it was built from."""
    graph: Any
    checkpointer: Checkpointer
    stamp: tuple


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
            runtime.checkpointer.close()
        except Exception as exc:
            logger.warning("Could not close the checkpointer for %s: %s", project_id, exc)


def invalidate_all_agents() -> None:
    """Drop every cached graph and close its checkpoint connection."""
    for project_id in list(_agent_graphs):
        invalidate_project_agent(project_id)


def delete_checkpoint_thread(thread_id: str) -> None:
    """Erase one conversation's stored memory when its session is deleted."""
    checkpointer.delete_thread(thread_id)


def get_agent_graph(project: ProjectContext):
    """Build or return the cached Deep Agents graph for a project.

    The cache validates itself rather than relying on being told to forget. A
    graph bakes in the project's name, its skills and the model, so a cached one
    is reused only while the project it was built from still stamps the same.
    That matters because invalidation does not cross a process boundary: another
    worker, another task, or a Runtime microVM would otherwise keep serving
    prompts assembled from a skill that has since been edited.

    Double-checked under a lock: two concurrent first prompts would otherwise
    each build a graph and open a checkpoint connection, and one would leak.
    """
    with _agent_graph_lock:
        cached = _agent_graphs.get(project.id)
        if cached and cached.stamp == project.stamp:
            return cached.graph

    from langchain_core.tools import tool
    from deepagents import FilesystemPermission, create_deep_agent
    from deepagents.backends import FilesystemBackend

    project_root = project.root
    backend = FilesystemBackend(root_dir=project_root, virtual_mode=True)
    skills = [("/skills", project.name)]
    permissions = [
        FilesystemPermission(**permission_spec)
        for permission_spec in project_filesystem_permission_specs()
    ]
    llm = get_llm_instance(project.model)

    @tool
    def execute_python(code: str) -> str:
        """Execute Python in an isolated sandbox holding project data at data/."""
        return execute_python_code(code, project)

    @tool
    def get_project_context() -> Dict[str, str]:
        """Return the sandbox paths for reading project data and writing outputs."""
        context = service_state.CURRENT_RUN_CONTEXT.get()
        if context is None or context.project_id != project.id:
            raise RuntimeError("No active run context for the selected project")
        # Server-side paths are deliberately withheld: they mean nothing inside the
        # sandbox, and the prompts forbid repeating them back to the user anyway.
        return {
            "project_name": project.name,
            "data_directory": "data",
            "artifact_directory": "out",
            "usage": (
                "Read inputs from data/ and write every generated file into out/, "
                "using relative paths. The server collects out/ after each execution "
                "and presents the files as authenticated downloads."
            ),
        }

    specialist_tools = [execute_python, get_project_context]
    subagents = build_skill_subagents(project, skills, specialist_tools)

    handle = checkpointer.create_checkpointer()
    graph = create_deep_agent(
        model=llm,
        tools=[],
        skills=skills,
        backend=backend,
        permissions=permissions,
        subagents=subagents,
        system_prompt=get_supervisor_system_prompt(project),
        checkpointer=handle.saver,
    )
    runtime = AgentRuntime(graph=graph, checkpointer=handle, stamp=project.stamp)
    with _agent_graph_lock:
        existing = _agent_graphs.get(project.id)
        if existing and existing.stamp == project.stamp:
            handle.close()          # lost the race; theirs is equally current
            return existing.graph
        if existing:
            existing.checkpointer.close()   # superseded: do not leak its resources
        _agent_graphs[project.id] = runtime
    return graph


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


def run_agent_events(
    context: RunContext,
    prompt: str,
    project: ProjectContext,
    ship_artifacts: bool = False,
) -> Iterator[Dict[str, Any]]:
    """Run a prompt, yielding protocol events. The agent half of a run.

    Owns the graph, the sandbox and the files a run generates. Deliberately
    knows nothing about the application database: no artifact rows, no chat
    messages, no run status. That is what lets this half run somewhere else --
    an AgentCore Runtime microVM -- with the persisting half left behind.

    `project` carries everything about the project this would otherwise query a
    database for. `ship_artifacts` is true exactly when "somewhere else" is the
    case, and generated files have to be handed over rather than left in place.
    """
    final_text = ""
    delta_parts: List[str] = []
    watch = cancellation.watch(context.run_id)
    # Held out here so the finally can close it deliberately. Left to refcounting
    # it is closed while the generator's frame is torn down, which happens after
    # everything else and cannot be timed or logged.
    agent_events = None
    try:
        if not model_gateway_configured():
            yield agent_protocol.status("Preparing a local response…")
            context_token = service_state.CURRENT_RUN_CONTEXT.set(context)
            try:
                final_text = simulate_agent_response(context, prompt, project)
            finally:
                service_state.CURRENT_RUN_CONTEXT.reset(context_token)
        else:
            agent = get_agent_graph(project)
            yield agent_protocol.status("Preparing the agent workspace…")
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
                # Checked between steps, never during one. A sandbox execution
                # already in flight is allowed to finish; interrupting it would
                # leave a workspace the next collection would misread.
                if watch.cancelled():
                    raise RunCancelled(context.run_id)

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
                        yield agent_protocol.status(status)
                    continue

                if mode == "messages":
                    # Kept, not forwarded. The caller cannot show these: raw
                    # model text may name artifact paths that are not links yet,
                    # so the answer is only released once it has been rewritten.
                    # They are still worth accumulating, as the fallback below is
                    # the only source of an answer when no update carries one.
                    fragment = delta_from_message_event(namespace, data)
                    if fragment:
                        delta_parts.append(fragment)
                    continue

                if mode == "updates":
                    update_text = final_text_from_update(namespace, data)
                    if update_text:
                        final_text = update_text

        if not final_text and delta_parts:
            final_text = "".join(delta_parts)
        if not final_text:
            final_text = "The model returned an empty response for this request. Please try again or rephrase your prompt."

        yield agent_protocol.status("Preparing generated files…")
        prefix = (
            artifact_transfer.ship(context.artifact_directory, context.run_id)
            if ship_artifacts
            else None
        )
        yield agent_protocol.artifacts(prefix)
        yield agent_protocol.answer(final_text)
    except RunCancelled:
        logger.info("Agent run %s stopped at the caller's request", context.run_id)
        yield agent_protocol.cancelled()
    except GeneratorExit:
        raise
    except Exception as exc:
        logger.exception("Agent run %s failed", context.run_id)
        yield agent_protocol.error(str(exc)[:1000])
    finally:
        _release_run(context, agent_events)


def _release_run(context: RunContext, agent_events: Any) -> None:
    """Let go of everything a run was holding, and say so if it took a while.

    Belongs to the agent half: it owns the graph and the sandbox, and it is the
    side that knows the run has stopped. The cancellation record is cleared here
    for the same reason -- the web half only ever writes it.
    """
    cancellation.clear(context.run_id)

    # Abandoning the graph stream does not stop the step it is in the middle of;
    # closing it waits for that step to finish. A model call can be a minute on
    # its own, so this is the floor on how quickly a cancelled run can let go --
    # and on AgentCore it is a minute of microVM.
    graph_seconds = 0.0
    if agent_events is not None:
        graph_started = time.monotonic()
        closer = getattr(agent_events, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception as exc:  # noqa: BLE001 - teardown must not raise
                logger.warning(
                    "Closing the graph stream for run %s failed: %s",
                    context.run_id,
                    exc,
                )
        graph_seconds = time.monotonic() - graph_started

    sandbox_started = time.monotonic()
    sandbox_runs.close_run_session(context)
    sandbox_seconds = time.monotonic() - sandbox_started

    if graph_seconds + sandbox_seconds > 5:
        logger.warning(
            "Releasing run %s took %.1fs (graph %.1fs, sandbox %.1fs)",
            context.run_id,
            graph_seconds + sandbox_seconds,
            graph_seconds,
            sandbox_seconds,
        )


def stream_agent_events(context: RunContext, prompt: str) -> Iterator[str]:
    """Run a prompt for a browser, yielding server-sent events. The web half.

    Consumes the agent half's events and does everything that needs the
    application database: recording progress, registering generated files,
    rewriting the answer's file references into authenticated links, saving the
    message and closing out the run.

    Which side of the boundary the agent half runs on is the transport's
    business, not this function's.
    """
    started_at = time.monotonic()
    final_text = ""
    staging_prefix: Optional[str] = None
    outcome = "completed"
    # Started before anything else and stopped in the finally, so the run counts
    # as alive for exactly as long as this function holds it. Progress events
    # also stamp it, but they cannot be relied on to: a long model call is
    # silent for minutes, and the AgentCore Runtime transport delivers nothing
    # at all until the run ends.
    heartbeat = start_run_heartbeat(context.run_id)
    try:
        yield sse_event(
            "run",
            {
                "project_id": context.project_id,
                "session_id": context.session_id,
                "run_id": context.run_id,
                "status": "Preparing the agent workspace…",
            },
        )

        for event in agent_client.agent_events(context, prompt):
            kind = event.get("type")
            if kind == agent_protocol.STATUS:
                yield run_activity_event(context, event.get("message", ""))
            elif kind == agent_protocol.ARTIFACTS:
                staging_prefix = event.get("staging_prefix")
            elif kind == agent_protocol.ANSWER:
                final_text = event.get("text", "")
            elif kind == agent_protocol.CANCELLED:
                outcome = "cancelled"
            elif kind == agent_protocol.ERROR:
                raise RuntimeError(event.get("message", "the agent reported a failure"))

        if outcome == "cancelled":
            finish_task_run(context.run_id, "cancelled")
            return

        if staging_prefix:
            # The agent ran elsewhere and left its files in object storage.
            yield run_activity_event(context, "Collecting generated files…")
            artifact_transfer.fetch(staging_prefix, context.artifact_directory)

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
        if staging_prefix:
            artifact_transfer.discard(staging_prefix)
        # The answer travels once, in `final`, and only after the rewrite that
        # turns file references into authenticated links. There is no partial
        # text before that, by design.
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
        # The caller stopped reading. Record it for whoever is running the graph,
        # which may be this process or a microVM that cannot see the disconnect.
        cancellation.request(context.run_id)
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
        # Whatever happened, this process is no longer holding the run, so it
        # must stop claiming to be. The run's terminal status is already written
        # by each branch above; this only ends the liveness stamping.
        heartbeat.stop()


def simulate_agent_response(
    context: RunContext, prompt: str, project: ProjectContext
) -> str:
    """Canned answers used when no model gateway is configured."""
    p_lower = prompt.lower()

    if project.id == "hpi-analytics" and "california" in p_lower and "growth" in p_lower:
        code = """
import pandas as pd
df = pd.read_csv('data/hpi_master.csv', dtype={'note': str, 'place_id': str}, low_memory=False)
ca = df[(df['level'] == 'State') & (df['place_id'] == 'CA') & (df['hpi_type'] == 'traditional') & (df['frequency'] == 'quarterly')]
v1 = ca[(ca['yr'] == 2010) & (ca['period'] == 1)]['index_nsa'].values[0]
v2 = ca[(ca['yr'] == 2020) & (ca['period'] == 1)]['index_nsa'].values[0]
print(f"VALS: {v1}, {v2}, {((v2-v1)/v1)*100:.2f}%")
"""
        out = execute_python_code(code, project)
        return (
            "Based on the HPI dataset, California (CA) experienced a growth of approximately "
            f"**79.40%** between Q1 2010 and Q1 2020.\n\n```\n{out}\n```"
        )

    if project.id == "hpi-analytics" and ("plot" in p_lower or "chart" in p_lower):
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
        execute_python_code(code, project)
        # No link here: the server appends the registered artifact after the run.
        return (
            "Here is the housing price index comparison for CA, NY, and TX "
            "from 2015 to present."
        )

    return (
        f"I received your request for project '{project.name}': '{prompt}'. "
        "LLM API key is not set, so this is a local test response."
    )
