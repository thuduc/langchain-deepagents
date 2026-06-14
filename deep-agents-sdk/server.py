import os
import re
import uuid
import shutil
import sqlite3
import zipfile
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent.parent
SDK_DIR = BASE_DIR / "deep-agents-sdk"
STATIC_DIR = SDK_DIR / "static"
CHARTS_DIR = STATIC_DIR / "charts"
TMP_UPLOADS_DIR = SDK_DIR / "tmp_uploads"
TMP_EXEC_DIR = SDK_DIR / "tmp_execution"
DB_PATH = SDK_DIR / "checkpoints.db"

load_dotenv(BASE_DIR / ".env")
if not os.environ.get("PORTKEY_API_KEY") and (BASE_DIR / ".env.portkey").exists():
    load_dotenv(BASE_DIR / ".env.portkey")
elif (BASE_DIR / ".env.portkey").exists():
    load_dotenv(BASE_DIR / ".env.portkey")

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
MEDIA_LABEL_RE = re.compile(
    r"^\s*(?:file|image|chart|plot|graph|figure|video|audio|media|output)?\s*"
    r"(?:saved|created|generated|written|exported)?\s*(?:file|image|chart|plot|graph|figure|video|audio|media|output)?\s*"
    r"(?:at|to|as|path)?\s*:?\s*$",
    re.IGNORECASE,
)


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


class ChatRequest(BaseModel):
    message: str
    project_id: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    project_id: str
    session_id: str


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


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
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
            """
        )
        seed_default_project(conn)


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


def normalize_project_media_links(project_id: str, text: str) -> str:
    """Replace local generated media paths with embeddable project media links."""
    output: List[str] = []

    for line in text.splitlines():
        if re.search(r"!\[[^\]]*\]\([^)]+\)", line) or "<video" in line or "<audio" in line:
            output.append(line)
            continue

        matches = list(MEDIA_PATH_RE.finditer(line))
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
        embeds = [media_embed_markdown(item) for item in media_items]

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
        for item, embed in zip(media_items, embeds):
            normalized_line = normalized_line.replace(item["raw"], f"\n\n{embed}\n\n")
        output.append(normalized_line)

    return "\n".join(output)


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


def load_skill_prompt(project_id: str, skill_name: str, session_id: str) -> str:
    project = get_project(project_id)
    project_root = Path(project["path"])
    skill_path = project_root / "skills" / skill_name / "SKILL.md"
    references_dir = project_root / "skills" / skill_name / "references"
    chart_dir = CHARTS_DIR / project["slug"] / session_id
    chart_dir.mkdir(parents=True, exist_ok=True)

    prompt = (
        f"You are an advanced specialist worker executing tasks for the skill '{skill_name}'.\n"
        f"The selected project is '{project['name']}'. All relative file paths are resolved from this project root: {project_root}.\n"
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
        "If the user asks for charts, save image files to this absolute directory: "
        f"'{chart_dir}'. Return markdown image links using this URL prefix: "
        f"'/static/charts/{project['slug']}/{session_id}/<filename>'. "
        "Do not present local filesystem paths for generated media in the final answer; embed the media with markdown instead. "
        "Always use index_nsa unless seasonally adjusted index_sa is specifically requested."
    )
    return prompt


def get_supervisor_system_prompt(project_id: str) -> str:
    project = get_project(project_id)
    skills = scan_project_skills(project_id)
    prompt = (
        "You are a supervisor coordinator. Your goal is to analyze the user prompt and coordinate the plan.\n"
        "You have access to specialist skills for the currently selected project only. "
        f"The selected project is '{project['name']}'.\n\n"
        "Available Skills:\n"
    )

    if skills:
        for skill in skills:
            prompt += f"- '{skill['name']}': {skill['description']}\n"
    else:
        prompt += "- No project skills are currently available.\n"

    prompt += (
        "\nCore Objective: Analyze the user's request. Create a plan and delegate sub-tasks to the "
        "'specialist_worker' by specifying the correct 'skill_name' and 'task_description'. "
        "Collect the execution results from the specialist, synthesize the findings, and present the final answer."
    )
    return prompt


def get_llm_instance():
    from langchain_openai import ChatOpenAI
    from portkey_ai import PORTKEY_GATEWAY_URL, createHeaders

    portkey_api_key = os.environ.get("PORTKEY_API_KEY")
    provider_slug = os.environ.get("PORTKEY_PROVIDER_SLUG", "google-ai-studio")
    model_name = os.environ.get("MODEL", "gemini-2.5-flash")

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


_agent_graphs: Dict[str, Any] = {}


def invalidate_project_agent(project_id: str) -> None:
    _agent_graphs.pop(project_id, None)


def get_agent_graph(project_id: str):
    if project_id in _agent_graphs:
        return _agent_graphs[project_id]

    from langchain_core.tools import tool
    from deepagents import create_deep_agent
    from langgraph.checkpoint.sqlite import SqliteSaver

    project_root = get_project_root(project_id)
    llm = get_llm_instance()

    @tool
    def read_file(path: str) -> str:
        """Read a file inside the selected project folder."""
        return read_project_file(project_id, path)

    @tool
    def write_file(path: str, content: str) -> str:
        """Write content to a file inside the selected project folder."""
        return write_project_file(project_id, path, content)

    @tool
    def execute_python(code: str) -> str:
        """Execute python code from the selected project's root directory."""
        return execute_python_code(code, project_root)

    @tool
    def specialist_worker(skill_name: str, task_description: str) -> str:
        """
        Invoke a specialist worker using one skill from the selected project.

        Args:
            skill_name: The project skill folder name to load.
            task_description: Detailed task instructions for the specialist.
        """
        worker_llm = get_llm_instance()
        current_session_id = getattr(specialist_worker, "_session_id", "default")
        system_prompt = load_skill_prompt(project_id, skill_name, current_session_id)

        try:
            transient_agent = create_deep_agent(
                model=worker_llm,
                tools=[read_file, write_file, execute_python],
                system_prompt=system_prompt,
            )
            result = transient_agent.invoke(
                {"messages": [{"role": "user", "content": task_description}]}
            )
            messages = result.get("messages", [])
            if messages:
                return message_content_to_text(messages[-1].content)
            return "Specialist completed the task but returned no content."
        except Exception as exc:
            return f"Error executing task using skill '{skill_name}': {str(exc)}"

    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    checkpointer = SqliteSaver(conn)

    graph = create_deep_agent(
        model=llm,
        tools=[specialist_worker],
        subagents=[],
        system_prompt=get_supervisor_system_prompt(project_id),
        checkpointer=checkpointer,
    )
    graph._specialist_worker_tool = specialist_worker
    _agent_graphs[project_id] = graph
    return graph


