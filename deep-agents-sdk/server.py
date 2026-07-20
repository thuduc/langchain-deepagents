import os
import re
import secrets
import uuid
import shutil
import sqlite3
import zipfile
import subprocess
import contextvars
import json
import logging
import hashlib
import mimetypes
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, FrozenSet, Iterator, List, Optional
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent.parent
SDK_DIR = BASE_DIR / "deep-agents-sdk"
STATIC_DIR = SDK_DIR / "static"

if str(SDK_DIR) not in sys.path:
    sys.path.insert(0, str(SDK_DIR))

from auth import (  # noqa: E402
    AuthenticationError,
    PROJECT_ADMIN_ROLE,
    TokenIdentity,
    create_development_token,
    decode_cdx_token,
    decode_development_token,
)
from runtime_config import load_runtime_paths  # noqa: E402

logger = logging.getLogger(__name__)

load_dotenv(BASE_DIR / ".env")


def environment_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DEVELOPMENT_LOGIN_ENABLED = environment_flag("DEEP_AGENTS_DEV_LOGIN_ENABLED")
DEVELOPMENT_IDENTITY_COOKIE = "deep_agents_dev_identity"
DEVELOPMENT_IDENTITY_LIFETIME_SECONDS = 12 * 60 * 60


def _configured_projects_dir() -> Path:
    raw = os.environ.get("PROJECTS_DIR", "projects")
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


PROJECTS_ROOT = _configured_projects_dir()
RUNTIME_PATHS = load_runtime_paths(BASE_DIR, PROJECTS_ROOT, STATIC_DIR)
DB_PATH = RUNTIME_PATHS.app_database
AGENT_CHECKPOINT_DB_PATH = RUNTIME_PATHS.agent_database
WORK_DIR = RUNTIME_PATHS.work_dir
ARTIFACTS_DIR = RUNTIME_PATHS.artifacts_dir
TMP_UPLOADS_DIR = RUNTIME_PATHS.uploads_dir


def load_or_create_development_signing_secret(database_dir: Path) -> str:
    key_path = database_dir / ".development-login.key"
    try:
        existing = key_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing

    generated = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(
            key_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        existing = key_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
        raise RuntimeError("Development login signing key is empty")
    with os.fdopen(descriptor, "w", encoding="utf-8") as key_file:
        key_file.write(generated)
    return generated


DEVELOPMENT_SIGNING_SECRET = (
    load_or_create_development_signing_secret(RUNTIME_PATHS.database_dir)
    if DEVELOPMENT_LOGIN_ENABLED
    else secrets.token_urlsafe(48)
)


@dataclass(frozen=True)
class RunContext:
    user_id: str
    project_id: str
    session_id: str
    run_id: str
    work_directory: Path
    artifact_directory: Path


CURRENT_RUN_CONTEXT: contextvars.ContextVar[Optional[RunContext]] = contextvars.ContextVar(
    "current_run_context",
    default=None,
)

@asynccontextmanager
async def app_lifespan(_: FastAPI):
    init_db()
    recover_interrupted_runs()
    try:
        yield
    finally:
        invalidate_all_agents()


app = FastAPI(title="Deep Agents Project Workspace", lifespan=app_lifespan)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a"}
DOCUMENT_EXTENSIONS = {".pdf"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | DOCUMENT_EXTENSIONS
MEDIA_PATH_RE = re.compile(
    r"(?P<path>`[^`\n]+?\.(?:png|jpe?g|gif|webp|svg|mp4|webm|mov|m4v|mp3|wav|ogg|m4a|pdf)`"
    r"|/[^`'\"\n\r]+?\.(?:png|jpe?g|gif|webp|svg|mp4|webm|mov|m4v|mp3|wav|ogg|m4a|pdf)"
    r"|(?:projects/|deep-agents-sdk/|examples/|data/|skills/|resources/|outputs/|charts/)?"
    r"(?:[\w .-]+/)+[\w .-]+?\.(?:png|jpe?g|gif|webp|svg|mp4|webm|mov|m4v|mp3|wav|ogg|m4a|pdf))",
    re.IGNORECASE,
)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<href>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_MEDIA_SRC_RE = re.compile(r"<(?:img|video|audio)\b[^>]*\bsrc=[\"'](?P<src>[^\"']+)[\"']", re.IGNORECASE)
MEDIA_LABEL_RE = re.compile(
    r"^\s*(?:file|image|chart|plot|graph|figure|video|audio|media|output)?\s*"
    r"(?:saved|created|generated|written|exported)?\s*(?:file|image|chart|plot|graph|figure|video|audio|media|output)?\s*"
    r"(?:at|to|as|path)?\s*:?\s*$",
    re.IGNORECASE,
)
TRAILING_MEDIA_LABEL_RE = re.compile(
    r"\s+(?:file|image|chart|plot|graph|figure|video|audio|media|output)?\s*(?:path|url|link)\s*:?\s*$",
    re.IGNORECASE,
)
DEFAULT_AVAILABLE_MODELS = ["gpt-5.5", "gpt-5.4", "gpt-5.4-codex"]
DEFAULT_MAX_SESSIONS_PER_PROJECT = 5
SETTING_DEFAULT_MODEL = "default_model"
SETTING_MAX_SESSIONS_PER_PROJECT = "max_sessions_per_project"


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)


class ProjectUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1)


class ProjectImportRequest(BaseModel):
    name: str = Field(..., min_length=1)
    upload_token: str = Field(..., min_length=1)
    mode: str = "merge"


class ContentImportRequest(BaseModel):
    upload_token: str = Field(..., min_length=1)
    mode: str = "merge"


class SessionCreateRequest(BaseModel):
    title: Optional[str] = None


class SettingsUpdateRequest(BaseModel):
    default_model: str = Field(..., min_length=1)
    max_sessions_per_project: int = Field(..., ge=1, le=100)


