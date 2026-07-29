"""Chat sessions and task runs: the lifecycle around a single prompt.

A session is one conversation; a run is one prompt within it. Runs carry the
project's content revision at the moment they started, so it is always possible
to tell which version of the data an answer was based on.
"""

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
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


# Identifies this process for the life of it. A run records the process driving
# it, so that another process can tell "abandoned by someone who died" from
# "someone else is still working on it" -- a distinction that does not exist
# while there is only one process, and is the whole game once there are several.
PROCESS_OWNER_ID = uuid.uuid4().hex

# How long a run may go without a heartbeat before another process may declare it
# abandoned. Generous on purpose: the heartbeat rides on the run's status events,
# and a single model call can leave a gap of minutes without anything being
# wrong. Only a hard kill relies on this -- an ordinary shutdown releases its own
# runs immediately.
DEFAULT_RUN_HEARTBEAT_TIMEOUT_SECONDS = 900


logger = logging.getLogger(__name__)


def _heartbeat_timeout() -> int:
    """How long a run may go unheard from before it counts as abandoned."""
    return bounded_env_int(
        "RUN_HEARTBEAT_TIMEOUT_SECONDS",
        DEFAULT_RUN_HEARTBEAT_TIMEOUT_SECONDS,
        30,
        24 * 60 * 60,
    )


def _heartbeat_cutoff() -> str:
    """The timestamp before which a still-running run counts as abandoned."""
    return (
        datetime.now(timezone.utc) - timedelta(seconds=_heartbeat_timeout())
    ).isoformat()


def touch_run_heartbeat(run_id: str) -> None:
    """Record that whoever is holding this run is still alive."""
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE task_runs SET heartbeat_at = ? WHERE id = ? AND status = 'running'",
            (utc_now(), run_id),
        )


class RunHeartbeat:
    """Keeps a run's heartbeat current for as long as someone holds the run.

    The heartbeat used to ride entirely on progress events, which is only
    sufficient while the agent is talkative. It is not: a single long model call
    reports nothing for minutes, and on the AgentCore Runtime transport nothing
    arrives at all until the run finishes, because the service buffers the whole
    response. A run outliving the timeout would then be swept up as abandoned --
    by the next prompt, which calls the sweep -- while it was still working.

    Liveness belongs to the process holding the run, not to how much the agent
    has to say, so it is stamped on a timer instead. A daemon thread, because
    the alternative is finding somewhere to do the work in a generator that
    spends its life blocked on the network.
    """

    def __init__(self, run_id: str, interval: Optional[float] = None) -> None:
        self.run_id = run_id
        # Comfortably inside the timeout, so several misses in a row are
        # survivable, and never so fast that a short run writes repeatedly.
        # Overridable so a test need not wait a real interval to observe a beat.
        self.interval = (
            interval
            if interval is not None
            else max(5.0, min(60.0, _heartbeat_timeout() / 3.0))
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._beat, name=f"heartbeat-{run_id[:8]}", daemon=True
        )
        self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                touch_run_heartbeat(self.run_id)
            except Exception as exc:  # noqa: BLE001 - a missed beat is survivable
                logger.warning("Could not record a heartbeat for run %s: %s", self.run_id, exc)

    def stop(self) -> None:
        """Stop beating. Safe to call more than once."""
        self._stop.set()
        self._thread.join(timeout=5)


def start_run_heartbeat(run_id: str, interval: Optional[float] = None) -> RunHeartbeat:
    """Begin stamping `run_id` as alive. The caller must stop() it."""
    return RunHeartbeat(run_id, interval)


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

    Sweeps abandoned runs first. Both limits below are counted from rows that say
    'running', so a run whose process died is a run that holds a slot forever.
    Doing it here rather than only at startup is what makes that self-healing:
    this is the one place where a stale row actually costs the user something.
    """
    with state.project_content_lock(project_id):
        project = get_project(project_id)
        run_id = uuid.uuid4().hex
        now = utc_now()
        fail_abandoned_runs()
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
                    status, latest_status, prompt, created_at,
                    owner_id, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)
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
                    PROCESS_OWNER_ID,
                    now,
                ),
            )
    context = create_run_context(user_id, project_id, session_id, run_id)
    return {"id": run_id, "context": context, "created_at": now}


def update_task_run_activity(run_id: str, message: str) -> None:
    """Record the latest progress line, so a reconnecting UI can catch up.

    Doubles as the run's heartbeat, which is why the write is no longer skipped
    when the status line repeats: a run that keeps reporting the same step is
    still very much alive, and suppressing the write would make it look dead.
    """
    status = message.strip()
    if not status:
        return
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE task_runs SET latest_status = ?, heartbeat_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (status, utc_now(), run_id),
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


def fail_abandoned_runs() -> int:
    """Fail runs whose owning process is gone. Returns how many.

    A run in the database with no process behind it blocks its session forever,
    since both concurrency limits count rows that say 'running'. Clearing them is
    therefore necessary -- but *which* rows is the whole question.

    This used to fail every running row, which is right only while there is
    exactly one process. With more than one -- two ECS tasks, or a rolling deploy
    where the new task starts before the old one drains -- a booting process
    would declare every one of its peers' live runs failed, killing work that was
    proceeding perfectly well. So a row is only abandoned when it says so itself:
    no owner (it predates this column, so it predates any live process), or a
    heartbeat old enough that no process could still be working on it.

    Deliberately not scoped to this process's own runs. A crashed peer's rows are
    exactly the ones that need clearing, and it is not around to clear them.
    """
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE task_runs
            SET status = 'failed',
                latest_status = 'Failed',
                error_summary = 'The process running this task stopped responding',
                completed_at = ?
            WHERE status = 'running'
              AND (owner_id IS NULL OR heartbeat_at IS NULL OR heartbeat_at < ?)
            """,
            (utc_now(), _heartbeat_cutoff()),
        )
        return cursor.rowcount


def release_owned_runs() -> int:
    """Fail this process's own runs at shutdown. Returns how many.

    The counterpart to the sweep, and what keeps an ordinary restart feeling the
    way it always has: a process that is going away knows its runs are dead now,
    so it says so rather than leaving them to be timed out. Only a hard kill --
    SIGKILL, an OOM, a lost host -- falls through to the heartbeat, and only that
    case waits.
    """
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE task_runs
            SET status = 'failed',
                latest_status = 'Failed',
                error_summary = 'Server restarted during the run',
                completed_at = ?
            WHERE status = 'running' AND owner_id = ?
            """,
            (utc_now(), PROCESS_OWNER_ID),
        )
        return cursor.rowcount


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
