"""Core application service: projects, identity, settings and skills.

This module also owns the module-level configuration the rest of the app reads
(storage roots, development login state) and re-exports the session, artifact
and runtime helpers at the bottom, so most callers can import one place.

The central invariant: project content is shared and unversioned per user, while
sessions, runs and artifacts are private. Anything reading private data filters
on user_id; anything mutating shared content requires PROJECT_ADMIN.
"""

import os
import re
import secrets
import uuid
import shutil
import sqlite3
import contextvars
import json
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import Depends, Header, HTTPException, Request


SDK_DIR = Path(__file__).resolve().parents[2]
BASE_DIR = SDK_DIR.parent
STATIC_DIR = SDK_DIR / "static"

if str(SDK_DIR) not in sys.path:
    sys.path.insert(0, str(SDK_DIR))

from deep_agents_app.security import (  # noqa: E402
    AuthenticationError,
    TokenIdentity,
    decode_cdx_token,
    decode_development_token,
)
from deep_agents_app.config import (  # noqa: E402
    ensure_child_path as config_ensure_child_path,
    load_runtime_paths,
)
from deep_agents_app.domain import CurrentUser, ProjectContext, RunContext  # noqa: E402
from deep_agents_app.db import connect, initialize_schema  # noqa: E402

logger = logging.getLogger(__name__)

_PROJECT_CONTENT_LOCKS: Dict[str, threading.RLock] = {}
_PROJECT_CONTENT_LOCKS_GUARD = threading.Lock()

_PROJECT_REPLICATION_LOCKS: Dict[str, threading.RLock] = {}
_PROJECT_REPLICATION_GUARD = threading.Lock()
_PROJECT_REPLICATION_PENDING: set = set()

load_dotenv(BASE_DIR / ".env")


def environment_flag(name: str, default: bool = False) -> bool:
    """Read a boolean setting, accepting 1/true/yes/on in any case."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def project_content_lock(project_id: str) -> threading.RLock:
    """Serialize content edits against run creation for one project.

    Prevents a run starting while files it may read are being replaced, and an
    edit landing while a run is in flight.
    """
    with _PROJECT_CONTENT_LOCKS_GUARD:
        return _PROJECT_CONTENT_LOCKS.setdefault(project_id, threading.RLock())


def project_replication_lock(project_id: str) -> threading.RLock:
    """Serialize replication of one project's data to the sandbox backend.

    Two syncs running at once could interleave uploads with the deletion of keys
    the other just wrote. Replication used to inherit this from the content lock
    by running inside it; now that it runs in the background it needs its own.

    Reentrant so the background worker can hold it across the bookkeeping that
    decides whether another sync still needs to be queued.
    """
    with _PROJECT_REPLICATION_GUARD:
        return _PROJECT_REPLICATION_LOCKS.setdefault(project_id, threading.RLock())


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
SANDBOX_DIR = RUNTIME_PATHS.sandbox_dir
ARTIFACTS_DIR = RUNTIME_PATHS.artifacts_dir
TMP_UPLOADS_DIR = RUNTIME_PATHS.uploads_dir


def load_or_create_development_signing_secret(database_dir: Path) -> str:
    """Load or create the key that signs development login cookies.

    Written with O_EXCL at mode 0600 so two workers starting together cannot
    each generate a different key and invalidate each other's cookies.
    """
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


CURRENT_RUN_CONTEXT: contextvars.ContextVar[Optional[RunContext]] = contextvars.ContextVar(
    "current_run_context",
    default=None,
)

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
_SKILLS_VALIDATED = False


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
    """The current time as an ISO-8601 UTC string, the format used throughout."""
    return datetime.now(timezone.utc).isoformat()


def duration_seconds_between(started_at: Optional[str], completed_at: Optional[str]) -> Optional[float]:
    """Elapsed seconds between two ISO timestamps, or None if either is absent."""
    if not started_at or not completed_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
        completed = datetime.fromisoformat(completed_at)
    except ValueError:
        return None
    return round(max(0.0, (completed - started).total_seconds()), 3)


def get_projects_dir() -> Path:
    """The shared projects root, created if it does not yet exist."""
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    return PROJECTS_ROOT.resolve()


def slugify(name: str) -> str:
    """Turn a display name into a filesystem- and URL-safe identifier."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or f"project-{uuid.uuid4().hex[:8]}"


def get_db_connection() -> sqlite3.Connection:
    """A configured connection to the application database."""
    return connect(DB_PATH)


