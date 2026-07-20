from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from deep_agents_app.api.dependencies import get_current_user
from deep_agents_app.domain import CurrentUser
from deep_agents_app.schemas import SessionCreateRequest
from deep_agents_app.services import workspace
from deep_agents_app.services import queries


router = APIRouter(prefix="/api", tags=["sessions"])


@router.get("/projects/{project_id}/sessions")
def list_sessions(project_id: str, user: CurrentUser = Depends(get_current_user)):
    workspace.get_project(project_id)
    return {"sessions": workspace.list_project_sessions(user.id, project_id)}


@router.post("/projects/{project_id}/sessions")
def create_session(project_id: str, request: SessionCreateRequest, user: CurrentUser = Depends(get_current_user)):
    session = workspace.create_session(user.id, project_id, request.title)
    return {"session": session, "sessions": workspace.list_project_sessions(user.id, project_id)}


@router.get("/projects/{project_id}/sessions/{session_id}")
def get_session(project_id: str, session_id: str, user: CurrentUser = Depends(get_current_user)):
    return queries.session_detail(user.id, project_id, session_id)


@router.get("/projects/{project_id}/sessions/{session_id}/runs")
def list_session_runs(project_id: str, session_id: str, user: CurrentUser = Depends(get_current_user)):
    return {"runs": queries.session_runs(user.id, project_id, session_id)}


@router.delete("/projects/{project_id}/sessions/{session_id}")
def delete_session(project_id: str, session_id: str, user: CurrentUser = Depends(get_current_user)):
    session = workspace.get_session(user.id, project_id, session_id)
    workspace.delete_session_resources(user.id, project_id, session_id, session["thread_id"])
    return {"deleted": True}


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, user: CurrentUser = Depends(get_current_user)):
    row = queries.owned_artifact(artifact_id, user.id)
    if not row:
        raise HTTPException(status_code=404, detail="Artifact not found")
    file_path = workspace.ensure_child_path(workspace.ARTIFACTS_DIR, workspace.ARTIFACTS_DIR / row["storage_key"])
    if not file_path.is_file() or file_path.is_symlink():
        raise HTTPException(status_code=404, detail="Artifact file not found")
    return FileResponse(file_path, media_type=row["media_type"], filename=row["display_name"])
