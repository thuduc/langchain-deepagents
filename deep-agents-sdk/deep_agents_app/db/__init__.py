"""SQLite access: connection factory and schema management."""

from .connection import connect
from .schema import initialize_schema

__all__ = ["connect", "initialize_schema"]
