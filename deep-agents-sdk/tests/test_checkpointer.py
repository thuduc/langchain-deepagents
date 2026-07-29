"""Tests for conversation-memory storage.

The SQLite backend is exercised for real, including a round trip through
LangGraph's saver interface. The DynamoDB backend is exercised against a stub
constructor, which verifies configuration without needing AWS.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SDK_ROOT = Path(__file__).resolve().parents[1]
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from deep_agents_app.runtime import checkpointer  # noqa: E402
from deep_agents_app.runtime.checkpointer import (  # noqa: E402
    Checkpointer,
    CheckpointerError,
    configured_checkpointer_name,
    create_checkpointer,
)
from deep_agents_app.services import workspace as state  # noqa: E402


def _config(thread_id):
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


class SelectionTests(unittest.TestCase):
    def test_defaults_to_sqlite(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEP_AGENTS_CHECKPOINTER", None)
            self.assertEqual(configured_checkpointer_name(), "sqlite")

    def test_reads_the_environment(self):
        with mock.patch.dict(os.environ, {"DEEP_AGENTS_CHECKPOINTER": " DynamoDB "}):
            self.assertEqual(configured_checkpointer_name(), "dynamodb")

    def test_unknown_backend_is_rejected(self):
        with self.assertRaisesRegex(CheckpointerError, "Unknown checkpointer"):
            create_checkpointer("redis")


class HandleTests(unittest.TestCase):
    def test_close_runs_once_and_is_idempotent(self):
        calls = []
        handle = Checkpointer(saver=object(), close=lambda: calls.append(1))

        handle.close()
        handle.close()

        self.assertEqual(calls, [1])

    def test_close_is_safe_without_resources(self):
        Checkpointer(saver=object()).close()  # must not raise


class SqliteCheckpointerTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="deep-agents-checkpoint-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        patched = mock.patch.object(
            state, "AGENT_CHECKPOINT_DB_PATH", self.tmp_dir / "nested" / "agent.db"
        )
        patched.start()
        self.addCleanup(patched.stop)

    def test_a_checkpoint_survives_a_round_trip(self):
        """The whole point of the store: a later turn can read an earlier one."""
        handle = create_checkpointer("sqlite")
        self.addCleanup(handle.close)

        saved = handle.saver.put(
            _config("thread-a"),
            {"v": 4, "id": "chk-1", "ts": "2026-01-01T00:00:00+00:00", "channel_values": {}, "channel_versions": {}, "versions_seen": {}},
            {"source": "input", "step": 0},
            {},
        )

        restored = handle.saver.get_tuple(saved)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.checkpoint["id"], "chk-1")

    def test_the_parent_directory_is_created(self):
        handle = create_checkpointer("sqlite")
        self.addCleanup(handle.close)

        self.assertTrue(state.AGENT_CHECKPOINT_DB_PATH.parent.is_dir())

    def test_close_releases_the_connection(self):
        handle = create_checkpointer("sqlite")
        handle.close()

        with self.assertRaises(Exception):
            handle.saver.get_tuple(_config("thread-a"))

    def test_delete_thread_removes_a_conversation(self):
        with mock.patch.dict(os.environ, {"DEEP_AGENTS_CHECKPOINTER": "sqlite"}):
            handle = create_checkpointer("sqlite")
            config = handle.saver.put(
                _config("thread-b"),
                {"v": 4, "id": "chk-1", "ts": "2026-01-01T00:00:00+00:00", "channel_values": {}, "channel_versions": {}, "versions_seen": {}},
                {"source": "input", "step": 0},
                {},
            )
            self.assertIsNotNone(handle.saver.get_tuple(config))
            handle.close()

            checkpointer.delete_thread("thread-b")

            probe = create_checkpointer("sqlite")
            self.addCleanup(probe.close)
            self.assertIsNone(probe.saver.get_tuple(_config("thread-b")))

    def test_preflight_accepts_a_working_store(self):
        with mock.patch.dict(os.environ, {"DEEP_AGENTS_CHECKPOINTER": "sqlite"}):
            checkpointer.preflight()  # must not raise


class DynamoDBCheckpointerTests(unittest.TestCase):
    """Configuration only; the saver itself belongs to langgraph-checkpoint-aws."""

    def setUp(self):
        self.built = {}

        def record(**kwargs):
            self.built.update(kwargs)
            return mock.Mock()

        patched = mock.patch(
            "langgraph_checkpoint_aws.DynamoDBSaver", side_effect=record
        )
        patched.start()
        self.addCleanup(patched.stop)

    def _env(self, **overrides):
        base = {
            "CHECKPOINT_DDB_TABLE": "deepagents-checkpoints",
            "AGENTCORE_REGION": "us-east-1",
            "AGENTCORE_BUCKET": "deepagents-test",
        }
        base.update(overrides)
        for key in ("CHECKPOINT_DDB_REGION", "CHECKPOINT_S3_BUCKET", "CHECKPOINT_DDB_TTL_DAYS"):
            base.setdefault(key, "")
        return mock.patch.dict(os.environ, base)

    def test_table_name_is_required(self):
        with mock.patch.dict(os.environ, {"CHECKPOINT_DDB_TABLE": ""}):
            with self.assertRaisesRegex(CheckpointerError, "CHECKPOINT_DDB_TABLE"):
                create_checkpointer("dynamodb")

    def test_configuration_comes_from_the_environment(self):
        with self._env():
            create_checkpointer("dynamodb")

        self.assertEqual(self.built["table_name"], "deepagents-checkpoints")
        self.assertEqual(self.built["region_name"], "us-east-1")

    def test_large_checkpoints_are_offloaded_to_s3(self):
        """DynamoDB caps an item at 400 KB; a long conversation exceeds it."""
        with self._env():
            create_checkpointer("dynamodb")

        self.assertEqual(
            self.built["s3_offload_config"],
            {"bucket_name": "deepagents-test", "key_prefix": "checkpoints"},
        )

    def test_a_dedicated_offload_bucket_wins(self):
        with self._env(CHECKPOINT_S3_BUCKET="separate-bucket"):
            create_checkpointer("dynamodb")

        self.assertEqual(self.built["s3_offload_config"]["bucket_name"], "separate-bucket")

    def test_retention_is_off_by_default(self):
        """Also keeps the saver from managing the bucket's lifecycle rules."""
        with self._env():
            create_checkpointer("dynamodb")

        self.assertIsNone(self.built["ttl_seconds"])

    def test_retention_is_converted_to_seconds(self):
        with self._env(CHECKPOINT_DDB_TTL_DAYS="30"):
            create_checkpointer("dynamodb")

        self.assertEqual(self.built["ttl_seconds"], 30 * 24 * 60 * 60)

    def test_zero_retention_means_keep_indefinitely(self):
        with self._env(CHECKPOINT_DDB_TTL_DAYS="0"):
            create_checkpointer("dynamodb")

        self.assertIsNone(self.built["ttl_seconds"])

    def test_unparseable_retention_is_rejected(self):
        with self._env(CHECKPOINT_DDB_TTL_DAYS="a month"):
            with self.assertRaisesRegex(CheckpointerError, "CHECKPOINT_DDB_TTL_DAYS"):
                create_checkpointer("dynamodb")


if __name__ == "__main__":
    unittest.main()
