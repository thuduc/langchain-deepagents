import os
import re
import uuid
import shutil
import sqlite3
import zipfile
import subprocess
import contextvars
import json
import logging
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent.parent
SDK_DIR = BASE_DIR / "deep-agents-sdk"
STATIC_DIR = SDK_DIR / "static"
CHARTS_DIR = STATIC_DIR / "charts"
TMP_UPLOADS_DIR = SDK_DIR / "tmp_uploads"
TMP_EXEC_DIR = SDK_DIR / "tmp_execution"
DB_PATH = SDK_DIR / "checkpoints.db"
AGENT_CHECKPOINT_DB_PATH = SDK_DIR / "agent_checkpoints.db"

logger = logging.getLogger(__name__)
CURRENT_AGENT_SESSION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_agent_session_id",
    default="default",
)

load_dotenv(BASE_DIR / ".env")

CHARTS_DIR.mkdir(parents=True, exist_ok=True)
TMP_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
TMP_EXEC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Deep Agents Project Workspace")

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


class ChatRequest(BaseModel):
    message: str
    project_id: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    project_id: str
    session_id: str


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


def get_projects_dir() -> Path:
    raw = os.environ.get("PROJECTS_DIR", "projects")
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


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
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                title TEXT NOT NULL,
                thread_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        seed_default_project(conn)
    prune_all_project_sessions()
    validate_all_project_skills_once()


def seed_default_project(conn: sqlite3.Connection) -> None:
    projects_dir = get_projects_dir()
    default_slug = "hpi-analytics"
    default_path = projects_dir / default_slug
    if not default_path.exists():
        return

    existing = conn.execute(
        "SELECT id FROM projects WHERE id = ? OR slug = ?", (default_slug, default_slug)
    ).fetchone()
    if existing:
        return

    now = utc_now()
    conn.execute(
        """
        INSERT INTO projects (id, name, slug, path, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (default_slug, "HPI Analytics", default_slug, str(default_path), now, now),
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
        _agent_graphs.clear()

    prune_all_project_sessions(max_sessions)
    return get_app_settings()


def prune_project_sessions(project_id: str, max_sessions: Optional[int] = None) -> None:
    limit = max_sessions if max_sessions is not None else get_app_settings()["max_sessions_per_project"]
    limit = coerce_max_sessions(limit, DEFAULT_MAX_SESSIONS_PER_PROJECT)
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id FROM chat_sessions
            WHERE project_id = ?
            ORDER BY updated_at DESC, created_at DESC, id DESC
            """,
            (project_id,),
        ).fetchall()
        stale_ids = [row["id"] for row in rows[limit:]]
        if stale_ids:
            conn.executemany(
                "DELETE FROM chat_sessions WHERE id = ? AND project_id = ?",
                [(session_id, project_id) for session_id in stale_ids],
            )


def prune_all_project_sessions(max_sessions: Optional[int] = None) -> None:
    limit = max_sessions if max_sessions is not None else get_app_settings()["max_sessions_per_project"]
    with get_db_connection() as conn:
        rows = conn.execute("SELECT id FROM projects").fetchall()
    for row in rows:
        prune_project_sessions(row["id"], limit)


