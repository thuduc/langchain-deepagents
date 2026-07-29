"""Tests for upgrading a database that predates the current schema.

Every other suite starts from an empty file, where `CREATE TABLE IF NOT EXISTS`
builds the current shape and nothing is ever migrated. That path proves almost
nothing about a deployment's real one, where the tables already exist and only
the patches in `initialize_schema` run -- a column added to the CREATE statement
is simply not applied, and anything referring to it fails at startup on every
existing installation while the whole test suite stays green.

So these tests start from an old database on purpose.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SDK_ROOT = Path(__file__).resolve().parents[1]
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from deep_agents_app.db.schema import SCHEMA_VERSION, initialize_schema  # noqa: E402


# task_runs as released before runs recorded an owner or a heartbeat.
LEGACY_SCHEMA_SQL = """
CREATE TABLE task_runs (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT NOT NULL,
    user_id TEXT NOT NULL, project_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'cancelled')),
    prompt TEXT NOT NULL, error_summary TEXT, created_at TEXT NOT NULL, completed_at TEXT
);
"""

LEGACY_RUN = (
    "r1", "p1", "s1", "u1", 1, "running", "an unfinished prompt", None,
    "2026-01-01T00:00:00+00:00", None,
)


class LegacyDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="schema-migration-")) / "app.db"
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript(LEGACY_SCHEMA_SQL)
        self.connection.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", LEGACY_RUN
        )
        self.connection.commit()

    def _columns(self, table):
        return {row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")}

    def _indexes(self):
        return {
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    def test_columns_added_after_the_first_release_are_patched_in(self):
        initialize_schema(self.connection)
        self.assertLessEqual(
            {"latest_status", "owner_id", "heartbeat_at"}, self._columns("task_runs")
        )

    def test_an_index_over_a_migrated_column_is_built(self):
        """The regression this guards: declaring it alongside the tables meant it
        referenced a column that an existing database did not have yet, and
        startup failed there while every fresh-database test passed."""
        initialize_schema(self.connection)
        self.assertIn("idx_runs_liveness", self._indexes())

    def test_existing_rows_survive_with_the_new_columns_empty(self):
        initialize_schema(self.connection)
        row = self.connection.execute("SELECT * FROM task_runs WHERE id = 'r1'").fetchone()
        self.assertEqual(row["prompt"], "an unfinished prompt")
        self.assertEqual(row["status"], "running")
        # A run already in the table predates this process, so no owner is the
        # truth about it -- and what marks it abandoned rather than someone's
        # live work.
        self.assertIsNone(row["owner_id"])
        self.assertIsNone(row["heartbeat_at"])

    def test_the_version_is_recorded(self):
        initialize_schema(self.connection)
        self.assertEqual(
            self.connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
        )

    def test_migrating_twice_changes_nothing(self):
        initialize_schema(self.connection)
        initialize_schema(self.connection)
        self.assertLessEqual({"owner_id", "heartbeat_at"}, self._columns("task_runs"))
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0], 1
        )


class EmptyDatabaseTests(unittest.TestCase):
    def test_a_fresh_database_reaches_the_same_shape(self):
        """The two paths must converge, which is the point of testing both."""
        path = Path(tempfile.mkdtemp(prefix="schema-fresh-")) / "app.db"
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)

        initialize_schema(connection)

        columns = {row["name"] for row in connection.execute("PRAGMA table_info(task_runs)")}
        self.assertLessEqual({"owner_id", "heartbeat_at", "latest_status"}, columns)
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        self.assertIn("idx_runs_liveness", indexes)


if __name__ == "__main__":
    unittest.main()
