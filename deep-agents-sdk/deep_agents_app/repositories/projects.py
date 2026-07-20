import sqlite3


def list_active(connection: sqlite3.Connection):
    return connection.execute(
        "SELECT * FROM projects WHERE status = 'active' ORDER BY updated_at DESC, name ASC"
    ).fetchall()


def update_name(connection: sqlite3.Connection, project_id: str, name: str, updated_at: str):
    connection.execute(
        "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
        (name, updated_at, project_id),
    )
    return connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
