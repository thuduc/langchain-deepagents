"""SQL for user-owned rows: sessions, messages, runs and artifacts.

Every query filters on user_id. That predicate is what keeps one user's
conversations and downloads invisible to another.
"""

import sqlite3


def messages(connection: sqlite3.Connection, user_id: str, project_id: str, session_id: str):
    """A conversation in order, joined to run timings for duration display."""
    return connection.execute(
        """
        SELECT cm.id, cm.role, cm.content, cm.created_at, cm.run_id,
               tr.created_at AS run_created_at, tr.completed_at AS run_completed_at
        FROM chat_messages AS cm
        LEFT JOIN task_runs AS tr ON tr.id = cm.run_id
        WHERE cm.user_id = ? AND cm.project_id = ? AND cm.session_id = ?
        ORDER BY cm.id ASC
        """,
        (user_id, project_id, session_id),
    ).fetchall()


def artifacts(connection: sqlite3.Connection, user_id: str, project_id: str, session_id: str):
    """Every artifact in a session, oldest first."""
    return connection.execute(
        """
        SELECT * FROM artifacts
        WHERE user_id = ? AND project_id = ? AND session_id = ?
        ORDER BY created_at ASC
        """,
        (user_id, project_id, session_id),
    ).fetchall()


def runs(connection: sqlite3.Connection, user_id: str, project_id: str, session_id: str):
    """Run history for a session, newest first."""
    return connection.execute(
        """
        SELECT id, status, latest_status, project_revision, created_at, completed_at
        FROM task_runs
        WHERE user_id = ? AND project_id = ? AND session_id = ?
        ORDER BY created_at DESC
        """,
        (user_id, project_id, session_id),
    ).fetchall()


def artifact_by_owner(connection: sqlite3.Connection, artifact_id: str, user_id: str):
    """One artifact, but only if this user owns it.

    The user_id predicate is the authorization check for artifact downloads;
    a mismatch yields no row, which the route turns into a 404.
    """
    return connection.execute(
        "SELECT * FROM artifacts WHERE id = ? AND user_id = ?", (artifact_id, user_id)
    ).fetchone()
