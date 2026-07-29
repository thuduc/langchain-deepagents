"""Contract tests for the sandbox backends.

The local backend is exercised for real. The AgentCore backend is exercised
against a stub boto3 session, which verifies the call sequence, the session tags,
and the scoping of the generated session policy without needing AWS.
"""

import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


SDK_ROOT = Path(__file__).resolve().parents[1]
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from deep_agents_app.runtime.sandbox import (  # noqa: E402
    SandboxError,
    SessionSpec,
    create_backend,
)
from deep_agents_app.runtime.sandbox import archive as archive_module  # noqa: E402
from deep_agents_app.runtime.sandbox.agentcore import AgentCoreSandbox  # noqa: E402
from deep_agents_app.runtime.sandbox.archive import extract_artifacts  # noqa: E402
from deep_agents_app.runtime.sandbox.base import (  # noqa: E402
    MAX_STREAM_CHARACTERS,
    clip_text,
    safe_relative_path,
    unique_destination,
)
from deep_agents_app.runtime.sandbox.local import LocalSandbox  # noqa: E402


class SandboxTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="deep-agents-sandbox-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.project_root = self.tmp_dir / "project"
        self.project_data = self.project_root / "data"
        self.project_data.mkdir(parents=True)
        # Skills sit alongside data and are mirrored too: the agent's prompts are
        # built by reading them, so they must exist wherever the agent runs.
        skill = self.project_root / "skills" / "pricing"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: pricing\n---\n", encoding="utf-8")
        (self.project_data / "prices.csv").write_text(
            "region,value\nsouth,110\nwest,125\n", encoding="utf-8"
        )

    def spec(self, **overrides) -> SessionSpec:
        values = {
            "user_id": "u-alice",
            "project_id": "p-hpi",
            "project_slug": "hpi-analytics",
            "session_id": "s-1",
            "run_id": "r-42",
            "project_data_dir": self.project_data,
            "timeout_seconds": 5,
        }
        values.update(overrides)
        return SessionSpec(**values)


class LocalSandboxTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.backend = LocalSandbox(base_dir=self.tmp_dir / "sandboxes")
        self.backend.preflight()

    def test_executes_code_and_captures_stdout(self):
        with self.backend.open_session(self.spec()) as session:
            result = session.execute("print('hello from the sandbox')")
        self.assertTrue(result.ok, result.stderr)
        self.assertIn("hello from the sandbox", result.stdout)

    def test_project_data_is_readable_at_data(self):
        code = "print(open('data/prices.csv').read().strip().splitlines()[-1])"
        with self.backend.open_session(self.spec()) as session:
            result = session.execute(code)
        self.assertTrue(result.ok, result.stderr)
        self.assertIn("west,125", result.stdout)

    def test_artifacts_are_collected_from_out(self):
        destination = self.tmp_dir / "artifacts"
        with self.backend.open_session(self.spec()) as session:
            result = session.execute("open('out/chart.txt', 'w').write('a chart')")
            self.assertTrue(result.ok, result.stderr)
            collected = session.collect_artifacts(destination)

        self.assertEqual([path.name for path in collected], ["chart.txt"])
        self.assertEqual(collected[0].read_text(encoding="utf-8"), "a chart")

    def test_nested_artifacts_keep_their_relative_path(self):
        destination = self.tmp_dir / "artifacts"
        code = (
            "import os\n"
            "os.makedirs('out/figures', exist_ok=True)\n"
            "open('out/figures/trend.txt', 'w').write('x')\n"
        )
        with self.backend.open_session(self.spec()) as session:
            self.assertTrue(session.execute(code).ok)
            collected = session.collect_artifacts(destination)

        self.assertEqual(
            collected[0].relative_to(destination), Path("figures/trend.txt")
        )

    def test_a_deliverable_saved_twice_is_collected_once(self):
        """Models often write a file both loose and under out/. One file, one artifact."""
        destination = self.tmp_dir / "artifacts"
        code = (
            "data = b'identical-bytes'\n"
            "open('chart.png', 'wb').write(data)\n"
            "open('out/chart.png', 'wb').write(data)\n"
        )
        with self.backend.open_session(self.spec()) as session:
            self.assertTrue(session.execute(code).ok)
            collected = session.collect_artifacts(destination)

        self.assertEqual([path.name for path in collected], ["chart.png"])

    def test_same_name_with_different_content_is_kept_separately(self):
        destination = self.tmp_dir / "artifacts"
        code = (
            "open('chart.png', 'wb').write(b'first-version')\n"
            "open('out/chart.png', 'wb').write(b'second-and-different')\n"
        )
        with self.backend.open_session(self.spec()) as session:
            self.assertTrue(session.execute(code).ok)
            collected = session.collect_artifacts(destination)

        self.assertEqual(
            sorted(path.name for path in collected), ["chart-1.png", "chart.png"]
        )

    def test_interpreter_droppings_are_not_offered_as_artifacts(self):
        """Importing a generated module writes __pycache__; it is not a deliverable."""
        destination = self.tmp_dir / "artifacts"
        code = (
            "import os\n"
            "open('helper.py', 'w').write('VALUE = 1\\n')\n"
            "os.makedirs('__pycache__', exist_ok=True)\n"
            "open('__pycache__/helper.cpython-312.pyc', 'wb').write(b'\\x00bytecode')\n"
            "open('stray.pyc', 'wb').write(b'\\x00bytecode')\n"
            "open('out/report.csv', 'w').write('a,b\\n1,2\\n')\n"
        )
        with self.backend.open_session(self.spec()) as session:
            self.assertTrue(session.execute(code).ok)
            collected = session.collect_artifacts(destination)

        self.assertEqual(
            sorted(path.name for path in collected), ["helper.py", "report.csv"]
        )

    def test_bytecode_writing_is_disabled_in_the_sandbox(self):
        with self.backend.open_session(self.spec()) as session:
            result = session.execute("import sys; print('nobytecode', sys.dont_write_bytecode)")
        self.assertIn("nobytecode True", result.stdout)

    def test_execution_times_out(self):
        with self.backend.open_session(self.spec()) as session:
            result = session.execute("import time\ntime.sleep(30)")
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)

    def test_network_is_blocked(self):
        code = "import socket\nsocket.socket(socket.AF_INET, socket.SOCK_STREAM)"
        with self.backend.open_session(self.spec()) as session:
            result = session.execute(code)
        self.assertFalse(result.ok)
        self.assertIn("Network access is disabled", result.stderr)

    def test_network_can_be_allowed_explicitly(self):
        code = (
            "import socket\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "print('opened')\n"
        )
        spec = self.spec(run_id="r-43", allow_network=True)
        with self.backend.open_session(spec) as session:
            result = session.execute(code)
        self.assertTrue(result.ok, result.stderr)

    def test_shared_project_data_cannot_be_mutated(self):
        original = (self.project_data / "prices.csv").read_text(encoding="utf-8")
        code = "open('data/prices.csv', 'w').write('tampered')"
        with self.backend.open_session(self.spec()) as session:
            result = session.execute(code)

        self.assertFalse(result.ok)
        self.assertEqual(
            (self.project_data / "prices.csv").read_text(encoding="utf-8"), original
        )

    def test_close_destroys_the_workspace(self):
        session = self.backend.open_session(self.spec())
        root = session.root
        self.assertTrue(root.exists())
        session.close()
        self.assertFalse(root.exists())
        session.close()  # idempotent

    def test_execute_after_close_is_rejected(self):
        session = self.backend.open_session(self.spec())
        session.close()
        with self.assertRaises(SandboxError):
            session.execute("print('nope')")

    def test_sessions_do_not_share_a_workspace(self):
        other = self.spec(user_id="u-bob", session_id="s-9", run_id="r-77")
        with self.backend.open_session(self.spec()) as alice:
            alice.execute("open('out/secret.txt', 'w').write('alice only')")
            with self.backend.open_session(other) as bob:
                result = bob.execute("import os; print(sorted(os.listdir('out')))")
        self.assertTrue(result.ok, result.stderr)
        self.assertNotIn("secret.txt", result.stdout)


class BackendSelectionTests(unittest.TestCase):
    def test_backend_is_selected_by_name(self):
        self.assertEqual(create_backend("local").name, "local")
        self.assertEqual(create_backend("agentcore").name, "agentcore")

    def test_unknown_backend_is_rejected(self):
        with self.assertRaisesRegex(SandboxError, "Unknown sandbox backend"):
            create_backend("does-not-exist")


