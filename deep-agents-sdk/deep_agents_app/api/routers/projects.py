import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from deep_agents_app.api.dependencies import get_current_user, require_project_admin
from deep_agents_app.domain import CurrentUser
from deep_agents_app.schemas import ContentImportRequest, ProjectCreateRequest, ProjectFolderCreateRequest, ProjectImportRequest, ProjectUpdateRequest
from deep_agents_app.services import project_files, workspace
from deep_agents_app.services import queries


router = APIRouter(prefix="/api", tags=["projects"])


async def _stage_content_upload(file: UploadFile, user_id: str) -> tuple[Path, str]:
    file_name = file.filename or ""
    upload_dir = workspace.ensure_child_path(
        workspace.TMP_UPLOADS_DIR,
        workspace.TMP_UPLOADS_DIR / user_id / "content-edits",
    )
    upload_dir.mkdir(parents=True, exist_ok=True)
    staged = workspace.ensure_child_path(upload_dir, upload_dir / f"{uuid.uuid4().hex}.upload")
    total = 0
    try:
        with staged.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > project_files.PROJECT_FILE_UPLOAD_LIMIT:
                    raise HTTPException(status_code=400, detail="File upload exceeds the 250MB limit")
                destination.write(chunk)
        return staged, file_name
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


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


@router.get("/projects/{project_id}/directory")
def project_directory(
    project_id: str,
    path: str = "",
    _: CurrentUser = Depends(get_current_user),
):
    return project_files.list_directory(project_id, path)


@router.get("/projects/{project_id}/file-search")
def project_file_search(
    project_id: str,
    query: str = "",
    _: CurrentUser = Depends(get_current_user),
):
    return project_files.search_files(project_id, query)


@router.get("/projects/{project_id}/file-preview")
def project_file_preview(
    project_id: str,
    path: str,
    _: CurrentUser = Depends(get_current_user),
):
    return project_files.preview_file(project_id, path)


@router.get("/projects/{project_id}/file-inline")
def project_file_inline(
    project_id: str,
    path: str,
    _: CurrentUser = Depends(get_current_user),
):
    file_path, media_type = project_files.inline_file(project_id, path)
    return FileResponse(
        file_path,
        media_type=media_type,
        filename=file_path.name,
        content_disposition_type="inline",
    )


@router.get("/projects/{project_id}/file-download")
def project_file_download(
    project_id: str,
    path: str,
    _: CurrentUser = Depends(get_current_user),
):
    file_path, media_type = project_files.download_file(project_id, path)
    return FileResponse(
        file_path,
        media_type=media_type,
        filename=file_path.name,
        content_disposition_type="attachment",
    )


@router.get("/projects/{project_id}/entry-info")
def project_entry_info(
    project_id: str,
    path: str,
    _: CurrentUser = Depends(get_current_user),
):
    return project_files.entry_info(project_id, path)


@router.get("/projects/{project_id}/content-summary")
def project_content_summary(
    project_id: str,
    _: CurrentUser = Depends(get_current_user),
):
    return project_files.content_summary(project_id)


@router.get("/projects/{project_id}/export")
def export_project(
    project_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    archive_path, download_name = project_files.export_project_archive(project_id, user.id)
    try:
        workspace.add_audit_event(
            user.id,
            "project.contents.export",
            project_id,
            {"file_name": download_name},
        )
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=download_name,
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@router.post("/projects/{project_id}/folders")
def create_project_folder(
    project_id: str,
    request: ProjectFolderCreateRequest,
    user: CurrentUser = Depends(require_project_admin),
):
    return project_files.create_folder(project_id, request.parent_path, request.name, user.id)


@router.post("/projects/{project_id}/files")
async def add_project_file(
    project_id: str,
    file: UploadFile = File(...),
    parent_path: str = "",
    user: CurrentUser = Depends(require_project_admin),
):
    staged, file_name = await _stage_content_upload(file, user.id)
    try:
        return await run_in_threadpool(
            project_files.add_file_from_staged,
            project_id,
            parent_path,
            file_name,
            staged,
            user.id,
        )
    finally:
        staged.unlink(missing_ok=True)


@router.put("/projects/{project_id}/files")
async def replace_project_file(
    project_id: str,
    path: str,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_project_admin),
):
    staged, file_name = await _stage_content_upload(file, user.id)
    try:
        return await run_in_threadpool(
            project_files.replace_file_from_staged,
            project_id,
            path,
            file_name,
            staged,
            user.id,
        )
    finally:
        staged.unlink(missing_ok=True)


@router.delete("/projects/{project_id}/entries")
def delete_project_entry(
    project_id: str,
    path: str,
    user: CurrentUser = Depends(require_project_admin),
):
    return project_files.delete_entry(project_id, path, user.id)


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
    with workspace.project_content_lock(project_id):
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
