"""Safe project file browsing and format-aware preview helpers."""

from __future__ import annotations

import codecs
import csv
import mimetypes
import os
import re
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import HTTPException

from deep_agents_app.services import workspace


TEXT_PREVIEW_MAX_BYTES = 512 * 1024
CSV_PREVIEW_MAX_ROWS = 200
CSV_PREVIEW_MAX_COLUMNS = 100
CSV_PREVIEW_MAX_CELL_CHARS = 2_000
DIRECTORY_ENTRY_LIMIT = 5_000
SEARCH_RESULT_LIMIT = 200
PROJECT_FILE_UPLOAD_LIMIT = 250 * 1024 * 1024
RESERVED_ROOT_DIRECTORIES = {"data", "skills"}
PORTABLE_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdx"}
CSV_EXTENSIONS = {".csv", ".tsv"}
IMAGE_EXTENSIONS = {".avif", ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".webp"}
PDF_EXTENSIONS = {".pdf"}
VIDEO_EXTENSIONS = {".m4v", ".mov", ".mp4", ".ogv", ".webm"}
AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".wav"}

CODE_LANGUAGES = {
    ".bash": "bash",
    ".c": "c",
    ".cc": "cpp",
    ".cfg": "ini",
    ".conf": "ini",
    ".cpp": "cpp",
    ".css": "css",
    ".diff": "diff",
    ".dockerfile": "dockerfile",
    ".env": "ini",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".htm": "xml",
    ".html": "xml",
    ".ini": "ini",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".patch": "diff",
    ".properties": "ini",
    ".py": "python",
    ".r": "r",
    ".rb": "ruby",
    ".rs": "rust",
    ".scss": "css",
    ".sh": "bash",
    ".sql": "sql",
    ".svg": "xml",
    ".toml": "ini",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".zsh": "bash",
}

TEXT_EXTENSIONS = {
    ".gitignore",
    ".log",
    ".rst",
    ".text",
    ".txt",
}


def _visible_parts(user_path: str) -> tuple[str, ...]:
    normalized = (user_path or "").replace("\\", "/").strip("/")
    if not normalized or normalized == ".":
        return ()
    parts = tuple(part for part in normalized.split("/") if part)
    if any(part in {".", ".."} or part.startswith(".") for part in parts):
        raise HTTPException(status_code=404, detail="Project file not found")
    return parts


def resolve_project_path(
    project_id: str,
    user_path: str,
    *,
    expected: str | None = None,
) -> tuple[Path, Path]:
    """Resolve a visible, non-symlink project path without exposing host paths."""
    root = workspace.get_project_root(project_id)
    parts = _visible_parts(user_path)
    unresolved = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise HTTPException(status_code=404, detail="Project file not found")
    path = workspace.ensure_child_path(root, unresolved)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Project file not found")
    if expected == "file" and not path.is_file():
        raise HTTPException(status_code=404, detail="Project file not found")
    if expected == "directory" and not path.is_dir():
        raise HTTPException(status_code=404, detail="Project folder not found")
    return root, path


def _validate_entry_name(name: str) -> str:
    if not name or name != name.strip() or len(name) > 255:
        raise HTTPException(status_code=400, detail="File and folder names must be 1 to 255 characters without surrounding spaces")
    if name in {".", ".."} or name.startswith(".") or name.endswith((".", " ")):
        raise HTTPException(status_code=400, detail="Hidden or ambiguous file and folder names are not allowed")
    if PORTABLE_INVALID_NAME.search(name):
        raise HTTPException(status_code=400, detail="The file or folder name contains unsupported characters")
    return name


def _new_child_path(project_id: str, parent_path: str, name: str) -> tuple[Path, Path, Path]:
    root, parent = resolve_project_path(project_id, parent_path, expected="directory")
    validated_name = _validate_entry_name(name)
    target = workspace.ensure_child_path(root, parent / validated_name)
    return root, parent, target


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _affects_skills(root: Path, path: Path) -> bool:
    parts = path.relative_to(root).parts
    return bool(parts and parts[0] == "skills")


