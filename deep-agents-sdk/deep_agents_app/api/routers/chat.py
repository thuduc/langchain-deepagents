import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from deep_agents_app.api.dependencies import get_current_user
from deep_agents_app.domain import CurrentUser, RunContext
from deep_agents_app.schemas import ChatRequest, ChatResponse
from deep_agents_app.runtime import engine
from deep_agents_app.services import artifacts as artifacts_service
from deep_agents_app.services import sessions as sessions_service
from deep_agents_app.services import workspace


router = APIRouter(prefix="/api/chat", tags=["chat"])


def prepare_run(request: ChatRequest, user: CurrentUser):
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(request.message) > 100_000:
        raise HTTPException(status_code=400, detail="Message exceeds 100,000 characters")
    workspace.get_project(request.project_id)
    session_id = request.session_id
    if session_id:
        sessions_service.get_session(user.id, request.project_id, session_id)
    else:
        session_id = sessions_service.create_session(user.id, request.project_id)["id"]
    sessions_service.maybe_title_session(user.id, request.project_id, session_id, request.message)
    run = sessions_service.create_task_run(user.id, request.project_id, session_id, request.message)
    sessions_service.add_chat_message(user.id, request.project_id, session_id, "user", request.message, run["id"])
    return session_id, run


# Both endpoints are async so that a run waiting on the model does not hold a
# worker thread. Their blocking SQLite and filesystem work is offloaded instead,
# which keeps ordinary requests from queueing behind in-flight agent runs.
@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest, user: CurrentUser = Depends(get_current_user)):
    started_at = time.monotonic()
    session_id, run = await run_in_threadpool(prepare_run, request, user)
    context: RunContext = run["context"]
    try:
        response_text = await engine.run_agent(context, request.message)
        artifacts = await run_in_threadpool(artifacts_service.register_run_artifacts, context)
        response_text = await run_in_threadpool(
            artifacts_service.prepare_response_artifacts, user.id, request.project_id, response_text, artifacts
        )
        await run_in_threadpool(
            sessions_service.add_chat_message, user.id, request.project_id, session_id, "assistant", response_text, run["id"]
        )
        sessions_service.finish_task_run(run["id"], "completed")
        return ChatResponse(
            response=response_text,
            project_id=request.project_id,
            session_id=session_id,
            run_id=run["id"],
            duration_seconds=round(time.monotonic() - started_at, 3),
        )
    except Exception as exc:
        workspace.logger.exception("Agent run %s failed", run["id"])
        sessions_service.finish_task_run(run["id"], "failed", str(exc)[:1000])
        raise HTTPException(status_code=500, detail=f"Agent run failed; reference run ID {run['id']}") from exc
    finally:
        sessions_service.cleanup_run_work(context)


@router.post("/stream")
async def chat_stream(request: ChatRequest, user: CurrentUser = Depends(get_current_user)):
    _, run = await run_in_threadpool(prepare_run, request, user)
    context: RunContext = run["context"]
    return StreamingResponse(
        engine.stream_agent_events(context, request.message),
        media_type="text/event-stream",
        headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
    )