class DevelopmentLoginRequest(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    project_admin: bool = False


class ChatRequest(BaseModel):
    message: str
    project_id: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    project_id: str
    session_id: str
    run_id: str
    duration_seconds: float


@dataclass(frozen=True)
class CurrentUser:
    id: str
    issuer: str
    subject: str
    roles: FrozenSet[str]

    @property
    def is_project_admin(self) -> bool:
        return PROJECT_ADMIN_ROLE in self.roles


SENSITIVE_FILESYSTEM_PATTERNS = [
    "/.env",
    "/.env.*",
    "/**/.env",
    "/**/.env.*",
    "/checkpoints.db",
    "/checkpoints.db-*",
    "/agent_checkpoints.db",
    "/agent_checkpoints.db-*",
]
PROJECT_FILESYSTEM_ALLOW_PATTERNS = ["/**"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def duration_seconds_between(started_at: Optional[str], completed_at: Optional[str]) -> Optional[float]:
    if not started_at or not completed_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
        completed = datetime.fromisoformat(completed_at)
    except ValueError:
        return None
    return round(max(0.0, (completed - started).total_seconds()), 3)


def get_projects_dir() -> Path:
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    return PROJECTS_ROOT.resolve()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or f"project-{uuid.uuid4().hex[:8]}"


class ClosingSQLiteConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH,
        timeout=10,
        check_same_thread=False,
        factory=ClosingSQLiteConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                issuer TEXT NOT NULL,
                subject TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE(issuer, subject)
            );

            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                relative_path TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'deleting')),
                content_revision INTEGER NOT NULL DEFAULT 1,
                created_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                thread_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS task_runs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                project_revision INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'cancelled')),
                latest_status TEXT NOT NULL DEFAULT 'Preparing the agent workspace…',
                prompt TEXT NOT NULL,
                error_summary TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                run_id TEXT,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(run_id) REFERENCES task_runs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                storage_key TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                media_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
                FOREIGN KEY(run_id) REFERENCES task_runs(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS upload_previews (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                storage_key TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                project_id TEXT,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_owner_project_updated
                ON chat_sessions(user_id, project_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_owner_session
                ON chat_messages(user_id, session_id, id);
            CREATE INDEX IF NOT EXISTS idx_runs_owner_session
                ON task_runs(user_id, session_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_artifacts_owner_run
                ON artifacts(user_id, run_id, created_at);
            """
        )
        task_run_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_runs)").fetchall()
        }
        if "latest_status" not in task_run_columns:
            conn.execute(
                """
                ALTER TABLE task_runs
                ADD COLUMN latest_status TEXT NOT NULL
                DEFAULT 'Preparing the agent workspace…'
                """
            )
        conn.execute("PRAGMA user_version = 3")
        register_project_directories(conn)
    prune_expired_uploads()
    validate_all_project_skills_once()


def project_display_name(slug: str) -> str:
    acronyms = {"hpi": "HPI", "nmdb": "NMDB", "fhfa": "FHFA"}
    return " ".join(acronyms.get(part, part.capitalize()) for part in slug.split("-"))


def register_project_directories(conn: sqlite3.Connection) -> None:
    projects_dir = get_projects_dir()
    now = utc_now()
    for project_path in sorted(projects_dir.iterdir()):
        if not project_path.is_dir() or project_path.name.startswith("."):
            continue
        slug = slugify(project_path.name)
        conn.execute(
            """
            INSERT INTO projects (
                id, name, slug, relative_path, status, content_revision,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', 1, NULL, ?, ?)
            ON CONFLICT(id) DO UPDATE SET relative_path = excluded.relative_path
            """,
            (slug, project_display_name(slug), slug, project_path.name, now, now),
        )


def upsert_current_user(identity: TokenIdentity) -> CurrentUser:
    now = utc_now()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE issuer = ? AND subject = ?",
            (identity.issuer, identity.subject),
        ).fetchone()
        if row:
            user_id = row["id"]
            conn.execute(
                "UPDATE users SET last_seen_at = ? WHERE id = ?",
                (now, user_id),
            )
        else:
            user_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO users (id, issuer, subject, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, identity.issuer, identity.subject, now, now),
            )
    return CurrentUser(
        id=user_id,
        issuer=identity.issuer,
        subject=identity.subject,
        roles=identity.roles,
    )


def get_current_user(
    request: Request,
    x_fnma_jws_token: Optional[str] = Header(default=None, alias="x-fnma-jws-token"),
) -> CurrentUser:
    try:
        if x_fnma_jws_token and x_fnma_jws_token.strip():
            identity = decode_cdx_token(x_fnma_jws_token)
        elif DEVELOPMENT_LOGIN_ENABLED:
            development_token = request.cookies.get(DEVELOPMENT_IDENTITY_COOKIE)
            if not development_token:
                raise AuthenticationError("Development identity required")
            identity = decode_development_token(
                development_token,
                DEVELOPMENT_SIGNING_SECRET,
            )
        else:
            raise AuthenticationError("Missing x-fnma-jws-token header")
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    init_db()
    return upsert_current_user(identity)


def require_project_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_project_admin:
        raise HTTPException(status_code=403, detail="PROJECT_ADMIN role is required")
    return user


def add_audit_event(
    user_id: str,
    action: str,
    project_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_events (user_id, action, project_id, details, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, action, project_id, json.dumps(details or {}, sort_keys=True), utc_now()),
        )


def parse_available_models() -> List[str]:
    raw = os.environ.get("AVAILABLE_MODELS", "")
    models = [model.strip() for model in raw.split(",") if model.strip()]
    return models or DEFAULT_AVAILABLE_MODELS.copy()


def env_default_model(available_models: List[str]) -> str:
    configured = os.environ.get("DEFAULT_MODEL", "").strip()
    if configured and configured in available_models:
        return configured
    return available_models[0] if available_models else DEFAULT_AVAILABLE_MODELS[0]


def env_max_sessions_per_project() -> int:
    raw = os.environ.get("MAX_SESSIONS_PER_PROJECT", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_SESSIONS_PER_PROJECT
    return min(max(value, 1), 100)


def coerce_max_sessions(value: Any, fallback: int) -> int:
    try:
        return min(max(int(value), 1), 100)
    except (TypeError, ValueError):
        return fallback


def bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def get_app_settings() -> Dict[str, Any]:
    available_models = parse_available_models()
    default_model = env_default_model(available_models)
    max_sessions = env_max_sessions_per_project()

    try:
        with get_db_connection() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    except sqlite3.OperationalError:
        rows = []

    saved = {row["key"]: row["value"] for row in rows}
    saved_default = saved.get(SETTING_DEFAULT_MODEL, "").strip()
    if saved_default in available_models:
        default_model = saved_default

    max_sessions = coerce_max_sessions(
        saved.get(SETTING_MAX_SESSIONS_PER_PROJECT), max_sessions
    )

    return {
        "available_models": available_models,
        "default_model": default_model,
        "max_sessions_per_project": max_sessions,
    }


def save_app_settings(default_model: str, max_sessions_per_project: int) -> Dict[str, Any]:
    available_models = parse_available_models()
    if default_model not in available_models:
        raise HTTPException(status_code=400, detail="Default model must be one of the available models")

    max_sessions = coerce_max_sessions(max_sessions_per_project, DEFAULT_MAX_SESSIONS_PER_PROJECT)
    old_settings = get_app_settings()
    now = utc_now()
    with get_db_connection() as conn:
        conn.executemany(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            [
                (SETTING_DEFAULT_MODEL, default_model, now),
                (SETTING_MAX_SESSIONS_PER_PROJECT, str(max_sessions), now),
            ],
        )

    if default_model != old_settings["default_model"]:
        invalidate_all_agents()

    prune_all_project_sessions(max_sessions)
    return get_app_settings()


def prune_project_sessions(
    user_id: str,
    project_id: str,
    max_sessions: Optional[int] = None,
) -> None:
    limit = max_sessions if max_sessions is not None else get_app_settings()["max_sessions_per_project"]
    limit = coerce_max_sessions(limit, DEFAULT_MAX_SESSIONS_PER_PROJECT)
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT cs.id, cs.thread_id FROM chat_sessions AS cs
            WHERE cs.user_id = ? AND cs.project_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM task_runs AS tr
                  WHERE tr.session_id = cs.id AND tr.status = 'running'
              )
            ORDER BY cs.updated_at DESC, cs.created_at DESC, cs.id DESC
            """,
            (user_id, project_id),
        ).fetchall()
    for row in rows[limit:]:
        delete_session_resources(user_id, project_id, row["id"], row["thread_id"])


def prune_all_project_sessions(max_sessions: Optional[int] = None) -> None:
    limit = max_sessions if max_sessions is not None else get_app_settings()["max_sessions_per_project"]
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_id, project_id FROM chat_sessions"
        ).fetchall()
    for row in rows:
        prune_project_sessions(row["user_id"], row["project_id"], limit)


def list_project_sessions(
    user_id: str,
    project_id: str,
    prune: bool = True,
) -> List[Dict[str, Any]]:
    if prune:
        prune_project_sessions(user_id, project_id)
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT cs.*,
                   (
                       SELECT tr.id FROM task_runs AS tr
                       WHERE tr.session_id = cs.id AND tr.user_id = cs.user_id
                         AND tr.status = 'running'
                       ORDER BY tr.created_at DESC LIMIT 1
                   ) AS active_run_id,
                   (
                       SELECT tr.latest_status FROM task_runs AS tr
                       WHERE tr.session_id = cs.id AND tr.user_id = cs.user_id
                         AND tr.status = 'running'
                       ORDER BY tr.created_at DESC LIMIT 1
                   ) AS active_run_status
            FROM chat_sessions AS cs
            WHERE cs.user_id = ? AND cs.project_id = ?
            ORDER BY cs.updated_at DESC, cs.created_at DESC, cs.id DESC
            """,
            (user_id, project_id),
        ).fetchall()
    return [dict(row) for row in rows]


def row_to_project(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "relative_path": row["relative_path"],
        "status": row["status"],
        "content_revision": row["content_revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def public_project(project: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in project.items()
        if key not in {"path", "relative_path", "created_by"}
    }


def ensure_child_path(parent: Path, child: Path) -> Path:
    parent_resolved = parent.resolve()
    child_resolved = child.resolve()
    if child_resolved != parent_resolved and parent_resolved not in child_resolved.parents:
        raise HTTPException(status_code=400, detail="Path escapes the project directory")
    return child_resolved


def get_project(project_id: str) -> Dict[str, Any]:
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    project = row_to_project(row)
    if project["status"] != "active":
        raise HTTPException(status_code=409, detail="Project is being deleted")
    project_path = ensure_child_path(
        get_projects_dir(), get_projects_dir() / project["relative_path"]
    )
    project["path"] = str(project_path)
    return project


def get_project_root(project_id: str) -> Path:
    return Path(get_project(project_id)["path"])


def get_skill_dir(project_id: str) -> Path:
    return get_project_root(project_id) / "skills"


def project_skills_source(project: Dict[str, Any]) -> List[tuple[str, str]]:
    return [("/skills", project["name"])]


def project_filesystem_permission_specs() -> List[Dict[str, Any]]:
    return [
        {
            "operations": ["read", "write"],
            "paths": SENSITIVE_FILESYSTEM_PATTERNS.copy(),
            "mode": "deny",
        },
        {
            "operations": ["write"],
            "paths": PROJECT_FILESYSTEM_ALLOW_PATTERNS.copy(),
            "mode": "deny",
        },
        {
            "operations": ["read"],
            "paths": PROJECT_FILESYSTEM_ALLOW_PATTERNS.copy(),
            "mode": "allow",
        },
    ]


def create_run_context(
    user_id: str,
    project_id: str,
    session_id: str,
    run_id: str,
    create: bool = True,
) -> RunContext:
    work_directory = ensure_child_path(
        WORK_DIR,
        WORK_DIR / user_id / project_id / session_id / run_id,
    )
    artifact_directory = ensure_child_path(
        ARTIFACTS_DIR,
        ARTIFACTS_DIR / user_id / project_id / session_id / run_id,
    )
    if create:
        work_directory.mkdir(parents=True, exist_ok=True)
        artifact_directory.mkdir(parents=True, exist_ok=True)
    return RunContext(
        user_id=user_id,
        project_id=project_id,
        session_id=session_id,
        run_id=run_id,
        work_directory=work_directory,
        artifact_directory=artifact_directory,
    )


def project_artifact_context(context: RunContext) -> Dict[str, str]:
    return {
        "session_id": context.session_id,
        "run_id": context.run_id,
        "work_directory": str(context.work_directory),
        "artifact_directory": str(context.artifact_directory),
        "artifact_url_note": "Artifact URLs are registered and added by the server after the run.",
    }


def project_isolation_audit(user_id: str, project_id: str) -> Dict[str, Any]:
    project = get_project(project_id)
    project_root = Path(project["path"])
    projects_dir = get_projects_dir()
    skills_dir = get_skill_dir(project_id)
    skills = scan_project_skills(project_id)
    sample_session_id = "isolation-audit"
    sample_run_id = "sample-run"
    run_context = create_run_context(
        user_id, project_id, sample_session_id, sample_run_id, create=False
    )
    artifact_context = project_artifact_context(run_context)
    artifact_dir = run_context.artifact_directory.resolve()

    checks = {
        "project_root_within_projects_dir": project_root == projects_dir or projects_dir in project_root.parents,
        "skills_dir_within_project_root": skills_dir == project_root or project_root in skills_dir.parents,
        "skills_are_project_local": all(
            (skills_dir / skill["name"] / "SKILL.md").resolve().is_file()
            and project_root in (skills_dir / skill["name"] / "SKILL.md").resolve().parents
            for skill in skills
        ),
        "checkpoint_threads_include_user_project_and_session": (
            session_thread_id(user_id, project_id, sample_session_id)
            == f"user:{user_id}:project:{project_id}:session:{sample_session_id}"
        ),
        "agent_checkpoints_separate_from_app_db": AGENT_CHECKPOINT_DB_PATH.resolve() != DB_PATH.resolve(),
        "artifact_directory_user_project_session_run_scoped": (
            artifact_dir
            == (
                ARTIFACTS_DIR
                / user_id
                / project_id
                / sample_session_id
                / sample_run_id
            ).resolve()
        ),
        "work_directory_user_project_session_run_scoped": (
            run_context.work_directory.resolve()
            == (
                WORK_DIR
                / user_id
                / project_id
                / sample_session_id
                / sample_run_id
            ).resolve()
        ),
    }

    return {
        "project_id": project_id,
        "project_name": project["name"],
        "project_root": str(project_root),
        "skills_dir": str(skills_dir),
        "skills": skills,
        "skills_source": project_skills_source(project),
        "filesystem_backend": {
            "root_dir": str(project_root),
            "virtual_mode": True,
        },
        "filesystem_permissions": project_filesystem_permission_specs(),
        "checkpointing": {
            "app_db": str(DB_PATH),
            "agent_checkpoint_db": str(AGENT_CHECKPOINT_DB_PATH),
            "sample_thread_id": session_thread_id(user_id, project_id, sample_session_id),
        },
        "artifacts": artifact_context,
        "checks": checks,
        "passed": all(checks.values()),
    }


def touch_project(project_id: str, bump_revision: bool = False) -> None:
    with get_db_connection() as conn:
        if bump_revision:
            conn.execute(
                """
                UPDATE projects
                SET updated_at = ?, content_revision = content_revision + 1
                WHERE id = ?
                """,
                (utc_now(), project_id),
            )
        else:
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?", (utc_now(), project_id)
            )


def create_project_record(name: str, created_by: str) -> Dict[str, Any]:
    projects_dir = get_projects_dir()
    base_slug = slugify(name)
    slug = base_slug

    with get_db_connection() as conn:
        counter = 2
        while conn.execute("SELECT 1 FROM projects WHERE id = ?", (slug,)).fetchone():
            slug = f"{base_slug}-{counter}"
            counter += 1

        project_path = ensure_child_path(projects_dir, projects_dir / slug)
        if project_path.exists():
            raise HTTPException(status_code=409, detail="Project folder already exists")

        project_path.mkdir(parents=True)
        (project_path / "data").mkdir()
        (project_path / "skills").mkdir()

        now = utc_now()
        conn.execute(
            """
            INSERT INTO projects (
                id, name, slug, relative_path, status, content_revision,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', 1, ?, ?, ?)
            """,
            (slug, name.strip(), slug, slug, created_by, now, now),
        )
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (slug,)).fetchone()
        project = row_to_project(row)
        project["path"] = str(project_path)
        return project


def relative_project_path(project_root: Path, user_path: str) -> Path:
    if not user_path or user_path in {".", "/"}:
        return project_root
    normalized = user_path.replace("\\", "/").lstrip("/")
    candidate = project_root / normalized
    return ensure_child_path(project_root, candidate)


def strip_media_reference(raw_path: str) -> str:
    cleaned = raw_path.strip().strip("`'\"")
    while cleaned and cleaned[-1] in ".,;:)":
        cleaned = cleaned[:-1]
    return cleaned


def project_media_url(user_id: str, project_id: str, file_path: Path) -> Optional[str]:
    project_root = get_project_root(project_id)
    resolved = file_path.resolve()

    if resolved.suffix.lower() not in MEDIA_EXTENSIONS or not resolved.is_file():
        return None

    if resolved == project_root or project_root in resolved.parents:
        rel = resolved.relative_to(project_root).as_posix()
        return f"/api/projects/{project_id}/media/{quote(rel, safe='/')}"

    artifacts_root = ARTIFACTS_DIR.resolve()
    if resolved == artifacts_root or artifacts_root in resolved.parents:
        storage_key = resolved.relative_to(artifacts_root).as_posix()
        with get_db_connection() as conn:
            row = conn.execute(
                """
                SELECT id FROM artifacts
                WHERE user_id = ? AND project_id = ? AND storage_key = ?
                """,
                (user_id, project_id, storage_key),
            ).fetchone()
        if row:
            return f"/api/artifacts/{row['id']}"

    return None


def resolve_media_reference(
    user_id: str,
    project_id: str,
    raw_path: str,
) -> Optional[Dict[str, str]]:
    cleaned = strip_media_reference(raw_path)
    if not cleaned:
        return None

    project_root = get_project_root(project_id)
    candidates: List[Path] = []

    if cleaned.startswith("/api/artifacts/") or cleaned.startswith(
        f"/api/projects/{project_id}/media/"
    ):
        return {"path": cleaned, "url": cleaned, "suffix": Path(cleaned).suffix.lower()}
    if cleaned.startswith("/"):
        candidates.append(Path(cleaned))
    else:
        candidates.append(project_root / cleaned)
        candidates.append(BASE_DIR / cleaned)

    for candidate in candidates:
        try:
            url = project_media_url(user_id, project_id, candidate)
        except (OSError, RuntimeError):
            continue
        if url:
            suffix = candidate.suffix.lower()
            return {"path": cleaned, "url": url, "suffix": suffix}
    return None


def media_embed_markdown(media: Dict[str, str]) -> str:
    suffix = media["suffix"]
    url = media["url"]
    if suffix in IMAGE_EXTENSIONS:
        return f"![Generated image]({url})"
    if suffix in VIDEO_EXTENSIONS:
        return f'<video controls src="{url}"></video>'
    if suffix in AUDIO_EXTENSIONS:
        return f'<audio controls src="{url}"></audio>'
    return f"[Generated file]({url})"


def line_without_media_paths(line: str, paths: List[str]) -> str:
    remainder = line
    for path in paths:
        remainder = remainder.replace(path, "")
    return remainder.strip(" \t:-`'\"")


def media_urls_already_embedded(user_id: str, project_id: str, line: str) -> set[str]:
    urls: set[str] = set()
    for match in MARKDOWN_IMAGE_RE.finditer(line):
        media = resolve_media_reference(user_id, project_id, match.group("href"))
        urls.add(media["url"] if media else match.group("href"))
    for match in HTML_MEDIA_SRC_RE.finditer(line):
        media = resolve_media_reference(user_id, project_id, match.group("src"))
        urls.add(media["url"] if media else match.group("src"))
    return urls


def embedded_media_spans(line: str) -> List[tuple[int, int]]:
    spans = [match.span() for match in MARKDOWN_IMAGE_RE.finditer(line)]
    spans.extend(match.span() for match in HTML_MEDIA_SRC_RE.finditer(line))
    return spans


def span_inside_any(start: int, end: int, spans: List[tuple[int, int]]) -> bool:
    return any(span_start <= start and end <= span_end for span_start, span_end in spans)


def normalize_project_media_links(user_id: str, project_id: str, text: str) -> str:
    """Replace local generated media paths with embeddable project media links."""
    output: List[str] = []
    embedded_urls: set[str] = set()

    for line in text.splitlines():
        existing_line_urls = media_urls_already_embedded(user_id, project_id, line)
        embedded_urls.update(existing_line_urls)
        protected_spans = embedded_media_spans(line)

        matches = [
            match
            for match in MEDIA_PATH_RE.finditer(line)
            if not span_inside_any(match.start("path"), match.end("path"), protected_spans)
        ]
        media_items: List[Dict[str, str]] = []
        for match in matches:
            media = resolve_media_reference(user_id, project_id, match.group("path"))
            if media:
                media["raw"] = match.group("path")
                media_items.append(media)

        if not media_items:
            output.append(line)
            continue

        raw_paths = [item["raw"] for item in media_items]
        remainder = line_without_media_paths(line, raw_paths)
        new_media_items: List[Dict[str, str]] = []
        duplicate_paths: List[str] = []
        seen_line_urls: set[str] = set()
        for item in media_items:
            url = item["url"]
            if url in embedded_urls or url in seen_line_urls:
                duplicate_paths.append(item["raw"])
                continue
            new_media_items.append(item)
            seen_line_urls.add(url)

        if not new_media_items:
            cleaned_line = line
            for path in duplicate_paths:
                cleaned_line = cleaned_line.replace(path, "")
            if existing_line_urls:
                cleaned_line = TRAILING_MEDIA_LABEL_RE.sub("", cleaned_line)
            cleaned_remainder = cleaned_line.strip(" \t:-`'\"")
            if cleaned_remainder and not MEDIA_LABEL_RE.match(cleaned_remainder):
                output.append(cleaned_line.rstrip(" \t:-"))
            continue

        embeds = [media_embed_markdown(item) for item in new_media_items]
        embedded_urls.update(item["url"] for item in new_media_items)

        if not remainder or MEDIA_LABEL_RE.match(remainder):
            label_index = len(output) - 1
            while label_index >= 0 and not output[label_index].strip():
                label_index -= 1
            if label_index >= 0 and MEDIA_LABEL_RE.match(output[label_index]):
                del output[label_index:]
                output.append("\n\n".join(embeds))
            else:
                output.append("\n\n".join(embeds))
            continue

        normalized_line = line
        for item, embed in zip(new_media_items, embeds):
            normalized_line = normalized_line.replace(item["raw"], f"\n\n{embed}\n\n", 1)
        for path in duplicate_paths:
            normalized_line = normalized_line.replace(path, "")
        output.append(normalized_line)

    return "\n".join(output)


_SKILLS_VALIDATED = False


def parse_skill_frontmatter(skill_md: Path) -> Dict[str, str]:
    try:
        content = skill_md.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not read skill file %s: %s", skill_md, exc)
        return {}

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        logger.warning("Skill file %s has no YAML frontmatter", skill_md)
        return {}

    metadata: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")
    return metadata


def validate_skill_metadata(skill_dir: Path) -> None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return

    metadata = parse_skill_frontmatter(skill_md)
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    if not name or not description:
        logger.warning("Skill %s is missing required name or description frontmatter", skill_md)
        return

    if name != skill_dir.name:
        logger.warning(
            "Skill %s frontmatter name %r does not match folder name %r",
            skill_md,
            name,
            skill_dir.name,
        )

    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        logger.warning(
            "Skill %s name %r is not Agent Skills spec-compliant; use lowercase "
            "alphanumeric words separated by single hyphens for full compliance",
            skill_md,
            name,
        )


def validate_project_skills(project_id: str) -> None:
    skills_dir = get_skill_dir(project_id)
    if not skills_dir.exists():
        return

    for skill_dir in sorted(skills_dir.iterdir()):
        if skill_dir.is_dir():
            validate_skill_metadata(skill_dir)


def validate_all_project_skills_once() -> None:
    global _SKILLS_VALIDATED
    if _SKILLS_VALIDATED:
        return

    try:
        with get_db_connection() as conn:
            rows = conn.execute("SELECT id FROM projects").fetchall()
        for row in rows:
            validate_project_skills(row["id"])
    except Exception as exc:
        logger.warning("Could not validate project skills: %s", exc)
    finally:
        _SKILLS_VALIDATED = True


def scan_project_skills(project_id: str) -> List[Dict[str, str]]:
    skills_dir = get_skill_dir(project_id)
    skills: List[Dict[str, str]] = []
    if not skills_dir.exists():
        return skills

    for skill_path in sorted(skills_dir.iterdir()):
        skill_md = skill_path / "SKILL.md"
        if skill_path.is_symlink() or not skill_path.is_dir() or not skill_md.exists():
            continue

        desc = f"Specialist skill for {skill_path.name}"
        try:
            lines = skill_md.read_text(encoding="utf-8").splitlines()[:30]
            for line in lines:
                if line.lower().startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip("\"'")
                    break
                if line.startswith("#"):
                    desc = line.replace("#", "").strip()
        except Exception:
            pass

        skills.append({"name": skill_path.name, "description": desc})
    return skills


def list_project_files(project_root: Path) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not project_root.exists():
        return items

    for path in sorted(project_root.rglob("*")):
        if path.is_symlink():
            continue
        rel = path.relative_to(project_root).as_posix()
        if any(part.startswith(".") for part in path.relative_to(project_root).parts):
            continue
        stat = path.stat()
        items.append(
            {
                "path": rel,
                "name": path.name,
                "type": "directory" if path.is_dir() else "file",
                "size": 0 if path.is_dir() else stat.st_size,
                "updated_at": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat(),
            }
        )
        if len(items) >= 1000:
            break
    return items


def upload_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
                    TMP_UPLOADS_DIR, TMP_UPLOADS_DIR / row["storage_key"]
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
            stale_path = ensure_child_path(TMP_UPLOADS_DIR, TMP_UPLOADS_DIR / row["storage_key"])
            stale_path.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail="Upload preview expired or not found")
        if consume:
            conn.execute(
                "UPDATE upload_previews SET consumed_at = ? WHERE token_hash = ?",
                (utc_now(), token_hash),
            )
    path = ensure_child_path(TMP_UPLOADS_DIR, TMP_UPLOADS_DIR / row["storage_key"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Upload preview expired or not found")
    return path


def record_upload_preview(user_id: str, token: str, zip_path: Path) -> None:
    ttl_minutes = bounded_env_int("UPLOAD_PREVIEW_TTL_MINUTES", 15, 1, 1440)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    storage_key = zip_path.resolve().relative_to(TMP_UPLOADS_DIR.resolve()).as_posix()
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


def session_thread_id(user_id: str, project_id: str, session_id: str) -> str:
    return f"user:{user_id}:project:{project_id}:session:{session_id}"


def create_session(
    user_id: str,
    project_id: str,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    get_project(project_id)
    session_id = uuid.uuid4().hex
    now = utc_now()
    final_title = title.strip() if title and title.strip() else "New chat"
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO chat_sessions (
                id, project_id, user_id, title, thread_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                project_id,
                user_id,
                final_title,
                session_thread_id(user_id, project_id, session_id),
                now,
                now,
            ),
        )
    prune_project_sessions(user_id, project_id)
    return {
        "id": session_id,
        "project_id": project_id,
        "title": final_title,
        "thread_id": session_thread_id(user_id, project_id, session_id),
        "created_at": now,
        "updated_at": now,
    }


def get_session(user_id: str, project_id: str, session_id: str) -> Dict[str, Any]:
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM chat_sessions
            WHERE id = ? AND project_id = ? AND user_id = ?
            """,
            (session_id, project_id, user_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return dict(row)


def add_chat_message(
    user_id: str,
    project_id: str,
    session_id: str,
    role: str,
    content: str,
    run_id: Optional[str] = None,
) -> None:
    now = utc_now()
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO chat_messages (
                project_id, session_id, user_id, run_id, role, content, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, session_id, user_id, run_id, role, content, now),
        )
        conn.execute(
            """
            UPDATE chat_sessions SET updated_at = ?
            WHERE id = ? AND project_id = ? AND user_id = ?
            """,
            (now, session_id, project_id, user_id),
        )


def maybe_title_session(user_id: str, project_id: str, session_id: str, message: str) -> None:
    title = message.strip().replace("\n", " ")
    if len(title) > 60:
        title = title[:57].rstrip() + "..."
    if not title:
        return

    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT title FROM chat_sessions
            WHERE id = ? AND project_id = ? AND user_id = ?
            """,
            (session_id, project_id, user_id),
        ).fetchone()
        if row and row["title"] == "New chat":
            conn.execute(
                """
                UPDATE chat_sessions SET title = ?, updated_at = ?
                WHERE id = ? AND project_id = ? AND user_id = ?
                """,
                (title, utc_now(), session_id, project_id, user_id),
            )


def create_task_run(
    user_id: str,
    project_id: str,
    session_id: str,
    prompt: str,
) -> Dict[str, Any]:
    project = get_project(project_id)
    run_id = uuid.uuid4().hex
    now = utc_now()
    with get_db_connection() as conn:
        active_count = conn.execute(
            "SELECT COUNT(*) AS count FROM task_runs WHERE user_id = ? AND status = 'running'",
            (user_id,),
        ).fetchone()["count"]
        concurrent_limit = bounded_env_int("MAX_CONCURRENT_RUNS_PER_USER", 3, 1, 20)
        if active_count >= concurrent_limit:
            raise HTTPException(
                status_code=429,
                detail="Concurrent task limit reached for this user",
            )
        active = conn.execute(
            """
            SELECT 1 FROM task_runs
            WHERE user_id = ? AND session_id = ? AND status = 'running'
            LIMIT 1
            """,
            (user_id, session_id),
        ).fetchone()
        if active:
            raise HTTPException(
                status_code=409,
                detail="This chat already has a task running",
            )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, session_id, user_id, project_revision,
                status, latest_status, prompt, created_at
            ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)
            """,
            (
                run_id,
                project_id,
                session_id,
                user_id,
                project["content_revision"],
                "Preparing the agent workspace…",
                prompt,
                now,
            ),
        )
    context = create_run_context(user_id, project_id, session_id, run_id)
    return {"id": run_id, "context": context, "created_at": now}


def update_task_run_activity(run_id: str, message: str) -> None:
    status = message.strip()
    if not status:
        return
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE task_runs SET latest_status = ?
            WHERE id = ? AND status = 'running' AND latest_status != ?
            """,
            (status, run_id, status),
        )


def finish_task_run(run_id: str, status: str, error_summary: Optional[str] = None) -> None:
    if status not in {"completed", "failed", "cancelled"}:
        raise ValueError(f"Unsupported terminal run status: {status}")
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE task_runs
            SET status = ?, latest_status = ?, error_summary = ?, completed_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (
                status,
                {
                    "completed": "Completed",
                    "failed": "Failed",
                    "cancelled": "Cancelled",
                }[status],
                error_summary,
                utc_now(),
                run_id,
            ),
        )


RUN_WORK_INTERNAL_NAMES = {
    ".matplotlib",
    ".python-cache",
    "__pycache__",
    "generated.py",
}


def unique_artifact_path(artifact_root: Path, relative_path: Path) -> Path:
    target = ensure_child_path(artifact_root, artifact_root / relative_path)
    if not target.exists():
        return target
    for sequence in range(2, 10_000):
        candidate = target.with_name(f"{target.stem}-{sequence}{target.suffix}")
        candidate = ensure_child_path(artifact_root, candidate)
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not allocate a unique artifact name for {relative_path}")


def collect_run_work_artifacts(context: RunContext) -> List[Path]:
    """Move generated workspace files into the run's retained artifact directory."""
    work_directory = ensure_child_path(WORK_DIR, context.work_directory)
    artifact_directory = ensure_child_path(ARTIFACTS_DIR, context.artifact_directory)
    if not work_directory.exists():
        return []

    artifact_directory.mkdir(parents=True, exist_ok=True)
    collected: List[Path] = []
    for path in sorted(work_directory.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative_path = path.relative_to(work_directory)
        if any(part in RUN_WORK_INTERNAL_NAMES for part in relative_path.parts):
            continue
        target = unique_artifact_path(artifact_directory, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        collected.append(target)
    return collected


def register_run_artifacts(context: RunContext) -> List[Dict[str, Any]]:
    artifacts: List[Dict[str, Any]] = []
    collect_run_work_artifacts(context)
    if not context.artifact_directory.exists():
        return artifacts
    for path in sorted(context.artifact_directory.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        resolved = ensure_child_path(ARTIFACTS_DIR, path)
        storage_key = resolved.relative_to(ARTIFACTS_DIR.resolve()).as_posix()
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        artifact_id = uuid.uuid4().hex
        size = path.stat().st_size
        with get_db_connection() as conn:
            existing = conn.execute(
                "SELECT * FROM artifacts WHERE storage_key = ?",
                (storage_key,),
            ).fetchone()
            if existing:
                artifacts.append(dict(existing))
                continue
            conn.execute(
                """
                INSERT INTO artifacts (
                    id, project_id, session_id, run_id, user_id, storage_key,
                    display_name, media_type, size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    context.project_id,
                    context.session_id,
                    context.run_id,
                    context.user_id,
                    storage_key,
                    path.name,
                    media_type,
                    size,
                    utc_now(),
                ),
            )
        artifacts.append(
            {
                "id": artifact_id,
                "storage_key": storage_key,
                "display_name": path.name,
                "media_type": media_type,
                "size": size,
            }
        )
    return artifacts


def append_artifact_links(text: str, artifacts: List[Dict[str, Any]]) -> str:
    links: List[str] = []
    for artifact in artifacts:
        url = f"/api/artifacts/{artifact['id']}"
        if url in text:
            continue
        name = artifact["display_name"]
        media_type = artifact["media_type"]
        if media_type.startswith("image/"):
            links.append(f"![{name}]({url})")
        else:
            links.append(f"[{name}]({url})")
    if not links:
        return text
    return f"{text.rstrip()}\n\n### Generated artifacts\n\n" + "\n\n".join(links)


def artifact_path_variants(artifact: Dict[str, Any]) -> List[str]:
    storage_key = str(artifact.get("storage_key", "")).strip()
    if not storage_key:
        return []
    try:
        artifact_path = ensure_child_path(ARTIFACTS_DIR, ARTIFACTS_DIR / storage_key)
    except (HTTPException, OSError, RuntimeError, ValueError):
        return []

    variants = {str(artifact_path), artifact_path.as_posix()}
    try:
        variants.add(artifact_path.relative_to(BASE_DIR).as_posix())
    except ValueError:
        pass
    storage_parts = PurePosixPath(storage_key).parts
    if len(storage_parts) >= 5:
        run_relative_path = Path(*storage_parts[4:])
        variants.add(run_relative_path.as_posix())
        work_path = WORK_DIR.joinpath(*storage_parts[:4], run_relative_path)
        variants.update({str(work_path), work_path.as_posix()})
    variants.update(f"file://{value}" for value in list(variants) if value.startswith("/"))
    return sorted(variants, key=len, reverse=True)


def artifact_label_only(line: str, artifacts: List[Dict[str, Any]]) -> bool:
    plain = re.sub(r"^[\s#>*_`-]+|[\s*_`]+$", "", line).strip()
    if not plain:
        return True
    lowered = plain.lower().rstrip(":")
    names = [str(item.get("display_name", "")).lower() for item in artifacts]
    if lowered in names:
        return True

    words = re.findall(r"[a-z0-9]+", lowered)
    artifact_words = {
        "artifact", "artifacts", "audio", "chart", "csv", "data", "download",
        "downloads", "excel", "file", "files", "generated", "graph", "heatmap",
        "image", "json", "output", "outputs", "path", "paths", "pdf", "plot",
        "spreadsheet", "video", "workbook",
    }
    is_short_label = len(words) <= 8 and bool(set(words) & artifact_words)
    return is_short_label and (plain.endswith(":") or len(words) <= 4)


EMPTY_FENCED_BLOCK_RE = re.compile(
    r"(?m)^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n"
    r"(?:[ \t]*\n)*[ \t]*(?P=fence)[ \t]*(?:\n|$)"
)
ARTIFACT_SECTION_WORDS = {
    "artifact",
    "artifacts",
    "chart",
    "charts",
    "download",
    "downloads",
    "file",
    "files",
    "graph",
    "graphs",
    "image",
    "images",
    "output",
    "outputs",
    "plot",
    "plots",
    "visualization",
    "visualizations",
}
ARTIFACT_ANNOUNCEMENT_WORDS = {
    "above",
    "available",
    "below",
    "created",
    "download",
    "downloaded",
    "exported",
    "generated",
    "here",
    "saved",
    "written",
}


def artifact_placeholder_line(line: str, artifacts: List[Dict[str, Any]]) -> bool:
    plain = re.sub(r"^[\s#>*_`-]+|[\s*_`]+$", "", line).strip()
    if not plain or re.fullmatch(r"[-*_]{3,}", plain):
        return True
    if artifact_label_only(plain, artifacts):
        return True
    words = set(re.findall(r"[a-z0-9]+", plain.lower()))
    return bool(words & ARTIFACT_SECTION_WORDS) and bool(
        words & ARTIFACT_ANNOUNCEMENT_WORDS
    )


def remove_empty_artifact_sections(text: str, artifacts: List[Dict[str, Any]]) -> str:
    """Remove empty fences and headings left behind after private paths are stripped."""
    text = EMPTY_FENCED_BLOCK_RE.sub("", text)
    lines = text.splitlines()
    output: List[str] = []
    index = 0
    while index < len(lines):
        heading = re.match(r"^\s*#{1,6}\s+(?P<title>.+?)\s*$", lines[index])
        if not heading:
            output.append(lines[index])
            index += 1
            continue

        end = index + 1
        while end < len(lines) and not re.match(r"^\s*#{1,6}\s+", lines[end]):
            end += 1
        title_words = set(re.findall(r"[a-z0-9]+", heading.group("title").lower()))
        section_lines = lines[index + 1 : end]
        if (
            title_words & ARTIFACT_SECTION_WORDS
            and all(artifact_placeholder_line(line, artifacts) for line in section_lines)
        ):
            while output and not output[-1].strip():
                output.pop()
            index = end
            continue

        output.extend(lines[index:end])
        index = end

    normalized = "\n".join(output).strip()
    return re.sub(r"\n{3,}", "\n\n", normalized)


GENERATED_ARTIFACTS_HEADING_RE = re.compile(
    r"(?im)^\s*#{1,6}\s+Generated artifacts\s*$"
)
VISUAL_ARTIFACT_HEADING_RE = re.compile(
    r"(?im)^\s*#{1,6}\s+.*(?:visualization|chart|plot|graph|image|artifact).*$"
)


def artifact_is_referenced(text: str, artifact: Dict[str, Any]) -> bool:
    artifact_id = str(artifact.get("id", "")).strip()
    if artifact_id and f"/api/artifacts/{artifact_id}" in text:
        return True
    return any(path in text for path in artifact_path_variants(artifact))


def select_response_artifacts(
    text: str, artifacts: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Hide superseded image attempts while retaining every non-image download."""
    if not artifacts:
        return []
    sections = GENERATED_ARTIFACTS_HEADING_RE.split(text, maxsplit=1)
    body = sections[0]
    previous_generated_section = sections[1] if len(sections) == 2 else ""
    images = [
        artifact
        for artifact in artifacts
        if str(artifact.get("media_type", "")).startswith("image/")
    ]
    if len(images) <= 1:
        return artifacts

    body_references = [
        artifact for artifact in images if artifact_is_referenced(body, artifact)
    ]
    if body_references:
        selected_image_ids = {artifact["id"] for artifact in body_references}
    elif EMPTY_FENCED_BLOCK_RE.search(body) and VISUAL_ARTIFACT_HEADING_RE.search(body):
        # Legacy responses lost their path before this selection existed. A
        # singular, empty visualization placeholder means earlier chart files
        # were intermediate attempts; retain the last registered image.
        selected_image_ids = {images[-1]["id"]}
    else:
        prior_references = [
            artifact
            for artifact in images
            if artifact_is_referenced(previous_generated_section, artifact)
        ]
        if not prior_references:
            return artifacts
        selected_image_ids = {artifact["id"] for artifact in prior_references}

    return [
        artifact
        for artifact in artifacts
        if not str(artifact.get("media_type", "")).startswith("image/")
        or artifact["id"] in selected_image_ids
    ]


def strip_internal_artifact_paths(text: str, artifacts: List[Dict[str, Any]]) -> str:
    """Remove registered file references before rebuilding the artifact section."""
    if not artifacts:
        return text

    path_entries: List[tuple[str, str]] = []
    for artifact in artifacts:
        name = str(artifact.get("display_name", "Generated file"))
        path_entries.extend((variant, name) for variant in artifact_path_variants(artifact))
        artifact_id = str(artifact.get("id", "")).strip()
        if artifact_id:
            path_entries.append((f"/api/artifacts/{artifact_id}", name))
    if not path_entries:
        return text

    # This section is generated by the server, so discard any previously stored
    # version and reconstruct it from the current artifact records below.
    text = GENERATED_ARTIFACTS_HEADING_RE.split(text, maxsplit=1)[0].rstrip()

    output: List[str] = []
    for line in text.splitlines():
        matching = [(path, name) for path, name in path_entries if path in line]
        if not matching:
            output.append(line)
            continue

        cleaned = line
        for path, name in matching:
            markdown_link = re.compile(
                rf"!?\[[^\]\n]*\]\(\s*<?{re.escape(path)}>?(?:\s+['\"][^'\"]*['\"])?\s*\)"
            )
            cleaned = markdown_link.sub("", cleaned)
            html_media = re.compile(
                rf"<(?:img|video|audio)\b[^>]*\bsrc=['\"]{re.escape(path)}['\"][^>]*>"
                rf"(?:\s*</(?:video|audio)>)?",
                re.IGNORECASE,
            )
            cleaned = html_media.sub("", cleaned)
            cleaned = cleaned.replace(path, "")

        cleaned = re.sub(r"[ \t]+([,.;:])", r"\1", cleaned).rstrip()

        if artifact_label_only(cleaned, artifacts):
            while output and not output[-1].strip():
                output.pop()
            if output and artifact_label_only(output[-1], artifacts):
                output.pop()
            continue
        output.append(cleaned)

    return remove_empty_artifact_sections("\n".join(output), artifacts)


def prepare_response_artifacts(
    user_id: str,
    project_id: str,
    text: str,
    artifacts: List[Dict[str, Any]],
) -> str:
    response_artifacts = select_response_artifacts(text, artifacts)
    sanitized = strip_internal_artifact_paths(text, artifacts)
    normalized = normalize_project_media_links(user_id, project_id, sanitized)
    return append_artifact_links(normalized, response_artifacts)


def cleanup_run_work(context: RunContext) -> None:
    try:
        work_dir = ensure_child_path(WORK_DIR, context.work_directory)
        if work_dir.exists():
            shutil.rmtree(work_dir)
        parent = work_dir.parent
        work_root = WORK_DIR.resolve()
        while parent != work_root and work_root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    except Exception as exc:
        logger.warning("Could not clean run work directory %s: %s", context.run_id, exc)


def recover_interrupted_runs() -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE task_runs
            SET status = 'failed', error_summary = 'Server restarted during the run',
                completed_at = ?
            WHERE status = 'running'
            """,
            (utc_now(),),
        )
    if not WORK_DIR.exists():
        return
    for child in WORK_DIR.iterdir():
        try:
            resolved = ensure_child_path(WORK_DIR, child)
            if resolved.is_dir() and not resolved.is_symlink():
                shutil.rmtree(resolved)
            elif resolved.is_file() or resolved.is_symlink():
                resolved.unlink()
        except Exception as exc:
            logger.warning("Could not remove orphaned work path %s: %s", child, exc)


def delete_session_resources(
    user_id: str,
    project_id: str,
    session_id: str,
    thread_id: Optional[str] = None,
) -> None:
    with get_db_connection() as conn:
        session = conn.execute(
            """
            SELECT thread_id FROM chat_sessions
            WHERE id = ? AND project_id = ? AND user_id = ?
            """,
            (session_id, project_id, user_id),
        ).fetchone()
        run_rows = conn.execute(
            """
            SELECT id FROM task_runs
            WHERE session_id = ? AND project_id = ? AND user_id = ?
            """,
            (session_id, project_id, user_id),
        ).fetchall()
        active_run = conn.execute(
            """
            SELECT 1 FROM task_runs
            WHERE session_id = ? AND project_id = ? AND user_id = ?
              AND status = 'running'
            LIMIT 1
            """,
            (session_id, project_id, user_id),
        ).fetchone()
    if not session:
        return
    if active_run:
        raise HTTPException(status_code=409, detail="A task is still running in this chat")

    artifact_session_dir = ensure_child_path(
        ARTIFACTS_DIR, ARTIFACTS_DIR / user_id / project_id / session_id
    )
    if artifact_session_dir.exists():
        shutil.rmtree(artifact_session_dir)
    for row in run_rows:
        context = create_run_context(
            user_id, project_id, session_id, row["id"], create=False
        )
        cleanup_run_work(context)

    delete_checkpoint_thread(thread_id or session["thread_id"])
    with get_db_connection() as conn:
        conn.execute(
            """
            DELETE FROM chat_sessions
            WHERE id = ? AND project_id = ? AND user_id = ?
            """,
            (session_id, project_id, user_id),
        )


def message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "\n\n".join(p for p in parts if p.strip())
    return str(content)


def python_binary() -> str:
    candidate = SDK_DIR / "venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    raise RuntimeError("SDK virtual environment python binary not found at deep-agents-sdk/venv/bin/python")


ProjectTreeSnapshot = Dict[str, tuple[str, int, int]]
_project_execution_locks_guard = threading.Lock()
_project_execution_locks: Dict[str, threading.RLock] = {}


def project_execution_lock(project_root: Path) -> threading.RLock:
    key = str(project_root.resolve())
    with _project_execution_locks_guard:
        return _project_execution_locks.setdefault(key, threading.RLock())


def project_tree_snapshot(project_root: Path) -> ProjectTreeSnapshot:
    snapshot: ProjectTreeSnapshot = {}
    for directory, directory_names, file_names in os.walk(
        project_root, topdown=True, followlinks=False
    ):
        directory_path = Path(directory)
        for name in list(directory_names):
            path = directory_path / name
            relative = path.relative_to(project_root).as_posix()
            metadata = path.lstat()
            kind = "symlink" if path.is_symlink() else "directory"
            snapshot[relative] = (kind, metadata.st_size, metadata.st_mtime_ns)
            if path.is_symlink():
                directory_names.remove(name)
        for name in file_names:
            path = directory_path / name
            relative = path.relative_to(project_root).as_posix()
            metadata = path.lstat()
            kind = "symlink" if path.is_symlink() else "file"
            snapshot[relative] = (kind, metadata.st_size, metadata.st_mtime_ns)
    return snapshot


def recover_new_project_files(
    project_root: Path,
    before: ProjectTreeSnapshot,
    after: ProjectTreeSnapshot,
    artifact_directory: Path,
) -> List[str]:
    """Quarantine files unexpectedly created in a shared project by generated code."""
    recovered: List[str] = []
    new_entries = sorted(set(after) - set(before))
    for relative_name in new_entries:
        kind = after[relative_name][0]
        source = project_root / relative_name
        if kind != "file" or not source.is_file() or source.is_symlink():
            continue
        relative_path = Path("recovered-project-writes") / relative_name
        target = unique_artifact_path(artifact_directory, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        recovered.append(relative_name)

    for relative_name in sorted(new_entries, key=lambda value: value.count("/"), reverse=True):
        source = project_root / relative_name
        try:
            if source.is_symlink():
                source.unlink()
            elif source.is_dir():
                source.rmdir()
        except OSError:
            pass
    return recovered


def shared_project_mutations(
    before: ProjectTreeSnapshot, after: ProjectTreeSnapshot
) -> List[str]:
    mutations: List[str] = []
    for relative_name in sorted(set(before) & set(after)):
        old_kind, old_size, old_mtime = before[relative_name]
        new_kind, new_size, new_mtime = after[relative_name]
        if old_kind == "directory" and new_kind == "directory":
            continue
        if (old_kind, old_size, old_mtime) != (new_kind, new_size, new_mtime):
            mutations.append(f"modified {relative_name}")
    for relative_name in sorted(set(before) - set(after)):
        if before[relative_name][0] != "directory":
            mutations.append(f"deleted {relative_name}")
    return mutations


def prepare_python_workspace(context: RunContext, project_root: Path) -> None:
    context.work_directory.mkdir(parents=True, exist_ok=True)
    context.artifact_directory.mkdir(parents=True, exist_ok=True)
    data_directory = project_root / "data"
    data_link = context.work_directory / "data"
    if data_directory.is_dir() and not data_link.exists():
        data_link.symlink_to(data_directory, target_is_directory=True)


def execute_python_code(code: str, project_root: Path) -> str:
    context = CURRENT_RUN_CONTEXT.get()
    if context is None:
        return "Error executing code: no active user run context."
    project_root = ensure_child_path(PROJECTS_ROOT, project_root)
    expected_project_root = get_project_root(context.project_id).resolve()
    if project_root != expected_project_root:
        return "Error executing code: active run context does not match the selected project."
    prepare_python_workspace(context, project_root)
    temp_path = context.work_directory / "generated.py"
    timeout_seconds = bounded_env_int("PYTHON_EXECUTION_TIMEOUT_SECONDS", 30, 1, 3600)
    with project_execution_lock(project_root):
        before = project_tree_snapshot(project_root)
        try:
            temp_path.write_text(code, encoding="utf-8")
            execution_env = {
                "PATH": os.environ.get("PATH", ""),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "MPLBACKEND": "Agg",
                "MPLCONFIGDIR": str(context.work_directory / ".matplotlib"),
                "PYTHONPYCACHEPREFIX": str(context.work_directory / ".python-cache"),
                "TMPDIR": str(context.work_directory),
                "DEEP_AGENTS_PROJECT_DIR": str(project_root),
                "DEEP_AGENTS_RUN_ARTIFACT_DIR": str(context.artifact_directory),
            }
            result = subprocess.run(
                [python_binary(), str(temp_path)],
                cwd=str(context.work_directory),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=execution_env,
            )
            output = ""
            if result.stdout:
                output += f"Output:\n{result.stdout[:1_000_000]}\n"
            if result.stderr:
                output += f"Errors:\n{result.stderr[:1_000_000]}\n"
            if result.returncode and not result.stderr:
                output += f"Errors:\nPython exited with status {result.returncode}.\n"
        except subprocess.TimeoutExpired:
            output = f"Error: Code execution timed out after {timeout_seconds} seconds."
        except Exception as exc:
            output = f"Error executing code: {str(exc)}"

        after = project_tree_snapshot(project_root)
        recovered = recover_new_project_files(
            project_root, before, after, context.artifact_directory
        )
        mutations = shared_project_mutations(before, after)
        if recovered:
            logger.warning(
                "Run %s wrote into shared project %s; recovered files: %s",
                context.run_id,
                context.project_id,
                ", ".join(recovered),
            )
        if mutations:
            mutation_summary = ", ".join(mutations[:20])
            logger.error(
                "Run %s mutated shared project %s: %s",
                context.run_id,
                context.project_id,
                mutation_summary,
            )
            output += (
                "\nError: generated Python changed shared project source content: "
                f"{mutation_summary}. Generated Python execution is not sandboxed."
            )
        return output or "Execution completed successfully with no output."


def load_skill_prompt(project_id: str, skill_name: str) -> str:
    project = get_project(project_id)
    project_root = Path(project["path"])
    skill_path = project_root / "skills" / skill_name / "SKILL.md"
    references_dir = project_root / "skills" / skill_name / "references"

    prompt = (
        f"You are an advanced specialist worker executing tasks for the skill '{skill_name}'.\n"
        f"The selected project is '{project['name']}'. Its shared source root is: {project_root}.\n"
        "Use Deep Agents filesystem tools with absolute virtual paths such as '/data/file.csv' when reading or writing project files.\n"
    )

    if skill_path.exists():
        prompt += f"\n--- Skill Guidelines ---\n{skill_path.read_text(encoding='utf-8')}\n"

    if references_dir.exists():
        for ref_path in sorted(references_dir.iterdir()):
            if ref_path.is_file() and ref_path.suffix.lower() in {".json", ".md", ".txt", ".csv"}:
                try:
                    prompt += (
                        f"\n--- Reference: {ref_path.relative_to(project_root).as_posix()} ---\n"
                        f"{ref_path.read_text(encoding='utf-8')}\n"
                    )
                except UnicodeDecodeError:
                    continue

    prompt += (
        "\nCore Objective: Analyze the task instructions, formulate python code to explore data or plot charts, "
        "run the code using the execute_python tool, and present the final answer to the user. "
        "The execute_python tool runs in a private per-user, per-session workspace where 'data/<filename>' "
        "is a read-through link to the selected project's shared data. Never write generated content into the "
        "shared project root. Before creating any generated file, call get_project_context and save the file "
        "inside the returned artifact_directory. Relative files created by execute_python are also collected "
        "into that run's artifact directory. Create each requested deliverable once; if you revise a chart or export, "
        "overwrite the same artifact rather than creating alternate versions. Do not include generated filenames, "
        "artifact_directory values, absolute paths, empty artifact placeholder sections, or separate file-path listings "
        "in the final answer. Describe the visualization's analytical meaning in prose instead. The server will register generated files and "
        "present them in a private authenticated Generated artifacts section automatically."
    )
    return prompt


def get_supervisor_system_prompt(project_id: str) -> str:
    project = get_project(project_id)
    return (
        "You are a supervisor coordinator. Your goal is to analyze the user prompt and coordinate the plan.\n"
        "Use the Deep Agents skills library and task tool for the currently selected project to decide which specialist "
        f"capability applies. The selected project is '{project['name']}'.\n\n"
        "Core Objective: Analyze the user's request. Create a plan, delegate substantive analysis to the best "
        "project-specific subagent using the task tool, synthesize the subagent result, and present the final answer. "
        "Delegate each requested deliverable once and tell the specialist to return analytical findings, not only a file path. "
        "Never repeat generated filenames, artifact directories, absolute paths, or path-only visualization placeholders "
        "from a specialist response. Describe the visualization's meaning in prose; the server attaches every retained "
        "download in the authenticated Generated artifacts section."
    )


def get_llm_instance():
    from langchain_openai import ChatOpenAI
    from portkey_ai import PORTKEY_GATEWAY_URL, createHeaders

    portkey_api_key = os.environ.get("PORTKEY_API_KEY")
    provider_slug = os.environ.get("PORTKEY_PROVIDER_SLUG", "google-ai-studio")
    model_name = get_app_settings()["default_model"]

    temp_env = os.environ.get("TEMPERATURE")
    if temp_env is not None:
        try:
            temperature = float(temp_env)
        except ValueError:
            temperature = 0.0
    else:
        temperature = 1.0 if "gpt-5" in model_name else 0.0

    headers = createHeaders(api_key=portkey_api_key, provider=provider_slug)
    return ChatOpenAI(
        model=f"@{provider_slug}/{model_name}" if not model_name.startswith("@") else model_name,
        temperature=temperature,
        base_url=PORTKEY_GATEWAY_URL,
        default_headers=headers,
        api_key=portkey_api_key,
    )


def build_skill_subagents(
    project_id: str,
    skills_source: List[Any],
    tools: List[Any],
) -> List[Dict[str, Any]]:
    subagents: List[Dict[str, Any]] = []
    for skill in scan_project_skills(project_id):
        skill_name = skill["name"]
        subagents.append(
            {
                "name": skill_name,
                "description": skill["description"],
                "system_prompt": load_skill_prompt(project_id, skill_name),
                "tools": tools,
                "skills": skills_source,
            }
        )
    return subagents


def agent_run_config(user_id: str, project_id: str, session_id: str) -> Dict[str, Any]:
    return {
        "configurable": {
            "thread_id": session_thread_id(user_id, project_id, session_id)
        },
        "recursion_limit": 100,
    }


def response_text_from_agent_result(result: Dict[str, Any]) -> str:
    messages = result.get("messages", [])
    if messages:
        last_msg = messages[-1]
        if hasattr(last_msg, "content"):
            response_text = message_content_to_text(last_msg.content)
        elif isinstance(last_msg, dict):
            response_text = message_content_to_text(last_msg.get("content", ""))
        else:
            response_text = str(last_msg)
        if response_text.strip():
            return response_text

    return "The model returned an empty response for this request. Please try again or rephrase your prompt."


@dataclass
class AgentRuntime:
    graph: Any
    checkpointer: Any
    connection: sqlite3.Connection


_agent_graphs: Dict[str, AgentRuntime] = {}
_agent_graph_lock = threading.RLock()


def invalidate_project_agent(project_id: str) -> None:
    with _agent_graph_lock:
        runtime = _agent_graphs.pop(project_id, None)
    if runtime:
        try:
            runtime.connection.close()
        except Exception as exc:
            logger.warning("Could not close checkpoint connection for %s: %s", project_id, exc)


def invalidate_all_agents() -> None:
    for project_id in list(_agent_graphs):
        invalidate_project_agent(project_id)


def delete_checkpoint_thread(thread_id: str) -> None:
    if not AGENT_CHECKPOINT_DB_PATH.exists():
        return
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(AGENT_CHECKPOINT_DB_PATH, timeout=10, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        saver = SqliteSaver(conn)
        saver.setup()
        saver.delete_thread(thread_id)
    finally:
        conn.close()


def get_agent_graph(project_id: str):
    with _agent_graph_lock:
        cached = _agent_graphs.get(project_id)
        if cached:
            return cached.graph

    from langchain_core.tools import tool
    from deepagents import FilesystemPermission, create_deep_agent
    from deepagents.backends import FilesystemBackend
    from langgraph.checkpoint.sqlite import SqliteSaver

    project = get_project(project_id)
    project_root = Path(project["path"])
    backend = FilesystemBackend(root_dir=project_root, virtual_mode=True)
    skills = project_skills_source(project)
    permissions = [
        FilesystemPermission(**permission_spec)
        for permission_spec in project_filesystem_permission_specs()
    ]
    llm = get_llm_instance()

    @tool
    def execute_python(code: str) -> str:
        """Execute Python in this run's private workspace with project data linked at data/."""
        return execute_python_code(code, project_root)

    @tool
    def get_project_context() -> Dict[str, str]:
        """Return the selected project root and private output locations for this run."""
        context = CURRENT_RUN_CONTEXT.get()
        if context is None or context.project_id != project_id:
            raise RuntimeError("No active run context for the selected project")
        return {
            "project_name": project["name"],
            "project_root": str(project_root),
            **project_artifact_context(context),
        }

    specialist_tools = [execute_python, get_project_context]
    subagents = build_skill_subagents(project_id, skills, specialist_tools)

    AGENT_CHECKPOINT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(AGENT_CHECKPOINT_DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    checkpointer = SqliteSaver(conn)

    graph = create_deep_agent(
        model=llm,
        tools=[],
        skills=skills,
        backend=backend,
        permissions=permissions,
        subagents=subagents,
        system_prompt=get_supervisor_system_prompt(project_id),
        checkpointer=checkpointer,
    )
    runtime = AgentRuntime(graph=graph, checkpointer=checkpointer, connection=conn)
    with _agent_graph_lock:
        existing = _agent_graphs.get(project_id)
        if existing:
            conn.close()
            return existing.graph
        _agent_graphs[project_id] = runtime
    return graph


def run_agent(context: RunContext, prompt: str) -> str:
    if not os.environ.get("PORTKEY_API_KEY"):
        context_token = CURRENT_RUN_CONTEXT.set(context)
        try:
            return simulate_agent_response(context, prompt)
        finally:
            CURRENT_RUN_CONTEXT.reset(context_token)

    agent = get_agent_graph(context.project_id)
    context_token = CURRENT_RUN_CONTEXT.set(context)
    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=agent_run_config(
                context.user_id, context.project_id, context.session_id
            ),
        )
        return response_text_from_agent_result(result)
    finally:
        CURRENT_RUN_CONTEXT.reset(context_token)


def sse_event(event_type: str, data: Dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def chunk_parts(chunk: Any) -> tuple[tuple[str, ...], str, Any]:
    if isinstance(chunk, tuple) and len(chunk) == 3:
        namespace, mode, data = chunk
        return tuple(namespace or ()), str(mode), data
    if isinstance(chunk, tuple) and len(chunk) == 2:
        mode, data = chunk
        return (), str(mode), data
    if isinstance(chunk, dict) and "type" in chunk:
        return tuple(chunk.get("ns") or ()), str(chunk["type"]), chunk.get("data")
    return (), "unknown", chunk


def task_status_from_event(namespace: tuple[str, ...], data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None

    name = str(data.get("name", ""))
    if not name:
        return None

    is_subagent = bool(namespace)
    if "input" in data and name == "model":
        return "A project specialist is analyzing the request…" if is_subagent else "Analyzing the request…"
    if "input" in data and name == "tools":
        return "A project specialist is working with the data…" if is_subagent else "Working with project data…"
    if "input" in data and name == "task":
        return "Delegating to a project specialist…"
    if "result" in data and name == "task":
        return "Reviewing the specialist’s results…"
    return None


def delta_from_message_event(namespace: tuple[str, ...], data: Any) -> str:
    if namespace or not isinstance(data, tuple) or len(data) != 2:
        return ""

    message, metadata = data
    if isinstance(metadata, dict) and metadata.get("langgraph_node") != "model":
        return ""

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


def final_text_from_update(namespace: tuple[str, ...], data: Any) -> str:
    if namespace or not isinstance(data, dict):
        return ""

    for update in data.values():
        if not isinstance(update, dict):
            continue
        messages = update.get("messages")
        if not messages:
            continue
        text = message_content_to_text(messages[-1].content if hasattr(messages[-1], "content") else messages[-1])
        if text.strip():
            return text
    return ""


def run_activity_event(context: RunContext, message: str) -> str:
    update_task_run_activity(context.run_id, message)
    return sse_event(
        "status",
        {
            "message": message,
            "project_id": context.project_id,
            "session_id": context.session_id,
            "run_id": context.run_id,
        },
    )


def stream_agent_events(context: RunContext, prompt: str) -> Iterator[str]:
    started_at = time.monotonic()
    final_text = ""
    delta_parts: List[str] = []
    emitted_delta = False
    initial_status = "Preparing the agent workspace…"
    try:
        yield sse_event(
            "run",
            {
                "project_id": context.project_id,
                "session_id": context.session_id,
                "run_id": context.run_id,
                "status": initial_status,
            },
        )
        if not os.environ.get("PORTKEY_API_KEY"):
            yield run_activity_event(context, "Preparing a local response…")
            context_token = CURRENT_RUN_CONTEXT.set(context)
            try:
                final_text = simulate_agent_response(context, prompt)
            finally:
                CURRENT_RUN_CONTEXT.reset(context_token)
        else:
            agent = get_agent_graph(context.project_id)
            yield run_activity_event(context, initial_status)
            agent_events = iter(
                agent.stream(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config=agent_run_config(
                        context.user_id, context.project_id, context.session_id
                    ),
                    stream_mode=["updates", "messages", "tasks"],
                    subgraphs=True,
                )
            )
            while True:
                context_token = CURRENT_RUN_CONTEXT.set(context)
                try:
                    chunk = next(agent_events)
                except StopIteration:
                    break
                finally:
                    CURRENT_RUN_CONTEXT.reset(context_token)
                namespace, mode, data = chunk_parts(chunk)

                if mode == "tasks":
                    status = task_status_from_event(namespace, data)
                    if status:
                        yield run_activity_event(context, status)
                    continue

                if mode == "messages":
                    delta = delta_from_message_event(namespace, data)
                    if delta:
                        emitted_delta = True
                        delta_parts.append(delta)
                        # Model tokens can contain internal artifact paths before
                        # the files have been registered. Keep the user-facing
                        # activity stream live, but wait for the sanitized final
                        # response before exposing model text.
                    continue

                if mode == "updates":
                    update_text = final_text_from_update(namespace, data)
                    if update_text:
                        final_text = update_text

        if not final_text and delta_parts:
            final_text = "".join(delta_parts)
        if not final_text:
            final_text = "The model returned an empty response for this request. Please try again or rephrase your prompt."

        yield run_activity_event(context, "Preparing generated files…")
        artifacts = register_run_artifacts(context)
        yield run_activity_event(context, "Finalizing the response…")
        response_text = prepare_response_artifacts(
            context.user_id, context.project_id, final_text, artifacts
        )
        add_chat_message(
            context.user_id,
            context.project_id,
            context.session_id,
            "assistant",
            response_text,
            context.run_id,
        )
        finish_task_run(context.run_id, "completed")
        if not emitted_delta:
            yield sse_event("delta", {"text": response_text})
        yield sse_event(
            "final",
            {
                "response": response_text,
                "project_id": context.project_id,
                "session_id": context.session_id,
                "run_id": context.run_id,
                "duration_seconds": round(time.monotonic() - started_at, 3),
            },
        )
    except GeneratorExit:
        finish_task_run(context.run_id, "cancelled")
        raise
    except Exception as exc:
        logger.exception("Agent run %s failed", context.run_id)
        error_text = "The agent run failed. Check the server logs using the run ID and try again."
        finish_task_run(context.run_id, "failed", str(exc)[:1000])
        add_chat_message(
            context.user_id,
            context.project_id,
            context.session_id,
            "assistant",
            error_text,
            context.run_id,
        )
        yield sse_event("error", {"message": error_text})
    finally:
        cleanup_run_work(context)


def simulate_agent_response(context: RunContext, prompt: str) -> str:
    project = get_project(context.project_id)
    project_root = Path(project["path"])
    p_lower = prompt.lower()

    if project["id"] == "hpi-analytics" and "california" in p_lower and "growth" in p_lower:
        code = """
import pandas as pd
df = pd.read_csv('data/hpi_master.csv', dtype={'note': str, 'place_id': str}, low_memory=False)
ca = df[(df['level'] == 'State') & (df['place_id'] == 'CA') & (df['hpi_type'] == 'traditional') & (df['frequency'] == 'quarterly')]
v1 = ca[(ca['yr'] == 2010) & (ca['period'] == 1)]['index_nsa'].values[0]
v2 = ca[(ca['yr'] == 2020) & (ca['period'] == 1)]['index_nsa'].values[0]
print(f"VALS: {v1}, {v2}, {((v2-v1)/v1)*100:.2f}%")
"""
        out = execute_python_code(code, project_root)
        return (
            "Based on the HPI dataset, California (CA) experienced a growth of approximately "
            f"**79.40%** between Q1 2010 and Q1 2020.\n\n```\n{out}\n```"
        )

    if project["id"] == "hpi-analytics" and ("plot" in p_lower or "chart" in p_lower):
        chart_id = uuid.uuid4().hex[:6]
        chart_path = context.artifact_directory / f"hpi_{chart_id}.png"
        code = f"""
import pandas as pd
import matplotlib.pyplot as plt
df = pd.read_csv('data/hpi_master.csv', dtype={{'note': str, 'place_id': str}}, low_memory=False)
plt.figure(figsize=(8, 4))
plt.grid(True, linestyle='--', alpha=0.5)
for s in ['CA', 'NY', 'TX']:
    sdf = df[(df['level'] == 'State') & (df['place_id'] == s) & (df['frequency'] == 'quarterly') & (df['yr'] >= 2015)].sort_values(['yr', 'period'])
    plt.plot(sdf['yr'] + (sdf['period']-1)/4.0, sdf['index_nsa'], label=s, linewidth=2)
plt.title("HPI Comparison (2015 - Present)")
plt.legend()
plt.tight_layout()
plt.savefig(r'{chart_path}', dpi=150)
"""
        execute_python_code(code, project_root)
        return (
            "Here is the housing price index comparison chart for CA, NY, and TX from 2015 to present:\n\n"
            f"![HPI Comparison Chart]({chart_path})"
        )

    return (
        f"I received your request for project '{project['name']}': '{prompt}'. "
        "LLM API key is not set, so this is a local test response."
    )


def ensure_no_active_project_runs(project_id: str) -> None:
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count FROM task_runs
            WHERE project_id = ? AND status = 'running'
            """,
            (project_id,),
        ).fetchone()
    if row and row["count"]:
        raise HTTPException(
            status_code=409,
            detail="Project content cannot change while tasks are running",
        )


def cleanup_upload_preview(token: str, zip_path: Path) -> None:
    zip_path.unlink(missing_ok=True)
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM upload_previews WHERE token_hash = ?",
            (upload_token_hash(token),),
        )


def reset_project_contents_transactional(project_root: Path) -> None:
    project_root = ensure_child_path(get_projects_dir(), project_root)
    staging = ensure_child_path(
        get_projects_dir(),
        project_root.parent / f".{project_root.name}.empty-{uuid.uuid4().hex}",
    )
    previous = ensure_child_path(
        get_projects_dir(),
        project_root.parent / f".{project_root.name}.previous-{uuid.uuid4().hex}",
    )
    try:
        staging.mkdir(parents=True)
        (staging / "data").mkdir()
        (staging / "skills").mkdir()
        project_root.rename(previous)
        staging.rename(project_root)
        shutil.rmtree(previous)
    except Exception:
        if project_root.exists() and previous.exists():
            shutil.rmtree(project_root)
        if previous.exists() and not project_root.exists():
            previous.rename(project_root)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def delete_project_resources(project_id: str, project_root: Path) -> None:
    ensure_no_active_project_runs(project_id)
    with get_db_connection() as conn:
        sessions = conn.execute(
            "SELECT id, user_id, thread_id FROM chat_sessions WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        conn.execute(
            "UPDATE projects SET status = 'deleting', updated_at = ? WHERE id = ?",
            (utc_now(), project_id),
        )

    invalidate_project_agent(project_id)
    try:
        for session in sessions:
            delete_session_resources(
                session["user_id"], project_id, session["id"], session["thread_id"]
            )
        for user_dir in ARTIFACTS_DIR.iterdir() if ARTIFACTS_DIR.exists() else []:
            candidate = user_dir / project_id
            try:
                candidate = ensure_child_path(ARTIFACTS_DIR, candidate)
            except HTTPException:
                continue
            if candidate.exists():
                shutil.rmtree(candidate)
        if project_root.exists():
            shutil.rmtree(project_root)
        with get_db_connection() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    except Exception:
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE projects SET status = 'active', updated_at = ? WHERE id = ?",
                (utc_now(), project_id),
            )
        raise


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
        "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
        "img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "x-fnma-jws-token, Cookie"
    return response


@app.get("/")
def get_index():
    return FileResponse(STATIC_DIR / "index.html")


def current_user_response(user: CurrentUser) -> Dict[str, Any]:
    return {
        "user": {
            "id": user.id,
            "subject": user.subject,
            "roles": sorted(user.roles),
            "is_project_admin": user.is_project_admin,
        }
    }


@app.get("/api/auth/config")
def api_auth_config(
    request: Request,
    x_fnma_jws_token: Optional[str] = Header(default=None, alias="x-fnma-jws-token"),
):
    cdx_header_present = bool(x_fnma_jws_token and x_fnma_jws_token.strip())
    result: Dict[str, Any] = {
        "development_login_enabled": DEVELOPMENT_LOGIN_ENABLED,
        "cdx_header_present": cdx_header_present,
    }
    if DEVELOPMENT_LOGIN_ENABLED and not cdx_header_present:
        development_token = request.cookies.get(DEVELOPMENT_IDENTITY_COOKIE)
        if development_token:
            try:
                identity = decode_development_token(
                    development_token,
                    DEVELOPMENT_SIGNING_SECRET,
                )
                result["development_identity"] = {
                    "subject": identity.subject,
                    "project_admin": identity.is_project_admin,
                }
            except AuthenticationError:
                pass
    return result


@app.post("/api/auth/development-login")
def api_development_login(
    login: DevelopmentLoginRequest,
    request: Request,
    response: Response,
):
    if not DEVELOPMENT_LOGIN_ENABLED:
        raise HTTPException(status_code=404, detail="Development login is disabled")

    subject = login.subject.strip()
    if not subject:
        raise HTTPException(status_code=422, detail="User subject is required")
    roles = [PROJECT_ADMIN_ROLE] if login.project_admin else []
    token = create_development_token(
        subject,
        roles,
        DEVELOPMENT_SIGNING_SECRET,
        DEVELOPMENT_IDENTITY_LIFETIME_SECONDS,
    )
    response.set_cookie(
        DEVELOPMENT_IDENTITY_COOKIE,
        token,
        max_age=DEVELOPMENT_IDENTITY_LIFETIME_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    identity = decode_development_token(token, DEVELOPMENT_SIGNING_SECRET)
    init_db()
    return current_user_response(upsert_current_user(identity))


@app.get("/api/auth/me")
def api_auth_me(user: CurrentUser = Depends(get_current_user)):
    return current_user_response(user)


@app.get("/api/settings")
def api_get_settings(_: CurrentUser = Depends(get_current_user)):
    return {"settings": get_app_settings()}


@app.put("/api/settings")
def api_update_settings(
    request: SettingsUpdateRequest,
    user: CurrentUser = Depends(require_project_admin),
):
    settings = save_app_settings(
        request.default_model.strip(),
        request.max_sessions_per_project,
    )
    add_audit_event(user.id, "settings.update")
    return {"settings": settings}


@app.get("/api/projects")
def api_list_projects(_: CurrentUser = Depends(get_current_user)):
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM projects WHERE status = 'active' ORDER BY updated_at DESC, name ASC"
        ).fetchall()
    return {"projects": [public_project(row_to_project(row)) for row in rows]}


@app.post("/api/projects")
def api_create_project(
    request: ProjectCreateRequest,
    user: CurrentUser = Depends(require_project_admin),
):
    project = create_project_record(request.name, user.id)
    validate_project_skills(project["id"])
    add_audit_event(user.id, "project.create", project["id"], {"name": project["name"]})
    return {"project": public_project(project)}


@app.put("/api/projects/{project_id}")
def api_update_project(
    project_id: str,
    request: ProjectUpdateRequest,
    user: CurrentUser = Depends(require_project_admin),
):
    get_project(project_id)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name cannot be empty")
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
            (name, utc_now(), project_id),
        )
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    invalidate_project_agent(project_id)
    add_audit_event(user.id, "project.update", project_id, {"name": name})
    return {"project": public_project(row_to_project(row))}


@app.get("/api/projects/{project_id}")
def api_get_project(project_id: str, _: CurrentUser = Depends(get_current_user)):
    project = get_project(project_id)
    project["skills"] = scan_project_skills(project_id)
    return {"project": public_project(project)}


@app.get("/api/projects/{project_id}/isolation")
def api_project_isolation(
    project_id: str,
    user: CurrentUser = Depends(require_project_admin),
):
    return {"audit": project_isolation_audit(user.id, project_id)}


@app.delete("/api/projects/{project_id}")
def api_delete_project(
    project_id: str,
    user: CurrentUser = Depends(require_project_admin),
):
    project = get_project(project_id)
    project_root = ensure_child_path(get_projects_dir(), Path(project["path"]))
    add_audit_event(
        user.id,
        "project.delete.requested",
        project_id,
        {"name": project["name"]},
    )
    delete_project_resources(project_id, project_root)
    return {"deleted": True}


@app.get("/api/projects/{project_id}/contents")
def api_project_contents(project_id: str, _: CurrentUser = Depends(get_current_user)):
    project_root = get_project_root(project_id)
    return {"items": list_project_files(project_root)}


@app.delete("/api/projects/{project_id}/contents")
def api_delete_project_contents(
    project_id: str,
    user: CurrentUser = Depends(require_project_admin),
):
    ensure_no_active_project_runs(project_id)
    project_root = get_project_root(project_id)
    reset_project_contents_transactional(project_root)
    touch_project(project_id, bump_revision=True)
    invalidate_project_agent(project_id)
    add_audit_event(user.id, "project.contents.delete", project_id)
    return {"deleted": True}


@app.post("/api/uploads/preview")
async def api_upload_preview(
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_project_admin),
):
    token = uuid.uuid4().hex
    user_upload_dir = ensure_child_path(TMP_UPLOADS_DIR, TMP_UPLOADS_DIR / user.id)
    user_upload_dir.mkdir(parents=True, exist_ok=True)
    zip_path = ensure_child_path(user_upload_dir, user_upload_dir / f"{uuid.uuid4().hex}.zip")
    total = 0
    try:
        with zip_path.open("wb") as dst:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 250 * 1024 * 1024:
                    raise HTTPException(status_code=400, detail="ZIP upload exceeds 250MB limit")
                dst.write(chunk)
        preview = inspect_zip(zip_path)
        record_upload_preview(user.id, token, zip_path)
    except Exception:
        zip_path.unlink(missing_ok=True)
        raise
    return {"upload_token": token, **preview}


@app.post("/api/projects/import")
def api_import_project(
    request: ProjectImportRequest,
    user: CurrentUser = Depends(require_project_admin),
):
    zip_path = upload_path_for_token(user.id, request.upload_token, consume=True)
    project: Optional[Dict[str, Any]] = None
    try:
        project = create_project_record(request.name, user.id)
        extract_zip(zip_path, Path(project["path"]), request.mode)
        touch_project(project["id"], bump_revision=True)
        validate_project_skills(project["id"])
        invalidate_project_agent(project["id"])
        add_audit_event(user.id, "project.import", project["id"], {"mode": request.mode})
        return {"project": public_project(get_project(project["id"]))}
    except Exception:
        if project:
            project_path = Path(project["path"])
            if project_path.exists():
                shutil.rmtree(project_path)
            with get_db_connection() as conn:
                conn.execute("DELETE FROM projects WHERE id = ?", (project["id"],))
        raise
    finally:
        cleanup_upload_preview(request.upload_token, zip_path)


@app.post("/api/projects/{project_id}/contents/import")
def api_import_project_contents(
    project_id: str,
    request: ContentImportRequest,
    user: CurrentUser = Depends(require_project_admin),
):
    ensure_no_active_project_runs(project_id)
    project_root = get_project_root(project_id)
    zip_path = upload_path_for_token(user.id, request.upload_token, consume=True)
    try:
        extract_zip(zip_path, project_root, request.mode)
        touch_project(project_id, bump_revision=True)
        validate_project_skills(project_id)
        invalidate_project_agent(project_id)
        add_audit_event(user.id, "project.contents.import", project_id, {"mode": request.mode})
        return {
            "project": public_project(get_project(project_id)),
            "items": list_project_files(project_root),
        }
    finally:
        cleanup_upload_preview(request.upload_token, zip_path)


@app.get("/api/projects/{project_id}/media/{media_path:path}")
def api_project_media(
    project_id: str,
    media_path: str,
    _: CurrentUser = Depends(get_current_user),
):
    project_root = get_project_root(project_id)
    file_path = relative_project_path(project_root, media_path)
    if file_path.suffix.lower() not in MEDIA_EXTENSIONS or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Project media not found")
    return FileResponse(file_path)


@app.get("/api/projects/{project_id}/sessions")
def api_list_sessions(project_id: str, user: CurrentUser = Depends(get_current_user)):
    get_project(project_id)
    return {"sessions": list_project_sessions(user.id, project_id)}


@app.post("/api/projects/{project_id}/sessions")
def api_create_session(
    project_id: str,
    request: SessionCreateRequest,
    user: CurrentUser = Depends(get_current_user),
):
    session = create_session(user.id, project_id, request.title)
    return {
        "session": session,
        "sessions": list_project_sessions(user.id, project_id),
    }


@app.get("/api/projects/{project_id}/sessions/{session_id}")
def api_get_session(
    project_id: str,
    session_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    session = get_session(user.id, project_id, session_id)
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT cm.id, cm.role, cm.content, cm.created_at, cm.run_id,
                   tr.created_at AS run_created_at,
                   tr.completed_at AS run_completed_at
            FROM chat_messages AS cm
            LEFT JOIN task_runs AS tr ON tr.id = cm.run_id
            WHERE cm.user_id = ? AND cm.project_id = ? AND cm.session_id = ?
            ORDER BY cm.id ASC
            """,
            (user.id, project_id, session_id),
        ).fetchall()
        artifact_rows = conn.execute(
            """
            SELECT * FROM artifacts
            WHERE user_id = ? AND project_id = ? AND session_id = ?
            ORDER BY created_at ASC
            """,
            (user.id, project_id, session_id),
        ).fetchall()
    artifacts_by_run: Dict[str, List[Dict[str, Any]]] = {}
    for artifact_row in artifact_rows:
        artifact = dict(artifact_row)
        artifacts_by_run.setdefault(artifact["run_id"], []).append(artifact)
    messages = []
    for row in rows:
        message = dict(row)
        if message["role"] == "assistant":
            message["content"] = prepare_response_artifacts(
                user.id,
                project_id,
                message["content"],
                artifacts_by_run.get(message["run_id"], []),
            )
            message["duration_seconds"] = duration_seconds_between(
                message.pop("run_created_at"), message.pop("run_completed_at")
            )
        else:
            message.pop("run_created_at")
            message.pop("run_completed_at")
        messages.append(message)
    return {"session": session, "messages": messages}


@app.get("/api/projects/{project_id}/sessions/{session_id}/runs")
def api_list_session_runs(
    project_id: str,
    session_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    get_session(user.id, project_id, session_id)
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, status, latest_status, project_revision, created_at, completed_at
            FROM task_runs
            WHERE user_id = ? AND project_id = ? AND session_id = ?
            ORDER BY created_at DESC
            """,
            (user.id, project_id, session_id),
        ).fetchall()
    return {"runs": [dict(row) for row in rows]}


@app.delete("/api/projects/{project_id}/sessions/{session_id}")
def api_delete_session(
    project_id: str,
    session_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    session = get_session(user.id, project_id, session_id)
    delete_session_resources(user.id, project_id, session_id, session["thread_id"])
    return {"deleted": True}


@app.get("/api/artifacts/{artifact_id}")
def api_get_artifact(
    artifact_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE id = ? AND user_id = ?",
            (artifact_id, user.id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Artifact not found")
    file_path = ensure_child_path(ARTIFACTS_DIR, ARTIFACTS_DIR / row["storage_key"])
    if not file_path.is_file() or file_path.is_symlink():
        raise HTTPException(status_code=404, detail="Artifact file not found")
    return FileResponse(
        file_path,
        media_type=row["media_type"],
        filename=row["display_name"],
    )


@app.post("/api/chat", response_model=ChatResponse)
def api_chat(request: ChatRequest, user: CurrentUser = Depends(get_current_user)):
    started_at = time.monotonic()
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(request.message) > 100_000:
        raise HTTPException(status_code=400, detail="Message exceeds 100,000 characters")
    get_project(request.project_id)

    session_id = request.session_id
    if session_id:
        get_session(user.id, request.project_id, session_id)
    else:
        session_id = create_session(user.id, request.project_id)["id"]

    maybe_title_session(user.id, request.project_id, session_id, request.message)
    run = create_task_run(user.id, request.project_id, session_id, request.message)
    context: RunContext = run["context"]
    add_chat_message(
        user.id, request.project_id, session_id, "user", request.message, run["id"]
    )
    try:
        response_text = run_agent(context, request.message)
        artifacts = register_run_artifacts(context)
        response_text = prepare_response_artifacts(
            user.id, request.project_id, response_text, artifacts
        )
        add_chat_message(
            user.id,
            request.project_id,
            session_id,
            "assistant",
            response_text,
            run["id"],
        )
        finish_task_run(run["id"], "completed")
        return ChatResponse(
            response=response_text,
            project_id=request.project_id,
            session_id=session_id,
            run_id=run["id"],
            duration_seconds=round(time.monotonic() - started_at, 3),
        )
    except Exception as exc:
        logger.exception("Agent run %s failed", run["id"])
        finish_task_run(run["id"], "failed", str(exc)[:1000])
        raise HTTPException(
            status_code=500,
            detail=f"Agent run failed; reference run ID {run['id']}",
        ) from exc
    finally:
        cleanup_run_work(context)


@app.post("/api/chat/stream")
def api_chat_stream(request: ChatRequest, user: CurrentUser = Depends(get_current_user)):
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(request.message) > 100_000:
        raise HTTPException(status_code=400, detail="Message exceeds 100,000 characters")
    get_project(request.project_id)

    session_id = request.session_id
    if session_id:
        get_session(user.id, request.project_id, session_id)
    else:
        session_id = create_session(user.id, request.project_id)["id"]

    maybe_title_session(user.id, request.project_id, session_id, request.message)
    run = create_task_run(user.id, request.project_id, session_id, request.message)
    context: RunContext = run["context"]
    add_chat_message(
        user.id, request.project_id, session_id, "user", request.message, run["id"]
    )

    return StreamingResponse(
        stream_agent_events(context, request.message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Accel-Buffering": "no",
        },
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