def _validate_skill_candidate(root: Path, target: Path, staged: Path) -> None:
    parts = target.relative_to(root).parts
    if len(parts) != 3 or parts[0] != "skills" or parts[2] != "SKILL.md":
        return
    metadata = workspace.parse_skill_frontmatter(staged)
    skill_name = parts[1]
    if not metadata.get("name") or not metadata.get("description"):
        raise HTTPException(status_code=422, detail="SKILL.md requires name and description frontmatter")
    if metadata["name"] != skill_name:
        raise HTTPException(status_code=422, detail=f"SKILL.md name must match its folder: {skill_name}")
    if not SKILL_NAME.fullmatch(metadata["name"]):
        raise HTTPException(status_code=422, detail="Skill names must use lowercase alphanumeric words separated by hyphens")


def _commit_mutation(
    project_id: str,
    user_id: str,
    action: str,
    details: Dict[str, Any],
    *,
    skills_changed: bool,
) -> Dict[str, Any]:
    workspace.record_project_content_mutation(user_id, project_id, action, details)
    if skills_changed:
        workspace.validate_project_skills(project_id)
        workspace.invalidate_project_agent(project_id)
    return workspace.public_project(workspace.get_project(project_id))


def create_folder(project_id: str, parent_path: str, name: str, user_id: str) -> Dict[str, Any]:
    with workspace.project_content_lock(project_id):
        workspace.ensure_no_active_project_runs(project_id)
        root, parent, target = _new_child_path(project_id, parent_path, name)
        if parent == root / "skills" and not SKILL_NAME.fullmatch(target.name):
            raise HTTPException(status_code=422, detail="Top-level skill folders must use lowercase alphanumeric words separated by hyphens")
        if target.exists():
            raise HTTPException(status_code=409, detail="A file or folder with this name already exists")
        target.mkdir()
        relative = _relative_path(root, target)
        try:
            project = _commit_mutation(
                project_id,
                user_id,
                "project.folder.create",
                {"path": relative},
                skills_changed=_affects_skills(root, target),
            )
        except Exception:
            target.rmdir()
            raise
        return {"project": project, "item": file_item(root, target), "parent_path": _relative_path(root, parent) if parent != root else ""}


def add_file_from_staged(
    project_id: str,
    parent_path: str,
    file_name: str,
    staged_upload: Path,
    user_id: str,
) -> Dict[str, Any]:
    with workspace.project_content_lock(project_id):
        workspace.ensure_no_active_project_runs(project_id)
        root, parent, target = _new_child_path(project_id, parent_path, file_name)
        if target.exists():
            raise HTTPException(status_code=409, detail="A file with this name already exists; use Replace instead")
        publish_stage = target.parent / f".{target.name}.upload-{uuid.uuid4().hex}"
        try:
            shutil.copyfile(staged_upload, publish_stage)
            _validate_skill_candidate(root, target, publish_stage)
            os.replace(publish_stage, target)
            relative = _relative_path(root, target)
            try:
                project = _commit_mutation(
                    project_id,
                    user_id,
                    "project.file.create",
                    {"path": relative, "size": target.stat().st_size},
                    skills_changed=_affects_skills(root, target),
                )
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return {"project": project, "item": file_item(root, target), "parent_path": _relative_path(root, parent) if parent != root else ""}
        finally:
            publish_stage.unlink(missing_ok=True)


