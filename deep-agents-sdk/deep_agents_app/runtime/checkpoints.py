"""LangGraph checkpoint storage maintenance.

This module deliberately depends only on :mod:`deep_agents_app.services.workspace`
so that session services can delete a thread without importing the agent engine,
which in turn depends on those session services.
"""

import sqlite3

from deep_agents_app.services import workspace as state


def delete_checkpoint_thread(thread_id: str) -> None:
    if not state.AGENT_CHECKPOINT_DB_PATH.exists():
        return
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(
        state.AGENT_CHECKPOINT_DB_PATH, timeout=10, check_same_thread=False
    )
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        saver = SqliteSaver(conn)
        saver.setup()
        saver.delete_thread(thread_id)
    finally:
        conn.close()
