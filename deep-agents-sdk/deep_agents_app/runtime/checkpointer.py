"""Where conversation memory lives.

    DEEP_AGENTS_CHECKPOINTER=sqlite     a file under DEEP_AGENTS_DB_DIR
    DEEP_AGENTS_CHECKPOINTER=dynamodb   a table any process can reach

LangGraph writes a checkpoint after every step of a run, and reads them back to
resume a conversation, so this is the store that decides whether a chat survives.
Losing it is quieter than it sounds: the transcript the UI shows comes from
chat_messages in the application database, so the conversation still displays --
the agent simply answers the next question as though it had never spoken to the
user before.

SQLite is correct while the agent runs inside the application's own process. Once
it runs anywhere else -- an AgentCore Runtime microVM, a second task -- that file
is unreachable, and the memory has to live somewhere both sides can see.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Callable, Optional


DEFAULT_CHECKPOINTER = "sqlite"

# DynamoDB rejects items over 400 KB, and a long conversation's message list will
# pass that. The saver offloads anything over its own threshold to S3 instead, so
# a bucket is configured whenever one is available rather than only on request.
DEFAULT_CHECKPOINT_S3_PREFIX = "checkpoints"


class CheckpointerError(RuntimeError):
    """A checkpointer is misconfigured, or cannot be reached."""


class Checkpointer:
    """A LangGraph saver together with whatever resources it owns.

    SQLite hands out a connection that has to be closed when the graph holding it
    is discarded; DynamoDB owns nothing that needs releasing. Callers close the
    handle either way rather than having to know which backend they were given.
    """

    def __init__(self, saver: Any, close: Optional[Callable[[], None]] = None) -> None:
        self.saver = saver
        self._close = close

    def close(self) -> None:
        """Release the saver's resources. Safe to call more than once."""
        if self._close is not None:
            self._close()
            self._close = None


def configured_checkpointer_name() -> str:
    """The backend named by DEEP_AGENTS_CHECKPOINTER, defaulting to sqlite."""
    return (
        os.environ.get("DEEP_AGENTS_CHECKPOINTER") or DEFAULT_CHECKPOINTER
    ).strip().lower()


def _build_sqlite() -> Checkpointer:
    """A checkpoint database on this host's disk.

    WAL lets a reader proceed while a write is in flight, and the busy timeout
    absorbs the contention between concurrent runs. Both require host-local
    shared memory, which is why this file must stay on a real disk and never on
    a network filesystem.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    from deep_agents_app.services import workspace as state

    state.AGENT_CHECKPOINT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        state.AGENT_CHECKPOINT_DB_PATH, timeout=10, check_same_thread=False
    )
    connection.execute("PRAGMA journal_mode = WAL;")
    connection.execute("PRAGMA busy_timeout = 5000;")
    saver = SqliteSaver(connection)
    saver.setup()
    return Checkpointer(saver, connection.close)


def _build_dynamodb() -> Checkpointer:
    """A checkpoint table shared by every process that can reach the account.

    Large checkpoints are offloaded to S3 when a bucket is configured, because a
    conversation's accumulated message list eventually exceeds DynamoDB's item
    size limit and the write would otherwise start failing partway through a
    long chat.
    """
    from langgraph_checkpoint_aws import DynamoDBSaver

    table = os.environ.get("CHECKPOINT_DDB_TABLE", "").strip()
    if not table:
        raise CheckpointerError(
            "CHECKPOINT_DDB_TABLE is required for the dynamodb checkpointer"
        )

    region = (
        os.environ.get("CHECKPOINT_DDB_REGION")
        or os.environ.get("AGENTCORE_REGION")
        or None
    )

    offload = None
    bucket = (
        os.environ.get("CHECKPOINT_S3_BUCKET")
        or os.environ.get("AGENTCORE_BUCKET")
        or ""
    ).strip()
    if bucket:
        offload = {
            "bucket_name": bucket,
            "key_prefix": os.environ.get(
                "CHECKPOINT_S3_PREFIX", DEFAULT_CHECKPOINT_S3_PREFIX
            ).strip("/"),
        }

    saver = DynamoDBSaver(
        table_name=table,
        region_name=region,
        ttl_seconds=_ttl_seconds(),
        s3_offload_config=offload,
    )
    return Checkpointer(saver)


def _ttl_seconds() -> Optional[int]:
    """Expiry for stored checkpoints, or None to keep them indefinitely.

    Off by default for two reasons. A checkpoint expiring under a conversation
    the user can still see gives the agent amnesia with no visible cause -- the
    transcript survives in chat_messages either way.

    The second is subtler: setting this makes the saver add its own S3 lifecycle
    rule to the offload bucket, read-modify-write, on top of whatever rules are
    already there. The sandbox bucket's lifecycle configuration is owned by
    CloudFormation, so the two would overwrite each other in turn. If retention
    is required, point CHECKPOINT_S3_BUCKET at a bucket the stack does not
    manage, or add the rule to the template and withhold
    s3:PutLifecycleConfiguration from the application.
    """
    raw = os.environ.get("CHECKPOINT_DDB_TTL_DAYS", "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError as exc:
        raise CheckpointerError(
            f"CHECKPOINT_DDB_TTL_DAYS must be a whole number of days, got {raw!r}"
        ) from exc
    return days * 24 * 60 * 60 if days > 0 else None


BACKENDS = {
    "sqlite": _build_sqlite,
    "dynamodb": _build_dynamodb,
}


def create_checkpointer(name: Optional[str] = None) -> Checkpointer:
    """Open a checkpointer. The caller owns it and must close it."""
    selected = (name or configured_checkpointer_name()).lower()
    if selected not in BACKENDS:
        raise CheckpointerError(
            f"Unknown checkpointer {selected!r}; expected one of "
            f"{', '.join(sorted(BACKENDS))}"
        )
    return BACKENDS[selected]()


def delete_thread(thread_id: str) -> None:
    """Erase one conversation's stored memory when its session is deleted."""
    handle = create_checkpointer()
    try:
        handle.saver.delete_thread(thread_id)
    finally:
        handle.close()


def preflight() -> None:
    """Prove the configured store is reachable, at startup rather than mid-run.

    Reads a checkpoint that will not exist, which exercises the credentials, the
    table and the region without writing anything.
    """
    handle = create_checkpointer()
    try:
        handle.saver.get_tuple(
            {"configurable": {"thread_id": "preflight", "checkpoint_ns": ""}}
        )
    except Exception as exc:  # noqa: BLE001 - reported as a configuration failure
        raise CheckpointerError(
            f"Checkpointer {configured_checkpointer_name()!r} is not usable: {exc}"
        ) from exc
    finally:
        handle.close()
