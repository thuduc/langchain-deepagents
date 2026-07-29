"""The agent tier as a standalone service.

Implements the contract AgentCore Runtime requires of a container: a health
check at GET /ping and the work at POST /invocations, on port 8080. Nothing
about it is AWS-specific, which is deliberate -- the same image runs under
`uvicorn` on a laptop, and that is how the boundary gets tested without
deploying anything.

What it does *not* have is as important as what it does. There is no application
database here, no user session, no artifact table, no browser. Identity arrives
in the payload, already established by the web tier, which is the only side that
can authenticate anyone. This process runs the graph, drives the sandbox, and
hands back events and files.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from deep_agents_app.domain import ProjectContext, RunContext

# The workspace service and the engine import each other, and only the workspace
# side can be first: it re-exports the engine's names once the engine has loaded.
# The web application gets that ordering for free by importing services first;
# this entry point has to ask for it.
from deep_agents_app.services import workspace  # noqa: F401
from deep_agents_app.runtime import agent_protocol, checkpointer, engine, project_hydration
from deep_agents_app.runtime import sandbox_runs
from deep_agents_app.runtime.sandbox import get_backend
from deep_agents_app.schemas import AgentInvocationRequest


logger = logging.getLogger(__name__)

# Hydrated project content lives here for the life of the container. Sharing it
# between invocations is the point: a conversation's turns land on the same
# microVM, and re-downloading tens of megabytes per turn would dominate the cost
# of a short question.
PROJECT_CACHE = Path(tempfile.gettempdir()) / "deep-agents-projects"

# How many invocations are in flight. AgentCore decides whether a session is idle
# from the /ping status, and reclaims one that has looked idle for fifteen
# minutes -- so a run that takes longer than that has to say it is still working
# or the platform will terminate the microVM underneath it.
_active_invocations = 0
_active_guard = threading.Lock()


def _invocation_started() -> None:
    global _active_invocations
    with _active_guard:
        _active_invocations += 1


def _invocation_finished() -> None:
    global _active_invocations
    with _active_guard:
        _active_invocations = max(0, _active_invocations - 1)


def health_status() -> str:
    """What /ping reports: whether this container is busy, in AgentCore's words.

    The two values are the platform's, not ours. 'HealthyBusy' keeps the session
    alive; 'Healthy' lets its idle clock run.
    """
    with _active_guard:
        return "HealthyBusy" if _active_invocations else "Healthy"


def invocation_events(request: AgentInvocationRequest) -> Iterator[str]:
    """Run one prompt, streaming protocol events.

    Generated files land in a directory belonging to this process, and are
    uploaded for the caller to collect once the run finishes. The directory is
    removed either way: it is scratch space on compute that may be reclaimed the
    moment this returns.
    """
    scratch = Path(tempfile.mkdtemp(prefix=f"agent-run-{request.run_id}-"))
    context = RunContext(
        user_id=request.user_id,
        project_id=request.project_id,
        session_id=request.session_id,
        run_id=request.run_id,
        artifact_directory=scratch,
    )
    # Marked before any work, released in the finally, so /ping reports this
    # container busy for exactly as long as it is.
    _invocation_started()
    try:
        root = project_hydration.hydrate(
            request.project_slug, request.content_revision, PROJECT_CACHE
        )
        project = ProjectContext(
            id=request.project_id,
            name=request.project_name,
            slug=request.project_slug,
            root=root,
            content_revision=request.content_revision,
            model=request.model,
        )
        for event in engine.run_agent_events(
            context, request.prompt, project, ship_artifacts=True
        ):
            yield agent_protocol.encode(event)
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised
        # Raising here would break the response mid-stream and leave the web tier
        # guessing. An error event is something it can act on.
        logger.exception("Invocation for run %s failed", request.run_id)
        yield agent_protocol.encode(agent_protocol.error(str(exc)[:1000]))
    finally:
        _invocation_finished()
        shutil.rmtree(scratch, ignore_errors=True)


@asynccontextmanager
async def agent_lifespan(_: FastAPI):
    """Prove this container's dependencies work before it accepts any work.

    The web tier has always done this; the agent tier did not, and it matters
    more here. This is the process that reads the gateway key, opens the
    checkpoint store and drives the sandbox -- so a misconfigured container used
    to start perfectly happily, answer /ping, and be marked READY. The
    deployment looked green and the first sign of trouble was a user being told
    their run had failed, with the reason only in this container's logs.

    Worse, /ping kept it looking healthy, so every later session was routed here
    to fail the same way, each paying a cold start and a project hydration
    first. A broken deployment produced a stream of unremarkable run failures
    rather than one obvious one.

    Raising here stops the container starting, which AgentCore surfaces as a
    RuntimeClientError with the cause in CloudWatch. That is the deliberate
    trade: failing loudly at deploy beats failing quietly per prompt. The cost
    is that a transient AWS fault during startup fails the start rather than
    being survived by a later retry.
    """
    workspace.preflight_model_gateway()
    checkpointer.preflight()
    get_backend()
    try:
        yield
    finally:
        # A sandbox session left open is a microVM still billing, and a cached
        # graph holds a checkpoint connection. Runs release their own, so this
        # only catches what a hard stop would otherwise strand.
        sandbox_runs.close_all_sessions()
        workspace.invalidate_all_agents()


def create_app() -> FastAPI:
    """Assemble the agent service."""
    application = FastAPI(title="Deep Agents Agent Runtime", lifespan=agent_lifespan)

    @application.get("/ping")
    def ping():
        """Health check, and the only thing keeping a long run's session alive.

        AgentCore polls this both to decide the container is ready and to decide
        whether the session is idle: fifteen minutes of looking idle and it
        reclaims the microVM, mid-run if need be. Reporting 'HealthyBusy' while
        an invocation is in flight is what prevents that.

        `time_of_last_update` is deliberately omitted. It is optional, and a
        value that advances on every ping reads as a status that never settles,
        which stops the idle timeout firing at all and leaks sessions until they
        hit MaxLifetime. Left out, the platform tracks the changes itself.
        """
        return {"status": health_status()}

    @application.post("/invocations")
    async def invocations(http_request: Request):
        """Run one prompt and stream the result.

        Streamed rather than returned whole because a run takes minutes, and a
        user watching a blank screen for that long will assume it has hung.

        The body is parsed here rather than declared as a typed parameter, so
        that a caller which forwards the payload without a JSON content type
        still works. AgentCore does exactly that unless asked otherwise, and the
        resulting rejection is a bare 422 at the caller with the reason visible
        only inside this container.
        """
        try:
            payload = json.loads(await http_request.body())
            request = AgentInvocationRequest.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            return JSONResponse(
                status_code=422, content={"detail": f"Malformed invocation: {exc}"}
            )
        return StreamingResponse(
            invocation_events(request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return application


app = create_app()