def replace_file_from_staged(
    project_id: str,
    user_path: str,
    upload_name: str,
    staged_upload: Path,
    user_id: str,
) -> Dict[str, Any]:
    with workspace.project_content_lock(project_id):
        workspace.ensure_no_active_project_runs(project_id)
        root, target = resolve_project_path(project_id, user_path, expected="file")
        _validate_entry_name(upload_name)
        if Path(upload_name).suffix.lower() != target.suffix.lower():
            raise HTTPException(status_code=400, detail="The replacement file must have the same extension")
        publish_stage = target.parent / f".{target.name}.upload-{uuid.uuid4().hex}"
        backup = target.parent / f".{target.name}.previous-{uuid.uuid4().hex}"
        previous_size = target.stat().st_size
        try:
            shutil.copyfile(staged_upload, publish_stage)
            _validate_skill_candidate(root, target, publish_stage)
            os.replace(target, backup)
            os.replace(publish_stage, target)
            relative = _relative_path(root, target)
            try:
                project = _commit_mutation(
                    project_id,
                    user_id,
                    "project.file.replace",
                    {"path": relative, "previous_size": previous_size, "size": target.stat().st_size},
                    skills_changed=_affects_skills(root, target),
                )
            except Exception:
                target.unlink(missing_ok=True)
                os.replace(backup, target)
                raise
            backup.unlink(missing_ok=True)
            return {"project": project, "item": file_item(root, target), "parent_path": _relative_path(root, target.parent) if target.parent != root else ""}
        except Exception:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        finally:
            publish_stage.unlink(missing_ok=True)
            if backup.exists() and target.exists():
                backup.unlink(missing_ok=True)


def entry_info(project_id: str, user_path: str) -> Dict[str, Any]:
    root, target = resolve_project_path(project_id, user_path)
    if target == root:
        raise HTTPException(status_code=400, detail="The project root cannot be changed")
    if target.is_file():
        return {**file_item(root, target), "descendant_count": 1, "total_size": target.stat().st_size}
    count = 0
    total_size = 0
    for current_root, directory_names, file_names in os.walk(target, followlinks=False):
        current = Path(current_root)
        directory_names[:] = [name for name in directory_names if not (current / name).is_symlink()]
        for name in file_names:
            path = current / name
            if path.is_symlink():
                continue
            count += 1
            try:
                total_size += path.stat().st_size
            except OSError:
                pass
    return {**file_item(root, target), "descendant_count": count, "total_size": total_size}


def content_summary(project_id: str) -> Dict[str, Any]:
    root = workspace.get_project_root(project_id)
    file_count = 0
    folder_count = 0
    total_size = 0
    has_content = False
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not name.startswith(".") and not (current / name).is_symlink()
        )
        for name in directory_names:
            folder_count += 1
            relative = (current / name).relative_to(root)
            if not (len(relative.parts) == 1 and relative.name in RESERVED_ROOT_DIRECTORIES):
                has_content = True
        for name in sorted(file_names):
            path = current / name
            if name.startswith(".") or path.is_symlink():
                continue
            file_count += 1
            has_content = True
            try:
                total_size += path.stat().st_size
            except OSError:
                pass
    return {
        "has_content": has_content,
        "file_count": file_count,
        "folder_count": folder_count,
        "total_size": total_size,
    }


