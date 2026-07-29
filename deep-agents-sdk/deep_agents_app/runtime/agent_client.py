"""Reaching the agent, wherever it happens to be running.

    DEEP_AGENTS_AGENT_TRANSPORT=local     in this process
    DEEP_AGENTS_AGENT_TRANSPORT=http      another process, over plain HTTP
    DEEP_AGENTS_AGENT_TRANSPORT=runtime   an AgentCore Runtime microVM

`local` is the development default and the only one that needs no AWS. `http`
exists because it is the same contract as `runtime` without the AWS transport
wrapped around it, which makes the boundary testable on one machine -- and
because a container run locally is the fastest way to reproduce what Runtime
will do.

Whichever is in use, callers see the same stream of protocol events.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterator

from deep_agents_app.domain import RunContext
from deep_agents_app.runtime import agent_protocol


logger = logging.getLogger(__name__)

DEFAULT_TRANSPORT = "local"

# Long enough for a slow run, since the agent streams progress throughout and a
# silent gap is a real failure rather than a slow model.
REQUEST_TIMEOUT_SECONDS = 900


class AgentTransportError(RuntimeError):
    """The agent could not be reached, or refused the request."""


def configured_transport() -> str:
    """The transport named by DEEP_AGENTS_AGENT_TRANSPORT, defaulting to local."""
    return (
        os.environ.get("DEEP_AGENTS_AGENT_TRANSPORT") or DEFAULT_TRANSPORT
    ).strip().lower()


# What each remote transport cannot work without. Checked at startup rather than
# on the first prompt, where the failure lands on a user instead of a deploy.
REQUIRED_SETTINGS = {
    "http": ("DEEP_AGENTS_AGENT_URL", "the URL of the agent server"),
    "runtime": ("AGENTCORE_RUNTIME_ARN", "the ARN of the AgentCore Runtime"),
}


def preflight() -> None:
    """Validate the transport and the stores it depends on. Raise if unusable.

    A remote transport moves the graph off this host, and two pieces of state
    stop working the moment it does. Both fail *quietly*, which is why this
    refuses to start rather than warning:

      * A checkpoint file on this host is unreachable from the agent, so every
        turn begins with no memory -- while the transcript still renders from
        chat_messages, so the chat looks fine and the agent simply has amnesia.
      * An in-process cancellation registry is not the one the agent reads, so
        the web tier records a stop that nothing acts on, and the run it failed
        to stop is a microVM billing by the second.

    Neither surfaces as an error anywhere. Left to be discovered in production,
    they would be discovered as "the agent forgets things" and "stop does
    nothing" -- a long way from the setting that caused them.
    """
    from deep_agents_app.runtime import cancellation, checkpointer

    transport = configured_transport()
    if transport not in {"local", "http", "runtime"}:
        raise AgentTransportError(
            f"Unknown DEEP_AGENTS_AGENT_TRANSPORT {transport!r}; "
            "expected local, http or runtime"
        )
    if transport == "local":
        return

    variable, purpose = REQUIRED_SETTINGS[transport]
    if not os.environ.get(variable, "").strip():
        raise AgentTransportError(
            f"{variable} is required for the {transport!r} agent transport: {purpose}"
        )

    for setting, actual, expected in (
        ("DEEP_AGENTS_CHECKPOINTER", checkpointer.configured_checkpointer_name(), "dynamodb"),
        ("DEEP_AGENTS_CANCELLATION", cancellation.configured_store_name(), "dynamodb"),
    ):
        if actual != expected:
            raise AgentTransportError(
                f"DEEP_AGENTS_AGENT_TRANSPORT={transport!r} runs the agent off this "
                f"host, so {setting}={actual!r} is unreachable from it. "
                f"Set {setting}={expected!r}."
            )


def agent_events(context: RunContext, prompt: str) -> Iterator[Dict[str, Any]]:
    """Run a prompt and yield the agent's protocol events."""
    transport = configured_transport()
    if transport == "local":
        return _local_events(context, prompt)
    if transport == "http":
        return _http_events(context, prompt)
    if transport == "runtime":
        return _runtime_events(context, prompt)
    raise AgentTransportError(
        f"Unknown agent transport {transport!r}; expected local, http or runtime"
    )


def _local_events(context: RunContext, prompt: str) -> Iterator[Dict[str, Any]]:
    """Run the agent here. Artifacts are already where the caller wants them."""
    from deep_agents_app.runtime import engine  # imported late: engine imports this
    from deep_agents_app.services import workspace

    project = workspace.project_context(context.project_id)
    return engine.run_agent_events(context, prompt, project, ship_artifacts=False)


