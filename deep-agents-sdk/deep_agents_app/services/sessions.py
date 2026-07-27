"""Chat sessions and task runs: the lifecycle around a single prompt.

A session is one conversation; a run is one prompt within it. Runs carry the
project's content revision at the moment they started, so it is always possible
to tell which version of the data an answer was based on.
"""

import uuid
from typing import Any, Dict, Optional

from fastapi import HTTPException

from deep_agents_app.services import artifact_store
from deep_agents_app.services import workspace as state
from deep_agents_app.services.workspace import (
    bounded_env_int,
    create_run_context,
    get_db_connection,
    get_project,
    prune_project_sessions,
    utc_now,
)


def _delete_checkpoint_thread(thread_id: str) -> None:
    from deep_agents_app.runtime.engine import delete_checkpoint_thread as runtime_delete_checkpoint_thread
    runtime_delete_checkpoint_thread(thread_id)


def session_thread_id(user_id: str, project_id: str, session_id: str) -> str:
    """The LangGraph checkpoint thread key for a conversation.

    Includes the user, project and session, so two users chatting in the same
    project never share conversational memory.
    """
    return f"user:{user_id}:project:{project_id}:session:{session_id}"


def create_session(
    user_id: str,
    project_id: str,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Start a conversation, pruning the user's oldest if they are at the cap."""
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
    """Fetch a session the caller owns, or raise 404.

    The user_id predicate is the authorization check: another user's session id
    is indistinguishable from one that does not exist.
    """
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
    """Append a message and mark the session as recently used."""
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
    """Name an untitled conversation after its first prompt."""
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
    """Open a run, enforcing the concurrency limits.

    Held under the project content lock so a run cannot start while project
    content is being edited, and vice versa. Refuses a second run in the same
    session, and more than MAX_CONCURRENT_RUNS_PER_USER across all of them.
    """
    with state.project_content_lock(project_id):
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
    """Record the latest progress line, so a reconnecting UI can catch up."""
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
    """Move a run to a terminal state. Only affects a run still running."""
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


def recover_interrupted_runs() -> None:
    """Fail runs left running by a previous process.

    Called at startup: a run in the database with no process behind it would
    otherwise block its session forever.
    """
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


def delete_session_resources(
    user_id: str,
    project_id: str,
    session_id: str,
    thread_id: Optional[str] = None,
) -> None:
    """Delete a conversation and everything it produced.

    Removes artifacts from the configured store, drops the checkpoint thread,
    and deletes the rows. Refuses while a run is still active.
    """
    with get_db_connection() as conn:
        session = conn.execute(
            """
            SELECT thread_id FROM chat_sessions
            WHERE id = ? AND project_id = ? AND user_id = ?
            """,
            (session_id, project_id, user_id),
        ).fetchone()
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

    artifact_store.get_store().delete_prefix(f"{user_id}/{project_id}/{session_id}")

    _delete_checkpoint_thread(thread_id or session["thread_id"])
    with get_db_connection() as conn:
        conn.execute(
            """
            DELETE FROM chat_sessions
            WHERE id = ? AND project_id = ? AND user_id = ?
            """,
            (session_id, project_id, user_id),
        )

def ensure_no_active_project_runs(project_id: str) -> None:
    """Refuse a content edit while any run in the project is executing."""
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