class StubClient:
    network_mode = "VPC"

    def __init__(self, service, calls):
        self._service = service
        self._calls = calls

    def _record(self, operation, **kwargs):
        self._calls.append((self._service, operation, kwargs))

    def get_caller_identity(self, **kwargs):
        self._record("get_caller_identity", **kwargs)
        return {"Account": "111122223333"}

    def head_bucket(self, **kwargs):
        self._record("head_bucket", **kwargs)
        return {}

    def get_code_interpreter(self, **kwargs):
        self._record("get_code_interpreter", **kwargs)
        return {"networkConfiguration": {"networkMode": self.network_mode}}

    def assume_role(self, **kwargs):
        self._record("assume_role", **kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "AKIAEXAMPLE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
            }
        }

    def start_code_interpreter_session(self, **kwargs):
        self._record("start_code_interpreter_session", **kwargs)
        return {"sessionId": "ci-session-1"}

    def invoke_code_interpreter(self, **kwargs):
        self._record("invoke_code_interpreter", **kwargs)
        return {
            "stream": [
                {
                    "result": {
                        "content": [{"type": "text", "text": "ok"}],
                        "structuredContent": {
                            "stdout": "42\n",
                            "stderr": "",
                            "exitCode": 0,
                        },
                        "isError": False,
                    }
                }
            ]
        }

    def stop_code_interpreter_session(self, **kwargs):
        self._record("stop_code_interpreter_session", **kwargs)
        return {}


def _md5_of(path):
    import hashlib

    return hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324


class SyncingS3Client(StubClient):
    """Records what a project-data sync would upload and delete."""

    objects = {}
    uploaded = []
    deleted = []
    put_objects = []
    head_metadata = {}
    revision_body = b"0"

    def get_paginator(self, name):
        objects = type(self).objects

        class _Paginator:
            def paginate(self, Bucket, Prefix):
                yield {
                    "Contents": [
                        {"Key": key, "ETag": f'"{meta["etag"]}"', "Size": meta["size"]}
                        for key, meta in objects.items()
                        if key.startswith(Prefix)
                    ]
                }

        return _Paginator()

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        type(self).uploaded.append(key)

    def head_object(self, Bucket, Key):
        return {"Metadata": type(self).head_metadata.get(Key, {})}

    def delete_objects(self, Bucket, Delete):
        type(self).deleted.extend(entry["Key"] for entry in Delete["Objects"])

    def put_object(self, Bucket, Key, Body):
        type(self).put_objects.append((Key, Body))

    def get_object(self, Bucket, Key):
        class _Body:
            def read(self_inner):
                return type(self).revision_body

        return {"Body": _Body()}


class StubBotoSession:
    client_class = StubClient

    def __init__(self):
        self.calls = []

    def client(self, service, **_kwargs):
        return self.client_class(service, self.calls)

    def operations(self):
        return [operation for _service, operation, _kwargs in self.calls]

    def invocations(self):
        return [
            kwargs
            for _service, operation, kwargs in self.calls
            if operation == "invoke_code_interpreter"
        ]

    def commands(self):
        return [
            kwargs["arguments"]["command"]
            for kwargs in self.invocations()
            if kwargs["name"] == "executeCommand"
        ]


class AgentCoreSandboxTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.stub = StubBotoSession()
        self.backend = AgentCoreSandbox(
            region="us-east-1",
            bucket="deepagents-test",
            interpreter_id="deepagents-abc1234567",
            data_access_role_arn="arn:aws:iam::111122223333:role/DeepAgentsSandboxData",
            boto_session=self.stub,
        )

    def test_preflight_accepts_a_vpc_interpreter(self):
        self.backend.preflight()
        self.assertIn("get_code_interpreter", self.stub.operations())

    def test_preflight_rejects_non_vpc_interpreter(self):
        class SandboxModeClient(StubClient):
            network_mode = "SANDBOX"

        self.stub.client_class = SandboxModeClient
        with self.assertRaisesRegex(SandboxError, "networkMode"):
            self.backend.preflight()

    def test_preflight_requires_configuration(self):
        # Constructor arguments fall back to the environment, and another test
        # module loads .env into this process, so clear those keys explicitly.
        cleared = {
            key: ""
            for key in (
                "AGENTCORE_REGION",
                "AGENTCORE_BUCKET",
                "AGENTCORE_INTERPRETER_ID",
                "AGENTCORE_DATA_ACCESS_ROLE_ARN",
            )
        }
        with mock.patch.dict(os.environ, cleared):
            backend = AgentCoreSandbox(boto_session=StubBotoSession())
            with self.assertRaisesRegex(SandboxError, "AGENTCORE_REGION"):
                backend.preflight()

    def test_session_policy_is_scoped_to_one_project_s_data(self):
        """data/ only: the mirror also carries skills, which the sandbox never reads."""
        policy = self.backend.session_policy(self.spec())
        read, listing = policy["Statement"]

        self.assertEqual(
            read["Resource"],
            "arn:aws:s3:::deepagents-test/projects/hpi-analytics/data/*",
        )
        self.assertEqual(listing["Action"], "s3:ListBucket")

        serialized = str(policy)
        self.assertNotIn("u-bob", serialized)
        self.assertNotIn("/artifacts/", serialized)
        self.assertNotIn("/skills", serialized)

    def test_session_credentials_grant_no_write_at_all(self):
        """Output returns inline, so nothing in the sandbox needs to write to S3.

        These credentials sit in the microVM where generated code can read them,
        so every permission they carry is a permission that code has.
        """
        policy = self.backend.session_policy(self.spec())
        for statement in policy["Statement"]:
            actions = statement["Action"]
            actions = [actions] if isinstance(actions, str) else actions
            for action in actions:
                self.assertFalse(
                    action.startswith(("s3:Put", "s3:Delete", "s3:Abort", "s3:Create")),
                    f"write action leaked into the session policy: {action}",
                )

    def test_session_tags_come_from_the_spec(self):
        tags = {tag["Key"]: tag["Value"] for tag in self.backend.session_tags(self.spec())}
        self.assertEqual(tags, {"slug": "hpi-analytics"})

    def test_assume_role_stays_within_the_packed_size_budget(self):
        """AWS packs the session policy and tags into one small budget.

        Five separate id tags overflowed it and failed AssumeRole outright, which
        is why the run identifiers travel as a single path-shaped tag.
        """
        spec = self.spec(
            user_id=uuid.uuid4().hex,
            project_id=uuid.uuid4().hex,
            session_id=uuid.uuid4().hex,
            run_id=uuid.uuid4().hex,
            project_slug="nmdb-analytics",
        )
        policy = json.dumps(self.backend.session_policy(spec), separators=(",", ":"))
        tags = sum(
            len(tag["Key"]) + len(tag["Value"])
            for tag in self.backend.session_tags(spec)
        )
        # Measured empirically against STS: ~950 characters was rejected at 104%.
        self.assertLess(len(policy) + tags, 800)

    def test_open_session_mints_credentials_then_hydrates(self):
        session = self.backend.open_session(self.spec())
        self.addCleanup(session.close)

        operations = self.stub.operations()
        self.assertEqual(operations[0], "assume_role")
        self.assertEqual(operations[1], "start_code_interpreter_session")

        assume = next(
            kwargs for _s, op, kwargs in self.stub.calls if op == "assume_role"
        )
        self.assertEqual(assume["DurationSeconds"], 3600)
        self.assertIn("Policy", assume)
        self.assertIn("Tags", assume)

        invocations = self.stub.invocations()
        self.assertEqual(invocations[0]["name"], "writeFiles")
        written = {item["path"] for item in invocations[0]["arguments"]["content"]}
        self.assertEqual(written, {"sandbox.json", ".aws-credentials"})

        commands = self.stub.commands()
        self.assertTrue(
            any(
                "aws s3 cp --recursive "
                "s3://deepagents-test/projects/hpi-analytics/data work/data" in command
                for command in commands
            ),
            commands,
        )
        self.assertTrue(
            all("AWS_SHARED_CREDENTIALS_FILE" in command for command in commands)
        )

    def test_execute_writes_code_then_runs_the_shared_bootstrap(self):
        session = self.backend.open_session(self.spec())
        self.addCleanup(session.close)
        self.stub.calls.clear()

        result = session.execute("print(6 * 7)")

        invocations = self.stub.invocations()
        self.assertEqual(invocations[0]["name"], "writeFiles")
        self.assertEqual(
            invocations[0]["arguments"]["content"][0]["path"], "generated.py"
        )
        self.assertEqual(invocations[1]["name"], "executeCode")
        self.assertEqual(invocations[1]["arguments"]["language"], "python")
        self.assertIn("runpy.run_path", invocations[1]["arguments"]["code"])
        self.assertEqual(result.stdout, "42\n")
        self.assertTrue(result.ok)

    def test_collect_artifacts_packs_reads_and_clears(self):
        self.stub.client_class = ArchiveReturningClient
        ArchiveReturningClient.archive = _tar_gz({"out/summary.csv": b"a,b\n1,2\n"})
        session = self.backend.open_session(self.spec())
        self.addCleanup(session.close)
        self.stub.calls.clear()
        destination = self.tmp_dir / "artifacts"

        collected = session.collect_artifacts(destination)

        self.assertEqual([path.name for path in collected], ["summary.csv"])
        self.assertEqual((destination / "summary.csv").read_bytes(), b"a,b\n1,2\n")

        commands = self.stub.commands()
        self.assertTrue(
            any("tar -czf collect.tgz -C work --exclude=./data ." in c for c in commands),
            commands,
        )
        # The working directory is emptied so the next archive holds only what
        # the next execution produced.
        self.assertTrue(any("rm -rf" in c and "! -name data" in c for c in commands), commands)
        # Nothing is uploaded: that is the entire point of this transport.
        self.assertFalse(any("aws s3 cp" in c and "work" in c for c in commands), commands)

    def test_collect_artifacts_refuses_an_oversized_archive(self):
        """Raised before the workspace is cleared, so the model can shrink it."""
        self.stub.client_class = ArchiveReturningClient
        ArchiveReturningClient.archive = b""
        ArchiveReturningClient.reported_size = 64 * 1024 * 1024
        self.addCleanup(setattr, ArchiveReturningClient, "reported_size", None)
        session = self.backend.open_session(self.spec())
        self.addCleanup(session.close)

        with self.assertRaisesRegex(SandboxError, "over the 16 MB limit"):
            session.collect_artifacts(self.tmp_dir / "artifacts")

    def test_config_sent_to_agentcore_omits_the_cpu_limit(self):
        """RLIMIT_CPU is cumulative, and the remote kernel outlives one execution."""
        session = self.backend.open_session(self.spec())
        self.addCleanup(session.close)

        written = {
            item["path"]: item["text"]
            for item in self.stub.invocations()[0]["arguments"]["content"]
        }
        config = json.loads(written["sandbox.json"])
        self.assertNotIn("cpu_seconds", config)
        self.assertEqual(config["timeout_seconds"], 5)

    def test_failed_shell_command_raises(self):
        class FailingClient(StubClient):
            def invoke_code_interpreter(self, **kwargs):
                self._record("invoke_code_interpreter", **kwargs)
                if kwargs["name"] != "executeCommand":
                    return super().invoke_code_interpreter(**kwargs)
                return {
                    "stream": [
                        {
                            "result": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "fatal error: An error occurred "
                                        "(AccessDenied) when calling ListObjectsV2",
                                    }
                                ],
                                "structuredContent": {"exitCode": 1},
                                "isError": False,
                            }
                        }
                    ]
                }

        self.stub.client_class = FailingClient
        with self.assertRaisesRegex(SandboxError, "AccessDenied"):
            self.backend.open_session(self.spec())

    def test_timeout_is_detected_from_the_sentinel(self):
        """The remote kernel swallows SystemExit, so exit codes cannot be trusted."""

        class TimingOutClient(StubClient):
            def invoke_code_interpreter(self, **kwargs):
                self._record("invoke_code_interpreter", **kwargs)
                if kwargs["name"] != "executeCode":
                    return super().invoke_code_interpreter(**kwargs)
                return {
                    "stream": [
                        {
                            "result": {
                                "structuredContent": {
                                    "stdout": "",
                                    "stderr": "__SANDBOX_TIMEOUT__ Execution exceeded 5 seconds",
                                    "exitCode": 1,
                                },
                                "isError": False,
                            }
                        }
                    ]
                }

        self.stub.client_class = TimingOutClient
        session = self.backend.open_session(self.spec())
        self.addCleanup(session.close)

        result = session.execute("import time; time.sleep(99)")
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)
        self.assertFalse(result.ok)

    def test_local_backend_needs_no_project_replication(self):
        """It reads the project directory directly, so there is nothing to sync."""
        backend = create_backend("local")
        self.assertTrue(backend.project_data_current("hpi-analytics", 7))
        self.assertEqual(
            backend.sync_project_content("hpi-analytics", self.project_root, 7), 0
        )

    def _sync(self, revision=9, objects=None):
        self.stub.client_class = SyncingS3Client
        SyncingS3Client.objects = objects or {}
        SyncingS3Client.uploaded = []
        SyncingS3Client.deleted = []
        SyncingS3Client.put_objects = []
        return self.backend.sync_project_content(
            "hpi-analytics", self.project_root, revision
        )

    def test_sync_uploads_only_what_differs(self):
        (self.project_data / "extra.csv").write_text("x,y\n1,2\n", encoding="utf-8")
        prices = self.project_data / "prices.csv"

        changed = self._sync(objects={
            # Already current: same size and single-part ETag as the local file.
            "projects/hpi-analytics/data/prices.csv": {
                "etag": _md5_of(prices),
                "size": prices.stat().st_size,
            },
            # No longer present locally, so it must be removed.
            "projects/hpi-analytics/data/stale.csv": {"etag": "whatever", "size": 1},
        })

        self.assertNotIn("projects/hpi-analytics/data/prices.csv", SyncingS3Client.uploaded)
        self.assertIn("projects/hpi-analytics/data/extra.csv", SyncingS3Client.uploaded)
        self.assertEqual(SyncingS3Client.deleted, ["projects/hpi-analytics/data/stale.csv"])
        self.assertEqual(changed, len(SyncingS3Client.uploaded) + 1)
        # The revision marker is written last so a partial sync never looks current.
        self.assertEqual(SyncingS3Client.put_objects[-1][0], "projects/hpi-analytics/.revision")
        self.assertEqual(SyncingS3Client.put_objects[-1][1], b"9")

    def test_sync_carries_skills_not_only_data(self):
        """The agent builds its prompts from these, so they must travel with it."""
        self._sync()

        self.assertIn(
            "projects/hpi-analytics/skills/pricing/SKILL.md", SyncingS3Client.uploaded
        )

    def test_sync_never_deletes_the_revision_marker(self):
        """It lives inside the mirrored prefix and would otherwise look stale."""
        self._sync(objects={"projects/hpi-analytics/.revision": {"etag": "x", "size": 1}})

        self.assertEqual(SyncingS3Client.deleted, [])

    def test_sync_skips_hidden_files(self):
        """Deletes stage as '.<name>.deleted-<uuid>'; a tombstone is not content."""
        (self.project_data / ".prices.csv.deleted-abc").write_text("x", encoding="utf-8")

        self._sync()

        self.assertFalse(
            any(".deleted-" in key for key in SyncingS3Client.uploaded),
            SyncingS3Client.uploaded,
        )

    def test_project_data_current_compares_the_revision_marker(self):
        self.stub.client_class = SyncingS3Client
        SyncingS3Client.revision_body = b"9"
        self.assertTrue(self.backend.project_data_current("hpi-analytics", 9))
        self.assertFalse(self.backend.project_data_current("hpi-analytics", 10))

    def test_close_stops_the_remote_session(self):
        session = self.backend.open_session(self.spec())
        self.stub.calls.clear()
        session.close()
        session.close()  # idempotent

        self.assertEqual(
            [op for _s, op, _k in self.stub.calls],
            ["stop_code_interpreter_session"],
        )