def run_agent(project_id: str, session_id: str, prompt: str) -> str:
    if not os.environ.get("PORTKEY_API_KEY"):
        return simulate_agent_response(project_id, session_id, prompt)

    try:
        agent = get_agent_graph(project_id)
        if hasattr(agent, "_specialist_worker_tool"):
            setattr(agent._specialist_worker_tool, "_session_id", session_id)

        result = agent.invoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config={
                "configurable": {"thread_id": session_thread_id(project_id, session_id)},
                "recursion_limit": 100,
            },
        )

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
    except Exception as exc:
        return f"Error invoking agent runner: {str(exc)}. Please check your environment configuration and PORTKEY_API_KEY."


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


@app.get("/api/projects")
def api_list_projects():
    init_db()
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC, name ASC").fetchall()
    return {"projects": [row_to_project(row) for row in rows]}


@app.post("/api/projects")
def api_create_project(request: ProjectCreateRequest):
    return {"project": create_project_record(request.name)}


@app.get("/api/projects/{project_id}")
def api_get_project(project_id: str):
    project = get_project(project_id)
    project["skills"] = scan_project_skills(project_id)
    return {"project": project}


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
    invalidate_project_agent(project["id"])
    return {"project": get_project(project["id"])}


@app.post("/api/projects/{project_id}/contents/import")
def api_import_project_contents(project_id: str, request: ContentImportRequest):
    project_root = get_project_root(project_id)
    zip_path = upload_path_for_token(request.upload_token)
    extract_zip(zip_path, project_root, request.mode)
    touch_project(project_id)
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
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM chat_sessions
            WHERE project_id = ?
            ORDER BY updated_at DESC
            """,
            (project_id,),
        ).fetchall()
    return {"sessions": [dict(row) for row in rows]}


@app.post("/api/projects/{project_id}/sessions")
def api_create_session(project_id: str, request: SessionCreateRequest):
    return {"session": create_session(project_id, request.title)}


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


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
examples_dir = BASE_DIR / "examples"
examples_dir.mkdir(exist_ok=True)
app.mount("/examples", StaticFiles(directory=examples_dir), name="examples")
