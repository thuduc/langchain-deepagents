"""Chat session endpoints and the authenticated artifact download route."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from deep_agents_app.api.dependencies import get_current_user
from deep_agents_app.domain import CurrentUser
from deep_agents_app.schemas import SessionCreateRequest
from deep_agents_app.services import artifact_store, queries, workspace
from deep_agents_app.services.artifact_store import ArtifactStoreError


router = APIRouter(prefix="/api", tags=["sessions"])


def sanitize_filename(name: str) -> str:
    """Keep a header value from carrying quotes or newlines into the response."""
    cleaned = "".join(char for char in name if char.isprintable() and char not in '"\\')
    return cleaned.replace("\r", "").replace("\n", "").strip() or "artifact"


@router.get("/projects/{project_id}/sessions")
def list_sessions(project_id: str, user: CurrentUser = Depends(get_current_user)):
    """The caller's conversations in this project, most recent first."""
    workspace.get_project(project_id)
    return {"sessions": workspace.list_project_sessions(user.id, project_id)}


@router.post("/projects/{project_id}/sessions")
def create_session(project_id: str, request: SessionCreateRequest, user: CurrentUser = Depends(get_current_user)):
    """Start a new conversation."""
    session = workspace.create_session(user.id, project_id, request.title)
    return {"session": session, "sessions": workspace.list_project_sessions(user.id, project_id)}


@router.get("/projects/{project_id}/sessions/{session_id}")
def get_session(project_id: str, session_id: str, user: CurrentUser = Depends(get_current_user)):
    """One conversation with its messages."""
    return queries.session_detail(user.id, project_id, session_id)


@router.get("/projects/{project_id}/sessions/{session_id}/runs")
def list_session_runs(project_id: str, session_id: str, user: CurrentUser = Depends(get_current_user)):
    """Run history for a conversation."""
    return {"runs": queries.session_runs(user.id, project_id, session_id)}


@router.delete("/projects/{project_id}/sessions/{session_id}")
def delete_session(project_id: str, session_id: str, user: CurrentUser = Depends(get_current_user)):
    """Delete a conversation and the artifacts it produced."""
    session = workspace.get_session(user.id, project_id, session_id)
    workspace.delete_session_resources(user.id, project_id, session_id, session["thread_id"])
    return {"deleted": True}


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, user: CurrentUser = Depends(get_current_user)):
    """Download one artifact, if the caller owns it.

    Ownership is enforced by the query rather than a separate check, so another
    user's artifact id is indistinguishable from one that does not exist.
    """
    row = queries.owned_artifact(artifact_id, user.id)
    if not row:
        raise HTTPException(status_code=404, detail="Artifact not found")
    # Streamed rather than redirected to a presigned URL: a presigned URL is a
    # bearer token that would bypass the CDX boundary this check enforces.
    try:
        stream = artifact_store.get_store().open_stream(row["storage_key"])
    except (FileNotFoundError, ArtifactStoreError):
        raise HTTPException(status_code=404, detail="Artifact file not found") from None
    return StreamingResponse(
        stream,
        media_type=row["media_type"],
        headers={
            "Content-Disposition": (
                f'attachment; filename="{sanitize_filename(row["display_name"])}"'
            ),
            "Content-Length": str(row["size"]),
        },
    )
