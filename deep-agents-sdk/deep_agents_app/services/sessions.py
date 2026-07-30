import shutil
import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from deep_agents_app.domain import RunContext
from deep_agents_app.runtime.checkpoints import delete_checkpoint_thread
from deep_agents_app.services import workspace as state
from deep_agents_app.services.workspace import (
    DEFAULT_MAX_SESSIONS_PER_PROJECT,
    bounded_env_int,
    coerce_max_sessions,
    create_run_context,
    ensure_child_path,
    get_app_settings,
    get_db_connection,
    get_project,
    logger,
    utc_now,
)


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


def prune_project_sessions(
    user_id: str,
    project_id: str,
    max_sessions: Optional[int] = None,
) -> None:
    """Enforce the retention cap for one user and project.

    This deletes chats and their artifacts, so it must only run on write paths
    such as creating a chat or lowering the configured limit. Read endpoints
    must never call it.
    """
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


def list_project_sessions(user_id: str, project_id: str) -> List[Dict[str, Any]]:
    """Read one user's chats for a project. This never deletes anything."""
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
    with state.project_content_lock(project_id):
        project = get_project(project_id)
        run_id = uuid.uuid4().hex
        now = utc_now()
        with get_db_connection() as conn:
            # The busy-chat check comes first because it is the more specific
            # diagnosis. Checking the account-wide limit first meant a user at
            # the limit who resubmitted into an already-busy chat was told to
            # wait for their other chats, which is not the actual problem.
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


def cleanup_run_work(context: RunContext) -> None:
    try:
        work_dir = ensure_child_path(state.WORK_DIR, context.work_directory)
        if work_dir.exists():
            shutil.rmtree(work_dir)
        parent = work_dir.parent
        work_root = state.WORK_DIR.resolve()
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
    if not state.WORK_DIR.exists():
        return
    for child in state.WORK_DIR.iterdir():
        try:
            resolved = ensure_child_path(state.WORK_DIR, child)
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
        state.ARTIFACTS_DIR, state.ARTIFACTS_DIR / user_id / project_id / session_id
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