class ArchiveReturningClient(StubClient):
    """Serves a real tar.gz through readFiles, as a live session would."""

    archive = b""
    reported_size = None  # overrides the size the tar command prints

    def invoke_code_interpreter(self, **kwargs):
        self._record("invoke_code_interpreter", **kwargs)
        if kwargs["name"] == "readFiles":
            return {
                "stream": [
                    {
                        "result": {
                            "content": [
                                {
                                    "type": "resource",
                                    "resource": {
                                        "blob": type(self).archive,
                                        "mimeType": "application/x-tar",
                                    },
                                }
                            ],
                            "isError": False,
                        }
                    }
                ]
            }
        stdout = ""
        command = kwargs["arguments"].get("command", "")
        if kwargs["name"] == "executeCommand" and "tar -czf" in command:
            declared = type(self).reported_size
            stdout = str(len(type(self).archive) if declared is None else declared)
        return {
            "stream": [
                {
                    "result": {
                        "content": [{"type": "text", "text": stdout or "ok"}],
                        "structuredContent": {
                            "stdout": stdout,
                            "stderr": "",
                            "exitCode": 0,
                        },
                        "isError": False,
                    }
                }
            ]
        }


def _tar_gz(files, symlinks=None):
    """Build an archive in memory, including members tar would never produce."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        for link, target in (symlinks or {}).items():
            info = tarfile.TarInfo(link)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return buffer.getvalue()


class ArchiveExtractionTests(unittest.TestCase):
    """Archive members are named by generated code and are not to be trusted.

    The sandbox builds this archive, so a member called '../../x' is as much a
    write primitive as a crafted S3 key was under the previous transport.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="deep-agents-archive-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.destination = self.tmp / "run"
        self.outside = self.tmp / "ESCAPED.txt"

    def test_ordinary_files_are_extracted(self):
        data = _tar_gz({"./out/summary.csv": b"a,b\n1,2\n", "./module.py": b"x = 1\n"})

        collected = extract_artifacts(data, self.destination)

        self.assertEqual(
            sorted(path.name for path in collected), ["module.py", "summary.csv"]
        )
        # out/ is a convention, not a namespace.
        self.assertTrue((self.destination / "summary.csv").is_file())
        self.assertFalse((self.destination / "out").exists())

    def test_relative_traversal_is_refused(self):
        data = _tar_gz({"../../ESCAPED.txt": b"planted"})

        self.assertEqual(extract_artifacts(data, self.destination), [])
        self.assertFalse(self.outside.exists())

    def test_absolute_member_is_refused(self):
        data = _tar_gz({"/tmp/deep-agents-escaped.txt": b"planted"})

        self.assertEqual(extract_artifacts(data, self.destination), [])
        self.assertFalse(Path("/tmp/deep-agents-escaped.txt").exists())

    def test_symlink_members_are_not_recreated(self):
        data = _tar_gz({}, symlinks={"./out/link.txt": "/etc/passwd"})

        self.assertEqual(extract_artifacts(data, self.destination), [])
        self.assertFalse((self.destination / "link.txt").exists())

    def test_a_refused_member_does_not_stop_the_rest(self):
        data = _tar_gz({"../ESCAPED.txt": b"planted", "./out/summary.csv": b"ok\n"})

        collected = extract_artifacts(data, self.destination)

        self.assertEqual([path.name for path in collected], ["summary.csv"])
        self.assertFalse(self.outside.exists())

    def test_by_products_are_filtered_like_the_local_backend(self):
        data = _tar_gz(
            {
                "./out/chart.png": b"\x89PNG",
                "./data/prices.csv": b"do not collect",
                "./pkg/__pycache__/mod.cpython-313.pyc": b"junk",
                "./mod.pyc": b"junk",
                "./.hidden": b"junk",
                "./out/.hidden": b"junk",
            }
        )

        collected = extract_artifacts(data, self.destination)

        self.assertEqual([path.name for path in collected], ["chart.png"])

    def test_identical_content_is_collected_once(self):
        """Models routinely save a deliverable loose and again under out/."""
        data = _tar_gz({"./chart.png": b"same-bytes", "./out/chart.png": b"same-bytes"})

        collected = extract_artifacts(data, self.destination)

        self.assertEqual([path.name for path in collected], ["chart.png"])

    def test_same_name_different_content_is_kept_separately(self):
        data = _tar_gz({"./chart.png": b"first", "./out/chart.png": b"second"})

        collected = extract_artifacts(data, self.destination)

        self.assertEqual(
            sorted(path.name for path in collected), ["chart-1.png", "chart.png"]
        )

    def test_too_many_members_is_refused(self):
        data = _tar_gz({f"./out/f{index}.txt": b"x" for index in range(6)})

        with mock.patch.object(archive_module, "MAX_MEMBERS", 3):
            with self.assertRaisesRegex(SandboxError, "more than 3 files"):
                extract_artifacts(data, self.destination)

    def test_decompression_bomb_is_refused(self):
        data = _tar_gz({"./out/big.bin": b"x" * 4096})

        with mock.patch.object(archive_module, "MAX_EXTRACTED_BYTES", 512):
            with self.assertRaisesRegex(SandboxError, "expands to more than"):
                extract_artifacts(data, self.destination)

    def test_an_unreadable_archive_raises(self):
        with self.assertRaisesRegex(SandboxError, "unreadable archive"):
            extract_artifacts(b"not an archive at all", self.destination)

    def test_no_staging_file_is_left_behind_on_failure(self):
        """Artifact registration walks this directory; a stray dotfile would ship."""
        data = _tar_gz({"./out/big.bin": b"x" * 4096})

        with mock.patch.object(archive_module, "MAX_EXTRACTED_BYTES", 512):
            with self.assertRaises(SandboxError):
                extract_artifacts(data, self.destination)

        self.assertEqual(list(self.destination.iterdir()), [])


