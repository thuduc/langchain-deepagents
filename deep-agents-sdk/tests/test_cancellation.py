"""Tests for the run cancellation channel.

The local store is exercised for real. The DynamoDB store runs against a stub
client, which verifies the key space and the fail-open behaviour without AWS.
"""

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SDK_ROOT = Path(__file__).resolve().parents[1]
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from deep_agents_app.domain import ProjectContext  # noqa: E402
from deep_agents_app.services import workspace  # noqa: E402, F401  (engine imports it)
from deep_agents_app.runtime import cancellation, engine  # noqa: E402
from deep_agents_app.runtime.cancellation import (  # noqa: E402
    DynamoDBCancellationStore,
    LocalCancellationStore,
    RunCancelled,
    configured_store_name,
    create_store,
)


def _project():
    """A minimal project descriptor; these tests never read its files."""
    return ProjectContext(
        id="p", name="P", slug="p", root=Path("/projects/p"),
        content_revision=1, model="gpt-5.5",
    )


class SelectionTests(unittest.TestCase):
    def setUp(self):
        cancellation.reset_store()
        self.addCleanup(cancellation.reset_store)

    def test_defaults_to_local(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEP_AGENTS_CANCELLATION", None)
            self.assertEqual(configured_store_name(), "local")

    def test_unknown_store_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown cancellation store"):
            create_store("carrier-pigeon")


class LocalStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = LocalCancellationStore()

    def test_a_run_is_not_cancelled_until_asked(self):
        self.assertFalse(self.store.is_cancelled("run-1"))

    def test_request_then_clear(self):
        self.store.request("run-1")
        self.assertTrue(self.store.is_cancelled("run-1"))
        self.store.clear("run-1")
        self.assertFalse(self.store.is_cancelled("run-1"))

    def test_runs_do_not_affect_each_other(self):
        self.store.request("run-1")
        self.assertFalse(self.store.is_cancelled("run-2"))

    def test_clearing_an_unknown_run_is_harmless(self):
        self.store.clear("never-seen")


class StubDynamoClient:
    """Records calls and serves whatever items were put."""

    def __init__(self, fail=False):
        self.items = {}
        self.calls = []
        self.fail = fail

    def _key(self, key):
        return (key["PK"]["S"], key["SK"]["S"])

    def put_item(self, TableName, Item):
        self.calls.append(("put_item", TableName))
        if self.fail:
            raise RuntimeError("throttled")
        self.items[self._key(Item)] = Item

    def get_item(self, TableName, Key, ConsistentRead=False):
        self.calls.append(("get_item", TableName))
        if self.fail:
            raise RuntimeError("throttled")
        item = self.items.get(self._key(Key))
        return {"Item": item} if item else {}

    def batch_write_item(self, RequestItems):
        self.calls.append(("batch_write_item", next(iter(RequestItems))))
        if self.fail:
            raise RuntimeError("throttled")
        for table, requests in RequestItems.items():
            for request in requests:
                self.items.pop(self._key(request["DeleteRequest"]["Key"]), None)


class DynamoDBStoreTests(unittest.TestCase):
    def setUp(self):
        self.client = StubDynamoClient()
        self.store = DynamoDBCancellationStore(
            table_name="deepagents-checkpoints", client=self.client
        )

    def test_round_trip(self):
        self.assertFalse(self.store.is_cancelled("run-1"))
        self.store.request("run-1")
        self.assertTrue(self.store.is_cancelled("run-1"))
        self.store.clear("run-1")
        self.assertFalse(self.store.is_cancelled("run-1"))

    def test_keys_do_not_collide_with_checkpoints(self):
        """The checkpoint saver owns this table; its keys must stay disjoint."""
        self.store.request("run-1")
        (pk, sk), item = next(iter(self.client.items.items()))

        self.assertEqual(pk, "CANCEL#run-1")
        self.assertEqual(sk, "CANCEL")
        self.assertNotIn("CHUNK", pk)
        self.assertIn("ttl", item)

    def test_reads_are_strongly_consistent(self):
        """An eventually-consistent read could miss a cancellation for seconds."""
        self.store.is_cancelled("run-1")

        with mock.patch.object(self.client, "get_item", wraps=self.client.get_item) as spied:
            self.store.is_cancelled("run-1")

        self.assertIs(spied.call_args.kwargs["ConsistentRead"], True)

    def test_deletion_uses_batch_write(self):
        """DeleteItem is deliberately absent from the table's IAM policy."""
        self.store.request("run-1")
        self.store.clear("run-1")

        self.assertIn("batch_write_item", [call[0] for call in self.client.calls])

    def test_an_unreadable_store_does_not_cancel_the_run(self):
        """Failing open: a throttled read must not look like a cancellation."""
        self.store._client = StubDynamoClient(fail=True)

        self.assertFalse(self.store.is_cancelled("run-1"))

    def test_recording_a_cancellation_never_raises(self):
        """The caller has already gone; there is nobody to report an error to."""
        self.store._client = StubDynamoClient(fail=True)

        self.store.request("run-1")
        self.store.clear("run-1")


class WatchTests(unittest.TestCase):
    def setUp(self):
        cancellation.reset_store()
        self.addCleanup(cancellation.reset_store)
        env = mock.patch.dict(os.environ, {"DEEP_AGENTS_CANCELLATION": "local"})
        env.start()
        self.addCleanup(env.stop)

    def test_it_notices_a_cancellation(self):
        watch = cancellation.watch("run-1", poll_seconds=0)
        self.assertFalse(watch.cancelled())

        cancellation.request("run-1")

        self.assertTrue(watch.cancelled())

    def test_polling_is_rate_limited(self):
        """The streaming loop asks on every turn; the store must not see them all."""
        store = cancellation.get_store()
        with mock.patch.object(store, "is_cancelled", return_value=False) as spied:
            watch = cancellation.watch("run-1", poll_seconds=60)
            for _ in range(100):
                watch.cancelled()

        self.assertEqual(spied.call_count, 1)

    def test_a_cancelled_run_stops_asking(self):
        cancellation.request("run-1")
        watch = cancellation.watch("run-1", poll_seconds=0)
        self.assertTrue(watch.cancelled())

        store = cancellation.get_store()
        with mock.patch.object(store, "is_cancelled") as spied:
            self.assertTrue(watch.cancelled())

        spied.assert_not_called()

    def test_a_request_from_another_thread_is_seen(self):
        """The real shape of this: one thread notices, another is running."""
        watch = cancellation.watch("run-1", poll_seconds=0)
        thread = threading.Thread(target=lambda: cancellation.request("run-1"))
        thread.start()
        thread.join()

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not watch.cancelled():
            time.sleep(0.01)

        self.assertTrue(watch.cancelled())


class ToolRefusalTests(unittest.TestCase):
    """The sandbox tool refuses once a run is cancelled.

    This is the half of cancellation that actually saves anything: it stops a
    new sandbox session being opened for work nobody is waiting for.
    """

    def setUp(self):
        cancellation.reset_store()
        self.addCleanup(cancellation.reset_store)
        env = mock.patch.dict(os.environ, {"DEEP_AGENTS_CANCELLATION": "local"})
        env.start()
        self.addCleanup(env.stop)

    def test_execute_python_refuses_a_cancelled_run(self):
        context = mock.Mock(run_id="run-1", project_id="p", user_id="u", session_id="s")
        cancellation.request("run-1")

        token = engine.service_state.CURRENT_RUN_CONTEXT.set(context)
        self.addCleanup(engine.service_state.CURRENT_RUN_CONTEXT.reset, token)

        with self.assertRaises(RunCancelled):
            engine.execute_python_code("print(1)", _project())

    def test_execute_python_proceeds_when_not_cancelled(self):
        """The check must not become a reason healthy runs fail."""
        context = mock.Mock(run_id="run-1", project_id="p", user_id="u", session_id="s")

        token = engine.service_state.CURRENT_RUN_CONTEXT.set(context)
        self.addCleanup(engine.service_state.CURRENT_RUN_CONTEXT.reset, token)

        with mock.patch.object(engine.sandbox_runs, "session_for_run",
                               side_effect=RuntimeError("reached the sandbox")):
            # Reaching the sandbox at all proves the guard let it past.
            result = engine.execute_python_code("print(1)", _project())

        self.assertIn("sandbox was unavailable", result)


if __name__ == "__main__":
    unittest.main()