def list_project_sessions(project_id: str, prune: bool = True) -> List[Dict[str, Any]]:
    if prune:
        prune_project_sessions(project_id)
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM chat_sessions
            WHERE project_id = ?
            ORDER BY updated_at DESC, created_at DESC, id DESC
            """,
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def row_to_project(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "path": row["path"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
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
    project_path = ensure_child_path(get_projects_dir(), Path(project["path"]))
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
            "operations": ["read", "write"],
            "paths": PROJECT_FILESYSTEM_ALLOW_PATTERNS.copy(),
            "mode": "allow",
        },
    ]


def project_chart_context(project: Dict[str, Any], session_id: str, create: bool = True) -> Dict[str, str]:
    chart_dir = CHARTS_DIR / project["slug"] / session_id
    if create:
        chart_dir.mkdir(parents=True, exist_ok=True)
    return {
        "session_id": session_id,
        "chart_directory": str(chart_dir),
        "chart_url_prefix": f"/static/charts/{project['slug']}/{session_id}/",
    }


def project_isolation_audit(project_id: str) -> Dict[str, Any]:
    project = get_project(project_id)
    project_root = Path(project["path"])
    projects_dir = get_projects_dir()
    skills_dir = get_skill_dir(project_id)
    skills = scan_project_skills(project_id)
    sample_session_id = "isolation-audit"
    chart_context = project_chart_context(project, sample_session_id, create=False)
    chart_dir = Path(chart_context["chart_directory"]).resolve()

    checks = {
        "project_root_within_projects_dir": project_root == projects_dir or projects_dir in project_root.parents,
        "skills_dir_within_project_root": skills_dir == project_root or project_root in skills_dir.parents,
        "skills_are_project_local": all(
            (skills_dir / skill["name"] / "SKILL.md").resolve().is_file()
            and project_root in (skills_dir / skill["name"] / "SKILL.md").resolve().parents
            for skill in skills
        ),
        "checkpoint_threads_include_project_and_session": (
            session_thread_id(project_id, sample_session_id)
            == f"project:{project_id}:session:{sample_session_id}"
        ),
        "agent_checkpoints_separate_from_app_db": AGENT_CHECKPOINT_DB_PATH.resolve() != DB_PATH.resolve(),
        "chart_directory_project_session_scoped": (
            chart_dir == (CHARTS_DIR / project["slug"] / sample_session_id).resolve()
        ),
        "chart_url_project_session_scoped": (
            chart_context["chart_url_prefix"] == f"/static/charts/{project['slug']}/{sample_session_id}/"
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
            "sample_thread_id": session_thread_id(project_id, sample_session_id),
        },
        "artifacts": chart_context,
        "checks": checks,
        "passed": all(checks.values()),
    }


def touch_project(project_id: str) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE projects SET updated_at = ? WHERE id = ?", (utc_now(), project_id)
        )


def create_project_record(name: str) -> Dict[str, Any]:
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
            INSERT INTO projects (id, name, slug, path, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (slug, name.strip(), slug, str(project_path), now, now),
        )
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (slug,)).fetchone()
        return row_to_project(row)


def delete_directory_contents(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


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


def project_media_url(project_id: str, file_path: Path) -> Optional[str]:
    project_root = get_project_root(project_id)
    resolved = file_path.resolve()

    if resolved.suffix.lower() not in MEDIA_EXTENSIONS or not resolved.is_file():
        return None

    if resolved == project_root or project_root in resolved.parents:
        rel = resolved.relative_to(project_root).as_posix()
        return f"/api/projects/{project_id}/media/{quote(rel, safe='/')}"

    static_root = STATIC_DIR.resolve()
    if resolved == static_root or static_root in resolved.parents:
        rel = resolved.relative_to(static_root).as_posix()
        return f"/static/{quote(rel, safe='/')}"

    examples_root = (BASE_DIR / "examples").resolve()
    if resolved == examples_root or examples_root in resolved.parents:
        rel = resolved.relative_to(examples_root).as_posix()
        return f"/examples/{quote(rel, safe='/')}"

    return None


def resolve_media_reference(project_id: str, raw_path: str) -> Optional[Dict[str, str]]:
    cleaned = strip_media_reference(raw_path)
    if not cleaned:
        return None

    project_root = get_project_root(project_id)
    candidates: List[Path] = []

    if cleaned.startswith("/static/"):
        candidates.append(STATIC_DIR / cleaned.removeprefix("/static/"))
    elif cleaned.startswith("/examples/"):
        candidates.append(BASE_DIR / "examples" / cleaned.removeprefix("/examples/"))
    elif cleaned.startswith("/"):
        candidates.append(Path(cleaned))
    else:
        candidates.append(project_root / cleaned)
        candidates.append(BASE_DIR / cleaned)

    for candidate in candidates:
        try:
            url = project_media_url(project_id, candidate)
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


def media_urls_already_embedded(project_id: str, line: str) -> set[str]:
    urls: set[str] = set()
    for match in MARKDOWN_IMAGE_RE.finditer(line):
        media = resolve_media_reference(project_id, match.group("href"))
        urls.add(media["url"] if media else match.group("href"))
    for match in HTML_MEDIA_SRC_RE.finditer(line):
        media = resolve_media_reference(project_id, match.group("src"))
        urls.add(media["url"] if media else match.group("src"))
    return urls


def embedded_media_spans(line: str) -> List[tuple[int, int]]:
    spans = [match.span() for match in MARKDOWN_IMAGE_RE.finditer(line)]
    spans.extend(match.span() for match in HTML_MEDIA_SRC_RE.finditer(line))
    return spans


def span_inside_any(start: int, end: int, spans: List[tuple[int, int]]) -> bool:
    return any(span_start <= start and end <= span_end for span_start, span_end in spans)


def normalize_project_media_links(project_id: str, text: str) -> str:
    """Replace local generated media paths with embeddable project media links."""
    output: List[str] = []
    embedded_urls: set[str] = set()

    for line in text.splitlines():
        existing_line_urls = media_urls_already_embedded(project_id, line)
        embedded_urls.update(existing_line_urls)
        protected_spans = embedded_media_spans(line)

        matches = [
            match
            for match in MEDIA_PATH_RE.finditer(line)
            if not span_inside_any(match.start("path"), match.end("path"), protected_spans)
        ]
        media_items: List[Dict[str, str]] = []
        for match in matches:
            media = resolve_media_reference(project_id, match.group("path"))
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
        if not skill_path.is_dir() or not skill_md.exists():
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


def upload_path_for_token(token: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise HTTPException(status_code=400, detail="Invalid upload token")
    path = TMP_UPLOADS_DIR / f"{token}.zip"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Upload preview expired or not found")
    return path


def is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return (mode & 0o170000) == 0o120000


def inspect_zip(zip_path: Path) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    total_size = 0

    try:
        with zipfile.ZipFile(zip_path) as archive:
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


def extract_zip(zip_path: Path, target_dir: Path, mode: str) -> None:
    if mode not in {"merge", "replace"}:
        raise HTTPException(status_code=400, detail="Import mode must be 'merge' or 'replace'")

    target_dir.mkdir(parents=True, exist_ok=True)
    inspect_zip(zip_path)

    if mode == "replace":
        delete_directory_contents(target_dir)

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
            with archive.open(info) as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def session_thread_id(project_id: str, session_id: str) -> str:
    return f"project:{project_id}:session:{session_id}"


def create_session(project_id: str, title: Optional[str] = None) -> Dict[str, Any]:
    get_project(project_id)
    session_id = uuid.uuid4().hex
    now = utc_now()
    final_title = title.strip() if title and title.strip() else "New chat"
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO chat_sessions (id, project_id, title, thread_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                project_id,
                final_title,
                session_thread_id(project_id, session_id),
                now,
                now,
            ),
        )
    prune_project_sessions(project_id)
    return {
        "id": session_id,
        "project_id": project_id,
        "title": final_title,
        "thread_id": session_thread_id(project_id, session_id),
        "created_at": now,
        "updated_at": now,
    }


def get_session(project_id: str, session_id: str) -> Dict[str, Any]:
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM chat_sessions
            WHERE id = ? AND project_id = ?
            """,
            (session_id, project_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return dict(row)


def add_chat_message(project_id: str, session_id: str, role: str, content: str) -> None:
    now = utc_now()
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO chat_messages (project_id, session_id, role, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, session_id, role, content, now),
        )
        conn.execute(
            """
            UPDATE chat_sessions SET updated_at = ? WHERE id = ? AND project_id = ?
            """,
            (now, session_id, project_id),
        )