def init_db() -> None:
    """Prepare the database and reconcile it with the projects on disk."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as conn:
        initialize_schema(conn)
        register_project_directories(conn)
    prune_expired_uploads()
    validate_all_project_skills_once()


def project_display_name(slug: str) -> str:
    """A human-readable name from a slug, keeping known acronyms uppercase."""
    acronyms = {"hpi": "HPI", "nmdb": "NMDB", "fhfa": "FHFA"}
    return " ".join(acronyms.get(part, part.capitalize()) for part in slug.split("-"))


def register_project_directories(conn: sqlite3.Connection) -> None:
    """Register every directory under the projects root as a project.

    Directories are the source of truth: dropping one in and restarting is a
    supported way to add a project.
    """
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
    """Find or create the local user row for an authenticated identity."""
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
    """Resolve the caller from the CDX header, or the development cookie.

    The FastAPI dependency behind every authenticated route. Raises 401 when
    neither is present or valid.
    """
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
    """Dependency for routes that mutate shared content. Raises 403 otherwise."""
    if not user.is_project_admin:
        raise HTTPException(status_code=403, detail="PROJECT_ADMIN role is required")
    return user


def add_audit_event(
    user_id: str,
    action: str,
    project_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Record who did what, for administrative actions."""
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_events (user_id, action, project_id, details, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, action, project_id, json.dumps(details or {}, sort_keys=True), utc_now()),
        )


def parse_available_models() -> List[str]:
    """The model allowlist from the environment."""
    raw = os.environ.get("AVAILABLE_MODELS", "")
    models = [model.strip() for model in raw.split(",") if model.strip()]
    return models or DEFAULT_AVAILABLE_MODELS.copy()


def env_default_model(available_models: List[str]) -> str:
    """The configured default model, falling back to the first allowed one."""
    configured = os.environ.get("DEFAULT_MODEL", "").strip()
    if configured and configured in available_models:
        return configured
    return available_models[0] if available_models else DEFAULT_AVAILABLE_MODELS[0]


def env_max_sessions_per_project() -> int:
    """The configured per-project session cap."""
    raw = os.environ.get("MAX_SESSIONS_PER_PROJECT", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_SESSIONS_PER_PROJECT
    return min(max(value, 1), 100)


def coerce_max_sessions(value: Any, fallback: int) -> int:
    """Read a stored cap, falling back when it is missing or invalid."""
    try:
        return min(max(int(value), 1), 100)
    except (TypeError, ValueError):
        return fallback


def bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """An integer setting clamped to a sane range, ignoring bad values."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def get_app_settings() -> Dict[str, Any]:
    """Current settings, seeded from the environment on first use."""
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
    """Persist settings, rejecting a model outside the allowlist."""
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
    """Delete the user's oldest conversations beyond the configured cap."""
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
    """Apply the session cap to every user, after the cap is lowered."""
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
    """The caller's conversations with any in-flight run surfaced for the UI."""
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
    """Map a project row to a dictionary, without its filesystem path."""
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
    """Strip internal fields so a project can be returned to the browser.

    Notably removes the filesystem path: server paths are never exposed.
    """
    return {
        key: value
        for key, value in project.items()
        if key not in {"path", "relative_path", "created_by"}
    }


def ensure_child_path(parent: Path, child: Path) -> Path:
    """Resolve `child` and assert it stays inside `parent`, as an HTTP error.

    The containment rule itself lives in config.ensure_child_path. This wrapper
    only translates its ValueError into the 400 the API layer expects, so the
    check cannot be reimplemented in two places and drift.
    """
    try:
        return config_ensure_child_path(parent, child)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Path escapes the project directory"
        ) from exc


def get_project(project_id: str) -> Dict[str, Any]:
    """Fetch an active project and resolve its root, or raise.

    Raises 404 when unknown and 409 while it is being deleted.
    """
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
    """The absolute path of a project's directory."""
    return Path(get_project(project_id)["path"])


def project_context(project_id: str) -> ProjectContext:
    """Assemble what the agent needs to know about a project, from the database.

    The one place the two halves meet. Everything here is a query the agent
    itself must not make, because it may be running where these tables are not.
    """
    project = get_project(project_id)
    return ProjectContext(
        id=project["id"],
        name=project["name"],
        slug=project["slug"],
        root=Path(project["path"]),
        content_revision=project["content_revision"],
        model=get_app_settings()[SETTING_DEFAULT_MODEL],
    )


