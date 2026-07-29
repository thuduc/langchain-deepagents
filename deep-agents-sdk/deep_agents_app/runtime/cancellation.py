"""Stopping a run that nobody is listening to any more.

When a user navigates away mid-run the browser's connection drops and the
streaming response raises GeneratorExit. While the agent runs in this process
that is enough on its own: the generator that notices is the same one holding the
sandbox session, and its finally block releases everything.

Once the agent runs inside an AgentCore Runtime microVM, that stops being true.
The disconnect is seen by the web tier, while the work continues in a different
process on different compute, holding a Code Interpreter session that bills by
the second. Something has to carry the signal across the gap.

    DEEP_AGENTS_CANCELLATION=local      an in-process registry
    DEEP_AGENTS_CANCELLATION=dynamodb   a record the other side polls

Who does what does not change with the transport: **the side that notices the
disconnect calls request(); the side running the graph calls clear() when it
finishes.** Today both are `stream_agent_events`. Split across the boundary,
request() stays with the web tier and clear() travels with the loop.

Cancellation is coarse, and more so than is comfortable. Two limits, both
measured against a live run rather than assumed:

  * The response stops promptly -- about a second from request to the stream
    ending, and the run is marked cancelled at once.
  * The *work* does not. Closing a LangGraph stream waits for the graph rather
    than aborting it, and a tool that raises is caught by LangGraph's ToolNode
    and reported back to the model instead of propagating. Refusing new sandbox
    executions roughly halves what a cancelled run goes on to consume; the graph
    still runs its plan to the end.

That is survivable in-process, where the only cost is CPU nobody is watching. It
is not survivable on AgentCore Runtime, where it is a microVM billing by the
second. The invocation boundary is the answer there: the handler should return
as soon as cancellation is seen and let the invocation end, rather than waiting
for the graph to unwind tidily.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Optional


logger = logging.getLogger(__name__)

DEFAULT_STORE = "local"

# How often the running side may ask whether it has been cancelled. The streaming
# loop turns over many times a second while tokens arrive, and asking a remote
# store at that rate would cost more than the run does.
DEFAULT_POLL_SECONDS = 2.0

# A cancellation record is only interesting while its run is alive. The ceiling
# exists so a record whose run died without clearing it cannot linger.
RECORD_TTL_SECONDS = 3600


class RunCancelled(RuntimeError):
    """Raised inside the agent loop when the caller has stopped listening."""


class CancellationStore(ABC):
    """Where a request to stop a run is recorded."""

    name: str

    @abstractmethod
    def request(self, run_id: str) -> None:
        """Record that a run should stop. Must not raise."""

    @abstractmethod
    def is_cancelled(self, run_id: str) -> bool:
        """Whether a stop was requested. Must not raise; False when unknown."""

    @abstractmethod
    def clear(self, run_id: str) -> None:
        """Forget a run. Must not raise."""


class LocalCancellationStore(CancellationStore):
    """An in-process set. Correct while the agent runs in the web tier."""

    name = "local"

    def __init__(self) -> None:
        self._cancelled: set[str] = set()
        self._guard = threading.Lock()

    def request(self, run_id: str) -> None:
        with self._guard:
            self._cancelled.add(run_id)

    def is_cancelled(self, run_id: str) -> bool:
        with self._guard:
            return run_id in self._cancelled

    def clear(self, run_id: str) -> None:
        with self._guard:
            self._cancelled.discard(run_id)


class DynamoDBCancellationStore(CancellationStore):
    """A record in the checkpoint table, which both sides can already reach.

    Sharing that table rather than provisioning another is a deliberate
    prototype trade: the key space is disjoint from the checkpoint saver's, TTL
    is already enabled on it, and the existing IAM policy already covers the
    calls. If cancellation ever grows beyond a flag it should get its own table.
    """

    name = "dynamodb"

    def __init__(
        self,
        table_name: Optional[str] = None,
        region_name: Optional[str] = None,
        client: Any = None,
    ) -> None:
        self.table_name = table_name or os.environ.get("CHECKPOINT_DDB_TABLE", "")
        self.region_name = (
            region_name
            or os.environ.get("CHECKPOINT_DDB_REGION")
            or os.environ.get("AGENTCORE_REGION")
            or None
        )
        self._client = client

    def client(self) -> Any:
        """The DynamoDB client, built on first use so local runs need no boto3."""
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb", region_name=self.region_name)
        return self._client

    @staticmethod
    def _key(run_id: str) -> dict:
        return {"PK": {"S": f"CANCEL#{run_id}"}, "SK": {"S": "CANCEL"}}

    def request(self, run_id: str) -> None:
        try:
            item = dict(self._key(run_id))
            item["ttl"] = {"N": str(int(time.time()) + RECORD_TTL_SECONDS)}
            self.client().put_item(TableName=self.table_name, Item=item)
        except Exception as exc:  # noqa: BLE001 - the caller has already gone
            logger.warning("Could not record cancellation for run %s: %s", run_id, exc)

    def is_cancelled(self, run_id: str) -> bool:
        try:
            response = self.client().get_item(
                TableName=self.table_name,
                Key=self._key(run_id),
                ConsistentRead=True,
            )
        except Exception as exc:  # noqa: BLE001 - never kill a run over a read
            # Failing open matters here: a throttled or unreachable store must
            # not look like a cancellation and abort work that is going fine.
            logger.warning("Could not read cancellation for run %s: %s", run_id, exc)
            return False
        return "Item" in response

    def clear(self, run_id: str) -> None:
        try:
            # BatchWriteItem rather than DeleteItem: the table's policy grants
            # the former, which is also how the checkpoint saver deletes.
            self.client().batch_write_item(
                RequestItems={
                    self.table_name: [{"DeleteRequest": {"Key": self._key(run_id)}}]
                }
            )
        except Exception as exc:  # noqa: BLE001 - the record expires on its own
            logger.warning("Could not clear cancellation for run %s: %s", run_id, exc)


BACKENDS = {
    "local": LocalCancellationStore,
    "dynamodb": DynamoDBCancellationStore,
}

_store: Optional[CancellationStore] = None
_store_guard = threading.Lock()


def configured_store_name() -> str:
    """The store named by DEEP_AGENTS_CANCELLATION, defaulting to local."""
    return (os.environ.get("DEEP_AGENTS_CANCELLATION") or DEFAULT_STORE).strip().lower()


def create_store(name: Optional[str] = None) -> CancellationStore:
    """Build a store without caching it. Useful in tests."""
    selected = (name or configured_store_name()).lower()
    if selected not in BACKENDS:
        raise ValueError(
            f"Unknown cancellation store {selected!r}; expected one of "
            f"{', '.join(sorted(BACKENDS))}"
        )
    return BACKENDS[selected]()


def get_store() -> CancellationStore:
    """Return the process-wide store, building it once."""
    global _store
    if _store is not None:
        return _store
    with _store_guard:
        if _store is None:
            _store = create_store()
    return _store


def reset_store() -> None:
    """Drop the cached store. Intended for tests and configuration reloads."""
    global _store
    with _store_guard:
        _store = None


def request(run_id: str) -> None:
    """Ask a run to stop. Called by whoever noticed the caller leave."""
    get_store().request(run_id)


def clear(run_id: str) -> None:
    """Forget a run. Called by whoever was running it, once it has stopped."""
    get_store().clear(run_id)


def is_cancelled(run_id: str) -> bool:
    """Ask the store directly, bypassing the watch's rate limit.

    For the few places worth paying a read on every call: chiefly the tool that
    hands work to the sandbox, where the alternative is starting an execution
    nobody is waiting for.
    """
    return get_store().is_cancelled(run_id)


class CancellationWatch:
    """A rate-limited view of one run's cancellation state.

    Answers from memory between checks so the streaming loop can ask on every
    turn without the cost landing on a remote store. Once cancelled it stays
    cancelled, so no further reads happen either.
    """

    def __init__(self, run_id: str, poll_seconds: Optional[float] = None) -> None:
        self.run_id = run_id
        self._interval = poll_seconds if poll_seconds is not None else _poll_seconds()
        self._cancelled = False
        self._checked_at = 0.0

    def cancelled(self) -> bool:
        """Whether this run has been asked to stop."""
        if self._cancelled:
            return True
        now = time.monotonic()
        if now - self._checked_at < self._interval:
            return False
        self._checked_at = now
        self._cancelled = get_store().is_cancelled(self.run_id)
        return self._cancelled


def watch(run_id: str, poll_seconds: Optional[float] = None) -> CancellationWatch:
    """Start watching a run for cancellation."""
    return CancellationWatch(run_id, poll_seconds)


def _poll_seconds() -> float:
    raw = os.environ.get("DEEP_AGENTS_CANCELLATION_POLL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_POLL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring unparseable DEEP_AGENTS_CANCELLATION_POLL_SECONDS=%r", raw)
        return DEFAULT_POLL_SECONDS
    return max(0.0, value)