def _payload(context: RunContext, prompt: str) -> Dict[str, Any]:
    """Describe the run for an agent that cannot look anything up itself."""
    from deep_agents_app.services import workspace

    project = workspace.project_context(context.project_id)
    return agent_protocol.invocation_payload(
        user_id=context.user_id,
        project_id=context.project_id,
        session_id=context.session_id,
        run_id=context.run_id,
        prompt=prompt,
        project_name=project.name,
        project_slug=project.slug,
        content_revision=project.content_revision,
        model=project.model,
    )


def _http_events(context: RunContext, prompt: str) -> Iterator[Dict[str, Any]]:
    """Call an agent server over HTTP, streaming its events back."""
    import httpx

    base_url = os.environ.get("DEEP_AGENTS_AGENT_URL", "").strip()
    if not base_url:
        raise AgentTransportError(
            "DEEP_AGENTS_AGENT_URL is required for the http agent transport"
        )

    with httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        with client.stream("POST", "/invocations", json=_payload(context, prompt)) as response:
            if response.status_code != 200:
                response.read()
                raise AgentTransportError(
                    f"Agent server returned {response.status_code}: {response.text[:300]}"
                )
            yield from agent_protocol.decode(response.iter_lines())


def _runtime_events(context: RunContext, prompt: str) -> Iterator[Dict[str, Any]]:
    """Invoke an AgentCore Runtime, streaming its events back.

    The session id is the conversation's, so AgentCore routes every turn of a
    chat to the same microVM. That is an optimisation, not a correctness
    requirement: conversation memory lives in the checkpoint store precisely so
    that a run landing on cold compute still resumes.
    """
    import boto3

    arn = os.environ.get("AGENTCORE_RUNTIME_ARN", "").strip()
    if not arn:
        raise AgentTransportError(
            "AGENTCORE_RUNTIME_ARN is required for the runtime agent transport"
        )

    client = boto3.client(
        "bedrock-agentcore", region_name=os.environ.get("AGENTCORE_REGION") or None
    )
    response = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=_runtime_session_id(context),
        # Both stated explicitly. The payload is forwarded to the container as
        # the request body, and without a content type the framework there sees
        # a string rather than an object and rejects it with a 422 whose only
        # trace is in the container's own logs.
        contentType="application/json",
        accept="text/event-stream",
        payload=json.dumps(_payload(context, prompt)).encode("utf-8"),
    )
    yield from agent_protocol.decode(_iter_text_lines(response.get("response")))


def _runtime_session_id(context: RunContext) -> str:
    """A stable per-conversation id, padded to the length AgentCore requires.

    The service rejects anything shorter than 33 characters, and a chat session
    id on its own is not always long enough.
    """
    raw = f"{context.user_id}-{context.session_id}"
    return raw if len(raw) >= 33 else raw.ljust(33, "0")


def _iter_available_bytes(body: Any) -> Iterator[bytes]:
    """Yield bytes as the stream produces them, never waiting for a full buffer.

    The obvious spelling -- `body.iter_chunks()` -- is wrong here, and quietly
    so. It asks for a fixed 1024 bytes per read, and the read underneath
    accumulates until it has exactly that many or the stream ends. A response
    smaller than one chunk therefore arrives in a single delivery when the run
    finishes, and a live stream is indistinguishable from a buffered one.

    Measured against a real AgentCore runtime: reading a byte at a time, the
    first status line arrived 6s in and the rest followed over the next 22s;
    through iter_chunks() the identical run produced nothing until 27.8s, then
    everything at once. The service streams correctly -- this function did not.

    read1() is the "give me whatever has arrived" counterpart and is preferred
    where the stream offers it. The fallback asks for a single byte, which
    cannot over-wait either, at the cost of a call per byte.
    """
    if body is None:
        return
    raw = getattr(body, "_raw_stream", None)
    read1 = getattr(raw, "read1", None)
    if read1 is not None:
        while True:
            chunk = read1(65536)
            if not chunk:
                return
            yield chunk
    elif hasattr(body, "iter_chunks"):
        yield from body.iter_chunks(chunk_size=1)
    else:
        yield body.read()


def _iter_text_lines(body: Any) -> Iterator[str]:
    """Turn a streaming response body into decoded lines, as they arrive."""
    buffer = ""
    for chunk in _iter_available_bytes(body):
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line
    if buffer:
        yield buffer