class SafeRelativePathTests(unittest.TestCase):
    def test_plain_relative_paths_are_accepted(self):
        for raw in ("summary.csv", "out/summary.csv", "./out/chart.png", "a/b/c.txt"):
            with self.subTest(raw=raw):
                self.assertIsNotNone(safe_relative_path(raw))

    def test_escaping_paths_are_refused(self):
        for raw in ("../x", "a/../../x", "/etc/passwd", "", "/", "..", "a\\..\\b"):
            with self.subTest(raw=raw):
                self.assertIsNone(safe_relative_path(raw))

    def test_unique_destination_refuses_to_leave_the_directory(self):
        """The funnel both backends share holds even if a caller's check is wrong."""
        with self.assertRaisesRegex(SandboxError, "escapes the artifact directory"):
            unique_destination(Path("/srv/artifacts/run"), Path("../../escaped.txt"))


class StreamClippingTests(unittest.TestCase):
    def test_short_output_is_untouched(self):
        self.assertEqual(clip_text("hello"), "hello")

    def test_long_output_is_bounded_and_marked(self):
        text = "x" * (MAX_STREAM_CHARACTERS * 50)

        clipped = clip_text(text)

        self.assertLess(len(clipped), MAX_STREAM_CHARACTERS + 200)
        self.assertIn("characters omitted", clipped)

    def test_both_ends_survive(self):
        """A traceback lands at the end; what ran is at the start. Keep both."""
        text = "FIRST" + ("x" * MAX_STREAM_CHARACTERS * 10) + "LAST"

        clipped = clip_text(text)

        self.assertTrue(clipped.startswith("FIRST"))
        self.assertTrue(clipped.endswith("LAST"))


class LocalOutputBoundsTests(SandboxTestCase):
    """A runaway print must not be buffered whole in the server's memory."""

    def setUp(self):
        super().setUp()
        self.backend = LocalSandbox(base_dir=self.tmp_dir / "sandboxes")
        self.backend.preflight()

    def test_a_flood_of_output_is_bounded(self):
        session = self.backend.open_session(self.spec(timeout_seconds=30))
        self.addCleanup(session.close)

        result = session.execute(
            "for i in range(200000): print('flooding the pipe', i)"
        )

        self.assertTrue(result.ok)
        self.assertLess(len(result.stdout), MAX_STREAM_CHARACTERS + 200)
        self.assertIn("characters omitted", result.stdout)
        # The end of the stream is what proves the child ran to completion
        # rather than being cut off partway.
        self.assertIn("199999", result.stdout)

    def test_normal_output_is_returned_whole(self):
        session = self.backend.open_session(self.spec())
        self.addCleanup(session.close)

        result = session.execute("print('a modest amount of output')")

        self.assertEqual(result.stdout.strip(), "a modest amount of output")
        self.assertNotIn("omitted", result.stdout)


if __name__ == "__main__":
    unittest.main()