def maybe_title_session(project_id: str, session_id: str, message: str) -> None:
    title = message.strip().replace("\n", " ")
    if len(title) > 60:
        title = title[:57].rstrip() + "..."
    if not title:
        return

    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT title FROM chat_sessions WHERE id = ? AND project_id = ?",
            (session_id, project_id),
        ).fetchone()
        if row and row["title"] == "New chat":
            conn.execute(
                "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE id = ? AND project_id = ?",
                (title, utc_now(), session_id, project_id),
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


def read_project_file(project_id: str, path: str) -> str:
    try:
        project_root = get_project_root(project_id)
        file_path = relative_project_path(project_root, path)
        if not file_path.is_file():
            return f"Error reading file: {path} is not a file."
        return file_path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error reading file: {str(exc)}"


def write_project_file(project_id: str, path: str, content: str) -> str:
    try:
        project_root = get_project_root(project_id)
        file_path = relative_project_path(project_root, path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"File successfully written to {path}"
    except Exception as exc:
        return f"Error writing file: {str(exc)}"


def python_binary() -> str:
    candidate = SDK_DIR / "venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    raise RuntimeError("SDK virtual environment python binary not found at deep-agents-sdk/venv/bin/python")


def execute_python_code(code: str, cwd: Path) -> str:
    temp_path = TMP_EXEC_DIR / f"temp_run_{uuid.uuid4().hex}.py"
    try:
        temp_path.write_text(code, encoding="utf-8")
        result = subprocess.run(
            [python_binary(), str(temp_path)],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = ""
        if result.stdout:
            output += f"Output:\n{result.stdout}\n"
        if result.stderr:
            output += f"Errors:\n{result.stderr}\n"
        return output or "Execution completed successfully with no output."
    except subprocess.TimeoutExpired:
        return "Error: Code execution timed out after 30 seconds."
    except Exception as exc:
        return f"Error executing code: {str(exc)}"
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def load_skill_prompt(project_id: str, skill_name: str, session_id: Optional[str] = None) -> str:
    project = get_project(project_id)
    project_root = Path(project["path"])
    skill_path = project_root / "skills" / skill_name / "SKILL.md"
    references_dir = project_root / "skills" / skill_name / "references"

    prompt = (
        f"You are an advanced specialist worker executing tasks for the skill '{skill_name}'.\n"
        f"The selected project is '{project['name']}'. All relative file paths are resolved from this project root: {project_root}.\n"
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
        "When reading project data, use paths relative to the selected project root, such as 'data/<filename>'. "
        "Do not present local filesystem paths for generated media in the final answer; embed the media with markdown instead. "
        "Always use index_nsa unless seasonally adjusted index_sa is specifically requested."
    )
    if session_id:
        chart_context = project_chart_context(project, session_id)
        prompt += (
            " If the user asks for charts, save image files to this absolute directory: "
            f"'{chart_context['chart_directory']}'. Return markdown image links using this URL prefix: "
            f"'{chart_context['chart_url_prefix']}<filename>'."
        )
    else:
        prompt += (
            " If the user asks for charts, call get_project_context first, save image files to the returned "
            "chart_directory, and return markdown image links using the returned chart_url_prefix."
        )
    return prompt


def get_supervisor_system_prompt(project_id: str) -> str:
    project = get_project(project_id)
    return (
        "You are a supervisor coordinator. Your goal is to analyze the user prompt and coordinate the plan.\n"
        "Use the Deep Agents skills library and task tool for the currently selected project to decide which specialist "
        f"capability applies. The selected project is '{project['name']}'.\n\n"
        "Core Objective: Analyze the user's request. Create a plan, delegate substantive analysis to the best "
        "project-specific subagent using the task tool, synthesize the subagent result, and present the final answer."
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


def agent_run_config(project_id: str, session_id: str) -> Dict[str, Any]:
    return {
        "configurable": {"thread_id": session_thread_id(project_id, session_id)},
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


_agent_graphs: Dict[str, Any] = {}
_active_agent_sessions: Dict[str, str] = {}


def invalidate_project_agent(project_id: str) -> None:
    _agent_graphs.pop(project_id, None)


def set_active_agent_session(project_id: str, session_id: str) -> None:
    _active_agent_sessions[project_id] = session_id


def clear_active_agent_session(project_id: str, session_id: str) -> None:
    if _active_agent_sessions.get(project_id) == session_id:
        _active_agent_sessions.pop(project_id, None)


def current_agent_session_id(project_id: str) -> str:
    session_id = CURRENT_AGENT_SESSION_ID.get()
    if session_id == "default":
        return _active_agent_sessions.get(project_id, session_id)
    return session_id


def get_agent_graph(project_id: str):
    if project_id in _agent_graphs:
        return _agent_graphs[project_id]

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
        """Execute python code from the selected project's root directory."""
        return execute_python_code(code, project_root)

    @tool
    def get_project_context() -> Dict[str, str]:
        """Return the selected project root and current session chart output locations."""
        session_id = current_agent_session_id(project_id)
        chart_context = project_chart_context(project, session_id)
        return {
            "project_name": project["name"],
            "project_root": str(project_root),
            **chart_context,
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
    _agent_graphs[project_id] = graph
    return graph


def run_agent(project_id: str, session_id: str, prompt: str) -> str:
    if not os.environ.get("PORTKEY_API_KEY"):
        return simulate_agent_response(project_id, session_id, prompt)

    try:
        agent = get_agent_graph(project_id)
        set_active_agent_session(project_id, session_id)
        session_token = CURRENT_AGENT_SESSION_ID.set(session_id)
        try:
            result = agent.invoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config=agent_run_config(project_id, session_id),
            )
        finally:
            try:
                CURRENT_AGENT_SESSION_ID.reset(session_token)
            except ValueError:
                CURRENT_AGENT_SESSION_ID.set("default")
            clear_active_agent_session(project_id, session_id)

        return response_text_from_agent_result(result)
    except Exception as exc:
        return f"Error invoking agent runner: {str(exc)}. Please check your environment configuration and PORTKEY_API_KEY."


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
    display_name = namespace[-1].split(":")[0] if is_subagent else name
    if "input" in data and name == "model":
        return f"{display_name} is thinking" if is_subagent else "Supervisor is thinking"
    if "input" in data and name == "tools":
        return f"{display_name} is using tools" if is_subagent else "Using tools"
    if "input" in data and name == "task":
        return "Starting specialist task"
    if "result" in data and name == "task":
        return "Specialist task completed"
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


def stream_agent_events(project_id: str, session_id: str, prompt: str):
    if not os.environ.get("PORTKEY_API_KEY"):
        yield sse_event("status", {"message": "Running local fallback"})
        response_text = normalize_project_media_links(project_id, simulate_agent_response(project_id, session_id, prompt))
        add_chat_message(project_id, session_id, "assistant", response_text)
        yield sse_event(
            "final",
            {"response": response_text, "project_id": project_id, "session_id": session_id},
        )
        return

    final_text = ""
    emitted_delta = False
    try:
        agent = get_agent_graph(project_id)
        set_active_agent_session(project_id, session_id)
        session_token = CURRENT_AGENT_SESSION_ID.set(session_id)
        try:
            yield sse_event("status", {"message": "Starting agent run"})
            for chunk in agent.stream(
                {"messages": [{"role": "user", "content": prompt}]},
                config=agent_run_config(project_id, session_id),
                stream_mode=["updates", "messages", "tasks"],
                subgraphs=True,
            ):
                namespace, mode, data = chunk_parts(chunk)

                if mode == "tasks":
                    status = task_status_from_event(namespace, data)
                    if status:
                        yield sse_event("status", {"message": status})
                    continue

                if mode == "messages":
                    delta = delta_from_message_event(namespace, data)
                    if delta:
                        emitted_delta = True
                        yield sse_event("delta", {"text": delta})
                    continue

                if mode == "updates":
                    update_text = final_text_from_update(namespace, data)
                    if update_text:
                        final_text = update_text
        finally:
            try:
                CURRENT_AGENT_SESSION_ID.reset(session_token)
            except ValueError:
                CURRENT_AGENT_SESSION_ID.set("default")
            clear_active_agent_session(project_id, session_id)

        if not final_text:
            final_text = "The model returned an empty response for this request. Please try again or rephrase your prompt."

        response_text = normalize_project_media_links(project_id, final_text)
        add_chat_message(project_id, session_id, "assistant", response_text)
        if not emitted_delta:
            yield sse_event("delta", {"text": response_text})
        yield sse_event(
            "final",
            {"response": response_text, "project_id": project_id, "session_id": session_id},
        )
    except Exception as exc:
        error_text = f"Error invoking agent runner: {str(exc)}. Please check your environment configuration and PORTKEY_API_KEY."
        add_chat_message(project_id, session_id, "assistant", error_text)
        yield sse_event("error", {"message": error_text})


def simulate_agent_response(project_id: str, session_id: str, prompt: str) -> str:
    project = get_project(project_id)
    project_root = Path(project["path"])
    p_lower = prompt.lower()

    if "california" in p_lower and "growth" in p_lower:
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

    if "plot" in p_lower or "chart" in p_lower:
        chart_id = uuid.uuid4().hex[:6]
        chart_dir = CHARTS_DIR / project["slug"] / session_id
        chart_dir.mkdir(parents=True, exist_ok=True)
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
plt.savefig(r'{chart_dir / f"hpi_{chart_id}.png"}', dpi=150)
"""
        execute_python_code(code, project_root)
        return (
            "Here is the housing price index comparison chart for CA, NY, and TX from 2015 to present:\n\n"
            f"![HPI Comparison Chart](/static/charts/{project['slug']}/{session_id}/hpi_{chart_id}.png)"
        )

    return (
        f"I received your request for project '{project['name']}': '{prompt}'. "
        "LLM API key is not set, so this is a local test response."
    )


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/")
def get_index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/settings")
def api_get_settings():
    init_db()
    return {"settings": get_app_settings()}


@app.put("/api/settings")
def api_update_settings(request: SettingsUpdateRequest):
    init_db()
    settings = save_app_settings(
        request.default_model.strip(),
        request.max_sessions_per_project,
    )
    return {"settings": settings}


@app.get("/api/projects")
def api_list_projects():
    init_db()
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC, name ASC").fetchall()
    return {"projects": [row_to_project(row) for row in rows]}


@app.post("/api/projects")
def api_create_project(request: ProjectCreateRequest):
    project = create_project_record(request.name)
    validate_project_skills(project["id"])
    return {"project": project}


@app.get("/api/projects/{project_id}")
def api_get_project(project_id: str):
    project = get_project(project_id)
    project["skills"] = scan_project_skills(project_id)
    return {"project": project}


@app.get("/api/projects/{project_id}/isolation")
def api_project_isolation(project_id: str):
    return {"audit": project_isolation_audit(project_id)}


@app.delete("/api/projects/{project_id}")
def api_delete_project(project_id: str):
    project = get_project(project_id)
    project_root = ensure_child_path(get_projects_dir(), Path(project["path"]))
    if project_root.exists():
        shutil.rmtree(project_root)
    with get_db_connection() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    invalidate_project_agent(project_id)
    return {"deleted": True}


@app.get("/api/projects/{project_id}/contents")
def api_project_contents(project_id: str):
    project_root = get_project_root(project_id)
    return {"items": list_project_files(project_root)}


@app.delete("/api/projects/{project_id}/contents")
def api_delete_project_contents(project_id: str):
    project_root = get_project_root(project_id)
    delete_directory_contents(project_root)
    touch_project(project_id)
    invalidate_project_agent(project_id)
    return {"deleted": True}


@app.post("/api/uploads/preview")
async def api_upload_preview(file: UploadFile = File(...)):
    token = uuid.uuid4().hex
    zip_path = TMP_UPLOADS_DIR / f"{token}.zip"
    total = 0
    with zip_path.open("wb") as dst:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 250 * 1024 * 1024:
                zip_path.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail="ZIP upload exceeds 250MB limit")
            dst.write(chunk)

    preview = inspect_zip(zip_path)
    return {"upload_token": token, **preview}


@app.post("/api/projects/import")
def api_import_project(request: ProjectImportRequest):
    project = create_project_record(request.name)
    zip_path = upload_path_for_token(request.upload_token)
    extract_zip(zip_path, Path(project["path"]), request.mode)
    touch_project(project["id"])
    validate_project_skills(project["id"])
    invalidate_project_agent(project["id"])
    return {"project": get_project(project["id"])}


@app.post("/api/projects/{project_id}/contents/import")
def api_import_project_contents(project_id: str, request: ContentImportRequest):
    project_root = get_project_root(project_id)
    zip_path = upload_path_for_token(request.upload_token)
    extract_zip(zip_path, project_root, request.mode)
    touch_project(project_id)
    validate_project_skills(project_id)
    invalidate_project_agent(project_id)
    return {"project": get_project(project_id), "items": list_project_files(project_root)}


@app.get("/api/projects/{project_id}/media/{media_path:path}")
def api_project_media(project_id: str, media_path: str):
    project_root = get_project_root(project_id)
    file_path = relative_project_path(project_root, media_path)
    if file_path.suffix.lower() not in MEDIA_EXTENSIONS or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Project media not found")
    return FileResponse(file_path)


@app.get("/api/projects/{project_id}/sessions")
def api_list_sessions(project_id: str):
    get_project(project_id)
    return {"sessions": list_project_sessions(project_id)}


@app.post("/api/projects/{project_id}/sessions")
def api_create_session(project_id: str, request: SessionCreateRequest):
    session = create_session(project_id, request.title)
    return {"session": session, "sessions": list_project_sessions(project_id)}


@app.get("/api/projects/{project_id}/sessions/{session_id}")
def api_get_session(project_id: str, session_id: str):
    session = get_session(project_id, session_id)
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, role, content, created_at FROM chat_messages
            WHERE project_id = ? AND session_id = ?
            ORDER BY id ASC
            """,
            (project_id, session_id),
        ).fetchall()
    messages = []
    for row in rows:
        message = dict(row)
        if message["role"] == "assistant":
            message["content"] = normalize_project_media_links(project_id, message["content"])
        messages.append(message)
    return {"session": session, "messages": messages}


@app.delete("/api/projects/{project_id}/sessions/{session_id}")
def api_delete_session(project_id: str, session_id: str):
    get_session(project_id, session_id)
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM chat_sessions WHERE id = ? AND project_id = ?",
            (session_id, project_id),
        )
    return {"deleted": True}


@app.post("/api/chat", response_model=ChatResponse)
def api_chat(request: ChatRequest):
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    get_project(request.project_id)

    session_id = request.session_id
    if session_id:
        get_session(request.project_id, session_id)
    else:
        session_id = create_session(request.project_id)["id"]

    maybe_title_session(request.project_id, session_id, request.message)
    add_chat_message(request.project_id, session_id, "user", request.message)
    response_text = run_agent(request.project_id, session_id, request.message)
    response_text = normalize_project_media_links(request.project_id, response_text)
    add_chat_message(request.project_id, session_id, "assistant", response_text)

    return ChatResponse(
        response=response_text,
        project_id=request.project_id,
        session_id=session_id,
    )


@app.post("/api/chat/stream")
def api_chat_stream(request: ChatRequest):
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    get_project(request.project_id)

    session_id = request.session_id
    if session_id:
        get_session(request.project_id, session_id)
    else:
        session_id = create_session(request.project_id)["id"]

    maybe_title_session(request.project_id, session_id, request.message)
    add_chat_message(request.project_id, session_id, "user", request.message)

    return StreamingResponse(
        stream_agent_events(request.project_id, session_id, request.message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
examples_dir = BASE_DIR / "examples"
examples_dir.mkdir(exist_ok=True)
app.mount("/examples", StaticFiles(directory=examples_dir), name="examples")