def export_project_archive(project_id: str, user_id: str) -> tuple[Path, str]:
    with workspace.project_content_lock(project_id):
        project = workspace.get_project(project_id)
        root = workspace.get_project_root(project_id)
        export_dir = workspace.ensure_child_path(
            workspace.TMP_UPLOADS_DIR,
            workspace.TMP_UPLOADS_DIR / user_id / "project-exports",
        )
        export_dir.mkdir(parents=True, exist_ok=True)
        archive_path = workspace.ensure_child_path(
            export_dir,
            export_dir / f"{project['slug']}-{uuid.uuid4().hex}.zip",
        )
        try:
            with zipfile.ZipFile(
                archive_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as archive:
                for current_root, directory_names, file_names in os.walk(root, followlinks=False):
                    current = Path(current_root)
                    directory_names[:] = sorted(
                        name
                        for name in directory_names
                        if not name.startswith(".") and not (current / name).is_symlink()
                    )
                    if current != root:
                        archive.writestr(f"{current.relative_to(root).as_posix()}/", b"")
                    for name in sorted(file_names):
                        path = current / name
                        if name.startswith(".") or path.is_symlink():
                            continue
                        archive.write(path, arcname=path.relative_to(root).as_posix())
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise
        return archive_path, f"{project['slug']}.zip"


def delete_entry(project_id: str, user_path: str, user_id: str) -> Dict[str, Any]:
    with workspace.project_content_lock(project_id):
        workspace.ensure_no_active_project_runs(project_id)
        root, target = resolve_project_path(project_id, user_path)
        relative = _relative_path(root, target)
        if target == root or relative in RESERVED_ROOT_DIRECTORIES:
            raise HTTPException(status_code=400, detail="The project root, data folder, and skills folder cannot be deleted")
        info = entry_info(project_id, user_path)
        backup = target.parent / f".{target.name}.deleted-{uuid.uuid4().hex}"
        os.replace(target, backup)
        try:
            project = _commit_mutation(
                project_id,
                user_id,
                "project.entry.delete",
                {
                    "path": relative,
                    "type": info["type"],
                    "descendant_count": info["descendant_count"],
                    "total_size": info["total_size"],
                },
                skills_changed=_affects_skills(root, target),
            )
        except Exception:
            os.replace(backup, target)
            raise
        if backup.is_dir():
            shutil.rmtree(backup, ignore_errors=True)
        else:
            backup.unlink(missing_ok=True)
        return {"deleted": True, "project": project, "path": relative, "parent_path": _relative_path(root, target.parent) if target.parent != root else ""}


def _updated_at(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _has_visible_children(path: Path) -> bool:
    try:
        return any(not child.name.startswith(".") and not child.is_symlink() for child in path.iterdir())
    except OSError:
        return False


def file_item(root: Path, path: Path) -> Dict[str, Any]:
    is_directory = path.is_dir()
    result: Dict[str, Any] = {
        "path": path.relative_to(root).as_posix(),
        "name": path.name,
        "type": "directory" if is_directory else "file",
        "size": 0 if is_directory else path.stat().st_size,
        "updated_at": _updated_at(path),
    }
    if is_directory:
        result["has_children"] = _has_visible_children(path)
    else:
        result["mime_type"] = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return result


def list_directory(project_id: str, user_path: str) -> Dict[str, Any]:
    root, directory = resolve_project_path(project_id, user_path, expected="directory")
    try:
        children = [
            child
            for child in directory.iterdir()
            if not child.name.startswith(".") and not child.is_symlink()
        ]
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Project folder cannot be read") from exc
    children.sort(key=lambda child: (not child.is_dir(), child.name.casefold()))
    truncated = len(children) > DIRECTORY_ENTRY_LIMIT
    return {
        "path": directory.relative_to(root).as_posix() if directory != root else "",
        "items": [file_item(root, child) for child in children[:DIRECTORY_ENTRY_LIMIT]],
        "truncated": truncated,
    }


def search_files(project_id: str, query: str) -> Dict[str, Any]:
    root = workspace.get_project_root(project_id)
    needle = query.strip().casefold()
    if len(needle) < 2:
        return {"items": [], "truncated": False}

    matches: List[Dict[str, Any]] = []
    truncated = False
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        directory_names[:] = sorted(
            [
                name
                for name in directory_names
                if not name.startswith(".") and not (current / name).is_symlink()
            ],
            key=str.casefold,
        )
        for name in sorted(file_names, key=str.casefold):
            path = current / name
            if name.startswith(".") or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if needle not in relative.casefold():
                continue
            if len(matches) >= SEARCH_RESULT_LIMIT:
                truncated = True
                break
            matches.append(file_item(root, path))
        if truncated:
            break
    return {"items": matches, "truncated": truncated}


def _preview_kind(path: Path, mime_type: str) -> tuple[str, str | None]:
    suffix = path.suffix.lower()
    lower_name = path.name.lower()
    if suffix in MARKDOWN_EXTENSIONS:
        return "markdown", "markdown"
    if suffix in CSV_EXTENSIONS:
        return "csv", None
    if lower_name == "dockerfile":
        return "code", "dockerfile"
    if suffix in CODE_LANGUAGES:
        return "code", CODE_LANGUAGES[suffix]
    if suffix in IMAGE_EXTENSIONS:
        return "image", None
    if suffix in PDF_EXTENSIONS:
        return "pdf", None
    if suffix in VIDEO_EXTENSIONS:
        return "video", None
    if suffix in AUDIO_EXTENSIONS:
        return "audio", None
    if suffix in TEXT_EXTENSIONS or mime_type.startswith("text/"):
        return "text", "plaintext"
    return "unsupported", None


def _read_text_preview(path: Path) -> tuple[str, bool]:
    with path.open("rb") as handle:
        raw = handle.read(TEXT_PREVIEW_MAX_BYTES + 1)
    truncated = len(raw) > TEXT_PREVIEW_MAX_BYTES
    raw = raw[:TEXT_PREVIEW_MAX_BYTES]
    if b"\x00" in raw[:8192]:
        raise UnicodeError("Binary content")
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    return decoder.decode(raw, final=not truncated), truncated


def _csv_preview(path: Path) -> Dict[str, Any]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    rows: List[List[str]] = []
    truncated = False
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            first = next(reader, None)
            if first is None:
                return {"columns": [], "rows": [], "truncated": False}
            column_count = min(len(first), CSV_PREVIEW_MAX_COLUMNS)
            columns = [
                (value or f"Column {index + 1}")[:CSV_PREVIEW_MAX_CELL_CHARS]
                for index, value in enumerate(first[:column_count])
            ]
            truncated = len(first) > column_count
            for index, row in enumerate(reader):
                if index >= CSV_PREVIEW_MAX_ROWS:
                    truncated = True
                    break
                normalized = [value[:CSV_PREVIEW_MAX_CELL_CHARS] for value in row[:column_count]]
                normalized.extend([""] * (column_count - len(normalized)))
                if len(row) > column_count or any(len(value) > CSV_PREVIEW_MAX_CELL_CHARS for value in row):
                    truncated = True
                rows.append(normalized)
    except (UnicodeDecodeError, csv.Error, OSError) as exc:
        raise HTTPException(status_code=422, detail="This data file cannot be previewed as UTF-8 CSV") from exc
    return {"columns": columns, "rows": rows, "truncated": truncated}


def preview_file(project_id: str, user_path: str) -> Dict[str, Any]:
    root, path = resolve_project_path(project_id, user_path, expected="file")
    item = file_item(root, path)
    mime_type = item["mime_type"]
    kind, language = _preview_kind(path, mime_type)
    preview: Dict[str, Any] = {**item, "kind": kind, "language": language}

    if kind == "csv":
        preview.update(_csv_preview(path))
        return preview
    if kind in {"markdown", "code", "text"}:
        try:
            content, truncated = _read_text_preview(path)
        except (UnicodeDecodeError, UnicodeError, OSError):
            preview.update(
                kind="unsupported",
                language=None,
                message="This file is not UTF-8 text and cannot be previewed safely.",
            )
            return preview
        preview.update(content=content, truncated=truncated)
        return preview
    if kind == "unsupported":
        preview["message"] = "Preview is not available for this file type. You can still download it."
    return preview


def inline_file(project_id: str, user_path: str) -> tuple[Path, str]:
    _, path = resolve_project_path(project_id, user_path, expected="file")
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    kind, _ = _preview_kind(path, mime_type)
    if kind not in {"image", "pdf", "video", "audio"}:
        raise HTTPException(status_code=404, detail="Inline preview is not available")
    return path, mime_type


def download_file(project_id: str, user_path: str) -> tuple[Path, str]:
    _, path = resolve_project_path(project_id, user_path, expected="file")
    return path, mimetypes.guess_type(path.name)[0] or "application/octet-stream"
