"""The chat endpoint: one streaming route, which is what the UI uses."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from deep_agents_app.api.dependencies import get_current_user
from deep_agents_app.domain import CurrentUser, RunContext
from deep_agents_app.schemas import ChatRequest
from deep_agents_app.services import workspace


router = APIRouter(prefix="/api/chat", tags=["chat"])


def prepare_run(request: ChatRequest, user: CurrentUser):
    """Validate a chat request and open a run for it.

    The authorization gate: passing another user's session_id fails here,
    because get_session matches on the caller's id. Every value later used to
    scope the sandbox and its storage comes from what this function returns,
    never from the request body.
    """
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(request.message) > 100_000:
        raise HTTPException(status_code=400, detail="Message exceeds 100,000 characters")
    workspace.get_project(request.project_id)
    session_id = request.session_id
    if session_id:
        workspace.get_session(user.id, request.project_id, session_id)
    else:
        session_id = workspace.create_session(user.id, request.project_id)["id"]
    workspace.maybe_title_session(user.id, request.project_id, session_id, request.message)
    run = workspace.create_task_run(user.id, request.project_id, session_id, request.message)
    workspace.add_chat_message(user.id, request.project_id, session_id, "user", request.message, run["id"])
    return session_id, run


@router.post("/stream")
def chat_stream(request: ChatRequest, user: CurrentUser = Depends(get_current_user)):
    """Run the agent, streaming progress as server-sent events.

    Emits `run`, then `status` updates while the agent works, then `final` (or
    `error`). Buffering is disabled so status lines reach the browser as they
    happen rather than in a burst at the end.

    The only way to start a run. A blocking variant existed alongside this one
    and was removed: it ran the graph in this process directly rather than
    through the agent transport, so it quietly ignored
    DEEP_AGENTS_AGENT_TRANSPORT and persisted finished runs differently.
    """
    _, run = prepare_run(request, user)
    context: RunContext = run["context"]
    return StreamingResponse(
        workspace.stream_agent_events(context, request.message),
        media_type="text/event-stream",
        headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
    )
