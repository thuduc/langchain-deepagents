import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from deep_agents_app.api.dependencies import get_current_user, require_project_admin
from deep_agents_app.domain import CurrentUser
from deep_agents_app.schemas import ContentImportRequest, ProjectCreateRequest, ProjectImportRequest, ProjectUpdateRequest
from deep_agents_app.services import workspace
from deep_agents_app.services import queries


router = APIRouter(prefix="/api", tags=["projects"])


@router.get("/projects")
def list_projects(_: CurrentUser = Depends(get_current_user)):
    return {"projects": queries.list_projects()}


@router.post("/projects")
def create_project(request: ProjectCreateRequest, user: CurrentUser = Depends(require_project_admin)):
    project = workspace.create_project_record(request.name, user.id)
    workspace.validate_project_skills(project["id"])
    workspace.add_audit_event(user.id, "project.create", project["id"], {"name": project["name"]})
    return {"project": workspace.public_project(project)}


@router.put("/projects/{project_id}")
def update_project(project_id: str, request: ProjectUpdateRequest, user: CurrentUser = Depends(require_project_admin)):
    workspace.get_project(project_id)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name cannot be empty")
    project = queries.rename_project(project_id, name)
    workspace.invalidate_project_agent(project_id)
    workspace.add_audit_event(user.id, "project.update", project_id, {"name": name})
    return {"project": project}


@router.get("/projects/{project_id}")
def get_project(project_id: str, _: CurrentUser = Depends(get_current_user)):
    project = workspace.get_project(project_id)
    project["skills"] = workspace.scan_project_skills(project_id)
    return {"project": workspace.public_project(project)}


@router.get("/projects/{project_id}/isolation")
def project_isolation(project_id: str, user: CurrentUser = Depends(require_project_admin)):
    return {"audit": workspace.project_isolation_audit(user.id, project_id)}


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, user: CurrentUser = Depends(require_project_admin)):
    project = workspace.get_project(project_id)
    project_root = workspace.ensure_child_path(workspace.get_projects_dir(), Path(project["path"]))
    workspace.add_audit_event(user.id, "project.delete.requested", project_id, {"name": project["name"]})
    workspace.delete_project_resources(project_id, project_root)
    return {"deleted": True}


@router.get("/projects/{project_id}/contents")
def project_contents(project_id: str, _: CurrentUser = Depends(get_current_user)):
    return {"items": workspace.list_project_files(workspace.get_project_root(project_id))}


@router.delete("/projects/{project_id}/contents")
def delete_project_contents(project_id: str, user: CurrentUser = Depends(require_project_admin)):
    workspace.ensure_no_active_project_runs(project_id)
    project_root = workspace.get_project_root(project_id)
    workspace.reset_project_contents_transactional(project_root)
    workspace.touch_project(project_id, bump_revision=True)
    workspace.invalidate_project_agent(project_id)
    workspace.add_audit_event(user.id, "project.contents.delete", project_id)
    return {"deleted": True}


@router.post("/uploads/preview")
async def upload_preview(file: UploadFile = File(...), user: CurrentUser = Depends(require_project_admin)):
    token = uuid.uuid4().hex
    user_dir = workspace.ensure_child_path(workspace.TMP_UPLOADS_DIR, workspace.TMP_UPLOADS_DIR / user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    zip_path = workspace.ensure_child_path(user_dir, user_dir / f"{uuid.uuid4().hex}.zip")
    total = 0
    try:
        with zip_path.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > 250 * 1024 * 1024:
                    raise HTTPException(status_code=400, detail="ZIP upload exceeds 250MB limit")
                destination.write(chunk)
        preview = workspace.inspect_zip(zip_path)
        workspace.record_upload_preview(user.id, token, zip_path)
    except Exception:
        zip_path.unlink(missing_ok=True)
        raise
    return {"upload_token": token, **preview}


@router.post("/projects/import")
def import_project(request: ProjectImportRequest, user: CurrentUser = Depends(require_project_admin)):
    zip_path = workspace.upload_path_for_token(user.id, request.upload_token, consume=True)
    project: Optional[Dict[str, Any]] = None
    try:
        project = workspace.create_project_record(request.name, user.id)
        workspace.extract_zip(zip_path, Path(project["path"]), request.mode)
        workspace.touch_project(project["id"], bump_revision=True)
        workspace.validate_project_skills(project["id"])
        workspace.invalidate_project_agent(project["id"])
        workspace.add_audit_event(user.id, "project.import", project["id"], {"mode": request.mode})
        return {"project": workspace.public_project(workspace.get_project(project["id"]))}
    except Exception:
        if project:
            project_path = Path(project["path"])
            if project_path.exists():
                shutil.rmtree(project_path)
            with workspace.get_db_connection() as conn:
                conn.execute("DELETE FROM projects WHERE id = ?", (project["id"],))
        raise
    finally:
        workspace.cleanup_upload_preview(request.upload_token, zip_path)


@router.post("/projects/{project_id}/contents/import")
def import_project_contents(project_id: str, request: ContentImportRequest, user: CurrentUser = Depends(require_project_admin)):
    workspace.ensure_no_active_project_runs(project_id)
    project_root = workspace.get_project_root(project_id)
    zip_path = workspace.upload_path_for_token(user.id, request.upload_token, consume=True)
    try:
        workspace.extract_zip(zip_path, project_root, request.mode)
        workspace.touch_project(project_id, bump_revision=True)
        workspace.validate_project_skills(project_id)
        workspace.invalidate_project_agent(project_id)
        workspace.add_audit_event(user.id, "project.contents.import", project_id, {"mode": request.mode})
        return {"project": workspace.public_project(workspace.get_project(project_id)), "items": workspace.list_project_files(project_root)}
    finally:
        workspace.cleanup_upload_preview(request.upload_token, zip_path)


@router.get("/projects/{project_id}/media/{media_path:path}")
def project_media(project_id: str, media_path: str, _: CurrentUser = Depends(get_current_user)):
    project_root = workspace.get_project_root(project_id)
    file_path = workspace.relative_project_path(project_root, media_path)
    if file_path.suffix.lower() not in workspace.MEDIA_EXTENSIONS or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Project media not found")
    return FileResponse(file_path)
