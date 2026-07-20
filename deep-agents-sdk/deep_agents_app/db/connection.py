import sqlite3
from pathlib import Path


class ClosingSQLiteConnection(sqlite3.Connection):
    """Commit or roll back a context-managed connection and always close it."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        database_path,
        timeout=10,
        check_same_thread=False,
        factory=ClosingSQLiteConnection,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL;")
    connection.execute("PRAGMA busy_timeout = 5000;")
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection
