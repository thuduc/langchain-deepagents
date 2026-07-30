import hashlib
import re
import shutil
import sqlite3
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List

from fastapi import HTTPException

from deep_agents_app.services import workspace as state
from deep_agents_app.services.workspace import (
    bounded_env_int,
    ensure_child_path,
    get_db_connection,
    get_projects_dir,
    logger,
    utc_now,
)


STAGING_SUBDIRECTORIES = ("content-edits", "project-exports")
STAGING_MAX_AGE_SECONDS = 60 * 60


def upload_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def prune_stale_upload_staging(max_age_seconds: int = STAGING_MAX_AGE_SECONDS) -> None:
    """Reclaim staged uploads and export archives left by interrupted requests.

    Both are normally removed by the request that created them, but a client
    disconnect can skip that cleanup. Neither is recorded in ``upload_previews``,
    so without this sweep nothing would ever reclaim the space.
    """
    root = state.TMP_UPLOADS_DIR
    if not root.exists():
        return
    cutoff = time.time() - max_age_seconds
    for user_dir in root.iterdir():
        if user_dir.is_symlink() or not user_dir.is_dir():
            continue
        for staging_name in STAGING_SUBDIRECTORIES:
            staging_dir = user_dir / staging_name
            if staging_dir.is_symlink() or not staging_dir.is_dir():
                continue
            for path in staging_dir.iterdir():
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    if path.stat().st_mtime > cutoff:
                        continue
                    ensure_child_path(root, path).unlink(missing_ok=True)
                except (OSError, HTTPException) as exc:
                    logger.warning("Could not remove stale staged file %s: %s", path, exc)


def prune_expired_uploads() -> None:
    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                "SELECT token_hash, storage_key FROM upload_previews WHERE expires_at <= ?",
                (utc_now(),),
            ).fetchall()
            conn.executemany(
                "DELETE FROM upload_previews WHERE token_hash = ?",
                [(row["token_hash"],) for row in rows],
            )
        for row in rows:
            try:
                path = ensure_child_path(
                    state.TMP_UPLOADS_DIR, state.TMP_UPLOADS_DIR / row["storage_key"]
                )
                path.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("Could not remove expired upload preview: %s", exc)
    except sqlite3.OperationalError:
        return


def upload_path_for_token(user_id: str, token: str, consume: bool = False) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise HTTPException(status_code=400, detail="Invalid upload token")
    token_hash = upload_token_hash(token)
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM upload_previews
            WHERE token_hash = ? AND user_id = ? AND consumed_at IS NULL
            """,
            (token_hash, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Upload preview expired or not found")
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            conn.execute("DELETE FROM upload_previews WHERE token_hash = ?", (token_hash,))
            stale_path = ensure_child_path(state.TMP_UPLOADS_DIR, state.TMP_UPLOADS_DIR / row["storage_key"])
            stale_path.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail="Upload preview expired or not found")
        if consume:
            conn.execute(
                "UPDATE upload_previews SET consumed_at = ? WHERE token_hash = ?",
                (utc_now(), token_hash),
            )
    path = ensure_child_path(state.TMP_UPLOADS_DIR, state.TMP_UPLOADS_DIR / row["storage_key"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Upload preview expired or not found")
    return path


def record_upload_preview(user_id: str, token: str, zip_path: Path) -> None:
    ttl_minutes = bounded_env_int("UPLOAD_PREVIEW_TTL_MINUTES", 15, 1, 1440)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    storage_key = zip_path.resolve().relative_to(state.TMP_UPLOADS_DIR.resolve()).as_posix()
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO upload_previews (
                token_hash, user_id, storage_key, expires_at, consumed_at, created_at
            ) VALUES (?, ?, ?, ?, NULL, ?)
            """,
            (upload_token_hash(token), user_id, storage_key, expires_at.isoformat(), utc_now()),
        )


def is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return (mode & 0o170000) == 0o120000


def inspect_zip(zip_path: Path) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    total_size = 0

    try:
        with zipfile.ZipFile(zip_path) as archive:
            if len(archive.infolist()) > 10_000:
                raise HTTPException(status_code=400, detail="ZIP contains too many entries")
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                if not name or name.startswith("__MACOSX/"):
                    continue

                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unsafe ZIP path rejected: {info.filename}",
                    )
                if is_zip_symlink(info):
                    raise HTTPException(
                        status_code=400,
                        detail=f"ZIP symlink rejected: {info.filename}",
                    )

                total_size += info.file_size
                entries.append(
                    {
                        "path": pure.as_posix(),
                        "type": "directory" if info.is_dir() else "file",
                        "size": info.file_size,
                    }
                )
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid ZIP") from exc

    if total_size > 1024 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="ZIP expands beyond 1GB limit")

    return {"entries": entries[:1000], "total_size": total_size, "entry_count": len(entries)}


def extract_zip_contents(zip_path: Path, target_dir: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.startswith("__MACOSX/"):
                continue
            rel = PurePosixPath(name).as_posix()
            destination = ensure_child_path(target_dir, target_dir / rel)
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                destination.unlink()
            with archive.open(info) as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def extract_zip(zip_path: Path, target_dir: Path, mode: str) -> None:
    if mode not in {"merge", "replace"}:
        raise HTTPException(status_code=400, detail="Import mode must be 'merge' or 'replace'")

    inspect_zip(zip_path)
    target_dir = ensure_child_path(get_projects_dir(), target_dir)
    staging_dir = ensure_child_path(
        get_projects_dir(), target_dir.parent / f".{target_dir.name}.staging-{uuid.uuid4().hex}"
    )
    previous_dir = ensure_child_path(
        get_projects_dir(), target_dir.parent / f".{target_dir.name}.previous-{uuid.uuid4().hex}"
    )
    try:
        if mode == "merge" and target_dir.exists():
            shutil.copytree(target_dir, staging_dir)
        else:
            staging_dir.mkdir(parents=True)
        extract_zip_contents(zip_path, staging_dir)

        if target_dir.exists():
            target_dir.rename(previous_dir)
        staging_dir.rename(target_dir)
        if previous_dir.exists():
            shutil.rmtree(previous_dir)
    except Exception:
        if target_dir.exists() and previous_dir.exists():
            shutil.rmtree(target_dir)
        if previous_dir.exists() and not target_dir.exists():
            previous_dir.rename(target_dir)
        raise
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)

def cleanup_upload_preview(token: str, zip_path: Path) -> None:
    zip_path.unlink(missing_ok=True)
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM upload_previews WHERE token_hash = ?",
            (upload_token_hash(token),),
        )
