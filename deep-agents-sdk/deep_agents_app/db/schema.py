"""Database schema and startup migrations.

Ownership is expressed in the tables themselves: chat_sessions, task_runs,
chat_messages and artifacts all carry user_id and cascade on delete, so removing
a user or a session takes their private data with it. Projects carry no user_id
because project content is shared.
"""

import sqlite3


SCHEMA_VERSION = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, issuer TEXT NOT NULL, subject TEXT NOT NULL,
    created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, UNIQUE(issuer, subject)
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'deleting')),
    content_revision INTEGER NOT NULL DEFAULT 1, created_by TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id TEXT NOT NULL,
    title TEXT NOT NULL, thread_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT NOT NULL,
    user_id TEXT NOT NULL, project_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'cancelled')),
    latest_status TEXT NOT NULL DEFAULT 'Preparing the agent workspace…',
    prompt TEXT NOT NULL, error_summary TEXT, created_at TEXT NOT NULL, completed_at TEXT,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    session_id TEXT NOT NULL, user_id TEXT NOT NULL, run_id TEXT,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(run_id) REFERENCES task_runs(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT NOT NULL,
    run_id TEXT NOT NULL, user_id TEXT NOT NULL, storage_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL, media_type TEXT NOT NULL, size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(run_id) REFERENCES task_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS upload_previews (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, storage_key TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL, consumed_at TEXT, created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, action TEXT NOT NULL,
    project_id TEXT, details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sessions_owner_project_updated
    ON chat_sessions(user_id, project_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_owner_session
    ON chat_messages(user_id, session_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_owner_session
    ON task_runs(user_id, session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_owner_run
    ON artifacts(user_id, run_id, created_at);
"""


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create the schema if absent and apply in-place migrations.

    Runs on every startup and is idempotent. Columns added after the first
    release are patched in explicitly, because CREATE TABLE IF NOT EXISTS will
    not alter a table that already exists.
    """
    connection.executescript(SCHEMA_SQL)
    task_run_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(task_runs)").fetchall()
    }
    if "latest_status" not in task_run_columns:
        connection.execute(
            "ALTER TABLE task_runs ADD COLUMN latest_status TEXT NOT NULL "
            "DEFAULT 'Preparing the agent workspace…'"
        )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (SCHEMA_VERSION,)
    )
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
