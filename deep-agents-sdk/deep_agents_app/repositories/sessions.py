import sqlite3


def messages(connection: sqlite3.Connection, user_id: str, project_id: str, session_id: str):
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
    return connection.execute(
        """
        SELECT * FROM artifacts
        WHERE user_id = ? AND project_id = ? AND session_id = ?
        ORDER BY created_at ASC
        """,
        (user_id, project_id, session_id),
    ).fetchall()


def runs(connection: sqlite3.Connection, user_id: str, project_id: str, session_id: str):
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
    return connection.execute(
        "SELECT * FROM artifacts WHERE id = ? AND user_id = ?", (artifact_id, user_id)
    ).fetchone()