def get_skill_dir(project_id: str) -> Path:
    """The skills directory inside a project."""
    return get_project_root(project_id) / "skills"


def project_skills_source(project: Dict[str, Any]) -> List[tuple[str, str]]:
    """The skill sources handed to the Deep Agents graph."""
    return [("/skills", project["name"])]


def project_filesystem_permission_specs() -> List[Dict[str, Any]]:
    """Filesystem rules for the agent's own file tools.

    These constrain the agent's built-in tools only. Generated Python is
    constrained by the sandbox instead, which is a separate mechanism.
    """
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
    """Build the context identifying a run and its artifact staging directory."""
    artifact_directory = ensure_child_path(
        ARTIFACTS_DIR,
        ARTIFACTS_DIR / user_id / project_id / session_id / run_id,
    )
    if create:
        artifact_directory.mkdir(parents=True, exist_ok=True)
    return RunContext(
        user_id=user_id,
        project_id=project_id,
        session_id=session_id,
        run_id=run_id,
        artifact_directory=artifact_directory,
    )


def project_artifact_context(context: RunContext) -> Dict[str, str]:
    """Artifact locations for a run, reported by the isolation audit."""
    return {
        "session_id": context.session_id,
        "run_id": context.run_id,
        "artifact_directory": str(context.artifact_directory),
        "artifact_url_note": "Artifact URLs are registered and added by the server after the run.",
    }


def project_isolation_audit(user_id: str, project_id: str) -> Dict[str, Any]:
    """Re-check the isolation invariants and report which sandbox is active.

    Intended as a deployment smoke test rather than a routine call.
    """
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
        "sandbox_backend": _sandbox_backend_name(),
        "checks": checks,
        "passed": all(checks.values()),
    }


def _sandbox_backend_name() -> str:
    from deep_agents_app.runtime.sandbox import configured_backend_name

    return configured_backend_name()


def replicate_project_data(project_id: str) -> None:
    """Push a project's content to wherever the agent and sandbox read it from.

    The whole project tree, since the agent's file tools and its skill prompts
    both read from it, not only the data the sandbox is hydrated with.

    A no-op for backends that read the project directory directly. Failures are
    logged rather than raised: the content change itself has already succeeded,
    and the next run re-checks the replica before using it.

    Synchronous. Callers on a request thread want schedule_project_replication.
    """
    from deep_agents_app.runtime.sandbox import get_backend  # circular at import time

    try:
        with project_replication_lock(project_id):
            project = get_project(project_id)
            backend = get_backend()
            changed = backend.sync_project_content(
                project["slug"],
                Path(project["path"]),
                project["content_revision"],
            )
        if changed:
            logger.info(
                "Replicated %s object(s) for project %s at revision %s",
                changed,
                project_id,
                project["content_revision"],
            )
    except Exception as exc:  # noqa: BLE001 - never fail an edit over replication
        logger.error(
            "Could not replicate project %s to the sandbox backend: %s. "
            "The next run will retry before executing anything.",
            project_id,
            exc,
        )


def schedule_project_replication(project_id: str) -> None:
    """Replicate a project's data in the background.

    Content edits hold the project content lock, which also gates run creation,
    and replication can mean listing and uploading an entire dataset. Doing that
    inline blocked every run in the project, and the user's own request, for as
    long as the transfer took.

    Correctness does not depend on this finishing, or starting: a run verifies
    the replica's revision before using it and syncs inline if it is stale. This
    is a pre-warm, so an interrupted one costs a slower first prompt, nothing
    more.

    Edits are coalesced. A sync reads the project's current revision when it
    runs rather than the one that scheduled it, so a single queued pass covers
    every edit made before it starts, and a burst of file operations leaves at
    most one sync running and one waiting.
    """
    with _PROJECT_REPLICATION_GUARD:
        if project_id in _PROJECT_REPLICATION_PENDING:
            return
        _PROJECT_REPLICATION_PENDING.add(project_id)

    def worker() -> None:
        try:
            with project_replication_lock(project_id):
                # Cleared once this pass is the one running, so edits arriving
                # behind it queue exactly one successor rather than a thread each.
                with _PROJECT_REPLICATION_GUARD:
                    _PROJECT_REPLICATION_PENDING.discard(project_id)
                replicate_project_data(project_id)
        except Exception:  # noqa: BLE001 - a background thread must not escape
            logger.exception("Background replication failed for project %s", project_id)
            with _PROJECT_REPLICATION_GUARD:
                _PROJECT_REPLICATION_PENDING.discard(project_id)

    threading.Thread(
        target=worker, name=f"replicate-{project_id}", daemon=True
    ).start()


