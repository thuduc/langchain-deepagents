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
from deep_agents_app.config import load_runtime_paths  # noqa: E402
from deep_agents_app.domain import CurrentUser, RunContext  # noqa: E402
from deep_agents_app.db import connect, initialize_schema  # noqa: E402

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


def get_db_connection() -> sqlite3.Connection:
    return connect(DB_PATH)


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as conn:
        initialize_schema(conn)
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

from deep_agents_app.services.artifacts import (  # noqa: E402, F401
    append_artifact_links,
    prepare_response_artifacts,
    register_run_artifacts,
    unique_artifact_path,
)


from deep_agents_app.services.sessions import (  # noqa: E402, F401
    add_chat_message,
    cleanup_run_work,
    create_session,
    create_task_run,
    delete_session_resources,
    ensure_no_active_project_runs,
    finish_task_run,
    get_session,
    maybe_title_session,
    recover_interrupted_runs,
    session_thread_id,
    update_task_run_activity,
)


from deep_agents_app.runtime.engine import (  # noqa: E402, F401
    execute_python_code,
    invalidate_project_agent,
    invalidate_all_agents,
    run_agent,
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