def touch_project(project_id: str, bump_revision: bool = False) -> None:
    """Mark a project as updated, optionally bumping its content revision.

    Bumping the revision means the sandbox's copy of the data is stale, so this
    triggers replication.
    """
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
    if bump_revision:
        schedule_project_replication(project_id)


def record_project_content_mutation(
    user_id: str,
    project_id: str,
    action: str,
    details: Dict[str, Any],
) -> None:
    """Atomically bump the content revision, audit it, and replicate.

    The path individual file and folder edits take. touch_project covers bulk
    operations; both must trigger replication.
    """
    now = utc_now()
    with get_db_connection() as conn:
        updated = conn.execute(
            """
            UPDATE projects
            SET updated_at = ?, content_revision = content_revision + 1
            WHERE id = ? AND status = 'active'
            """,
            (now, project_id),
        )
        if updated.rowcount != 1:
            raise HTTPException(status_code=404, detail="Project not found")
        conn.execute(
            """
            INSERT INTO audit_events (user_id, action, project_id, details, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, action, project_id, json.dumps(details, sort_keys=True), now),
        )
    # Individual file and folder edits bump the revision here rather than through
    # touch_project, so the replication hook has to cover both paths. Dispatched
    # rather than run inline: this call sits inside the project content lock,
    # which also gates run creation.
    schedule_project_replication(project_id)


def create_project_record(name: str, created_by: str) -> Dict[str, Any]:
    """Create a project row and its directory skeleton."""
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
    """Resolve a user-supplied path inside a project, refusing escapes."""
    if not user_path or user_path in {".", "/"}:
        return project_root
    normalized = user_path.replace("\\", "/").lstrip("/")
    candidate = project_root / normalized
    return ensure_child_path(project_root, candidate)


def parse_skill_frontmatter(skill_md: Path) -> Dict[str, str]:
    """Read the name and description from a SKILL.md front matter block."""
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
    """Check one skill's front matter and that its name matches its folder."""
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
    """Validate every skill in a project, raising on the first problem.

    Run before publishing a content change, so a malformed skill is rejected at
    edit time rather than surfacing as a broken agent later.
    """
    skills_dir = get_skill_dir(project_id)
    if not skills_dir.exists():
        return

    for skill_dir in sorted(skills_dir.iterdir()):
        if skill_dir.is_dir():
            validate_skill_metadata(skill_dir)


def validate_all_project_skills_once() -> None:
    """Validate all projects once per process, at startup."""
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
    """Discover a project's skills and their descriptions."""
    return scan_skills(get_skill_dir(project_id))


def scan_skills(skills_dir: Path) -> List[Dict[str, str]]:
    """Discover skills in a directory, without needing to know whose it is.

    Split out from scan_project_skills so the agent can read skills from a
    directory hydrated out of object storage, where there is no project row to
    look the path up from.
    """
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
    """List a project's files for the agent's file picker."""
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


def reset_project_contents_transactional(project_root: Path) -> None:
    """Empty a project by swapping in a fresh directory.

    Builds the replacement beside the original and moves it into place, so an
    interrupted reset leaves the existing content intact.
    """
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
    """Delete a project's row, files and every session that referenced it."""
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

from deep_agents_app.services.artifacts import (  # noqa: E402, F401
    append_artifact_links,
    prepare_response_artifacts,
    register_run_artifacts,
)


from deep_agents_app.services.sessions import (  # noqa: E402, F401
    add_chat_message,
    create_session,
    create_task_run,
    delete_session_resources,
    ensure_no_active_project_runs,
    fail_abandoned_runs,
    finish_task_run,
    get_session,
    maybe_title_session,
    release_owned_runs,
    session_thread_id,
    start_run_heartbeat,
    touch_run_heartbeat,
    update_task_run_activity,
)


from deep_agents_app.runtime.engine import (  # noqa: E402, F401
    execute_python_code,
    invalidate_project_agent,
    invalidate_all_agents,
    preflight_model_gateway,
    task_status_from_event,
    stream_agent_events,
)

from deep_agents_app.services.uploads import (  # noqa: E402, F401
    cleanup_upload_preview,
    extract_zip,
    inspect_zip,
    prune_expired_uploads,
    record_upload_preview,
    upload_path_for_token,
)
