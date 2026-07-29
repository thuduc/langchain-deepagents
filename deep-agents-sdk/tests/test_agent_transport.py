"""Tests for the agent tier: the wire format, the transports, and the server.

The contract these cover is unusual in that the in-process path never exercises
it. With DEEP_AGENTS_AGENT_TRANSPORT=local the events are handed from one
generator straight to another, so nothing here is checked -- serialisation,
parsing, the payload's shape, the streaming response -- and the first thing to
find a mistake would be a deploy onto AgentCore Runtime.

So the HTTP transport is driven end to end against the real agent server over an
ASGI transport, which is the same code path a container would take, with only the
graph and the S3 mirror stubbed out.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient


SDK_ROOT = Path(__file__).resolve().parents[1]
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

# Set before the application package is imported: it resolves its storage roots
# at import time, and this suite must not touch a development database.
_RUNTIME_ROOT = Path(tempfile.mkdtemp(prefix="agent-transport-test-"))
os.environ.setdefault("DEEP_AGENTS_DB_DIR", str(_RUNTIME_ROOT / "databases"))
os.environ.setdefault("DEEP_AGENTS_GENERATED_DIR", str(_RUNTIME_ROOT / "generated"))

from deep_agents_app.domain import RunContext  # noqa: E402
from deep_agents_app.runtime import (  # noqa: E402
    agent_client,
    agent_protocol,
    agent_server,
    artifact_transfer,
)
from deep_agents_app.runtime.agent_client import AgentTransportError  # noqa: E402
from deep_agents_app.runtime.artifact_transfer import ArtifactTransferError  # noqa: E402
from deep_agents_app.schemas import AgentInvocationRequest  # noqa: E402


def _context(run_id="run-1", user_id="user-1", session_id="session-1"):
    return RunContext(
        user_id=user_id,
        project_id="hpi-analytics",
        session_id=session_id,
        run_id=run_id,
        artifact_directory=Path(tempfile.gettempdir()) / "unused",
    )


def _payload(**overrides):
    fields = {
        "user_id": "user-1",
        "project_id": "hpi-analytics",
        "session_id": "session-1",
        "run_id": "run-1",
        "prompt": "How did prices move?",
        "project_name": "HPI Analytics",
        "project_slug": "hpi-analytics",
        "content_revision": 7,
        "model": "gpt-5.5",
    }
    fields.update(overrides)
    return agent_protocol.invocation_payload(**fields)


class ProtocolTests(unittest.TestCase):
    """The module's own claim: an event that cannot survive JSON is not allowed."""

    def test_every_event_survives_a_round_trip(self):
        events = [
            agent_protocol.status("Delegating to a project specialist…"),
            agent_protocol.answer("Prices rose 4.2% — see the ‘chart’ for détail."),
            agent_protocol.artifacts("staging/agent/run-1"),
            agent_protocol.artifacts(None),
            agent_protocol.cancelled(),
            agent_protocol.error("the sandbox was unavailable"),
        ]
        stream = "".join(agent_protocol.encode(event) for event in events)

        self.assertEqual(list(agent_protocol.decode(iter(stream.splitlines()))), events)

    def test_unreadable_lines_are_skipped_rather_than_ending_the_stream(self):
        lines = [
            "event: status",
            "data: {not json",
            "",
            ": a comment",
            "data: \"a bare string, not an event\"",
            "data: {\"no_type\": true}",
            "data: " + json.dumps(agent_protocol.status("still here")),
        ]

        self.assertEqual(
            list(agent_protocol.decode(iter(lines))),
            [agent_protocol.status("still here")],
        )

    def test_the_invocation_payload_satisfies_the_schema_that_receives_it(self):
        """Producer and validator have to agree; nothing else checks that they do."""
        request = AgentInvocationRequest.model_validate(_payload())

        self.assertEqual(request.project_slug, "hpi-analytics")
        self.assertEqual(request.content_revision, 7)
        # It also has to survive being sent, which is how it actually travels.
        self.assertEqual(json.loads(json.dumps(_payload())), _payload())


class TransportSelectionTests(unittest.TestCase):
    def test_defaults_to_local(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEP_AGENTS_AGENT_TRANSPORT", None)
            self.assertEqual(agent_client.configured_transport(), "local")

    def test_reads_and_normalises_the_environment(self):
        with mock.patch.dict(os.environ, {"DEEP_AGENTS_AGENT_TRANSPORT": " Runtime "}):
            self.assertEqual(agent_client.configured_transport(), "runtime")

    def test_an_unknown_transport_is_refused(self):
        with mock.patch.dict(os.environ, {"DEEP_AGENTS_AGENT_TRANSPORT": "smoke-signal"}):
            with self.assertRaises(AgentTransportError):
                list(agent_client.agent_events(_context(), "hello"))

    def test_http_without_a_url_is_refused_when_the_run_starts(self):
        with mock.patch.dict(
            os.environ, {"DEEP_AGENTS_AGENT_TRANSPORT": "http", "DEEP_AGENTS_AGENT_URL": ""}
        ):
            with self.assertRaises(AgentTransportError):
                list(agent_client.agent_events(_context(), "hello"))

    def test_runtime_without_an_arn_is_refused_when_the_run_starts(self):
        with mock.patch.dict(
            os.environ, {"DEEP_AGENTS_AGENT_TRANSPORT": "runtime", "AGENTCORE_RUNTIME_ARN": ""}
        ):
            with self.assertRaises(AgentTransportError):
                list(agent_client.agent_events(_context(), "hello"))


class AgentServerTestCase(unittest.TestCase):
    """Runs the real agent server with the graph and the S3 mirror stubbed."""

    def setUp(self):
        self.project_root = Path(tempfile.mkdtemp(prefix="agent-project-"))
        self.addCleanup(shutil.rmtree, self.project_root, ignore_errors=True)
        self.scratch_directories = []
        self.received = []
        self.events = [
            agent_protocol.status("Analyzing the request…"),
            agent_protocol.artifacts(None),
            agent_protocol.answer("Prices rose 4.2%."),
        ]

        def fake_run(context, prompt, project, ship_artifacts=False):
            self.received.append(
                {
                    "prompt": prompt,
                    "ship_artifacts": ship_artifacts,
                    "project_name": project.name,
                    "project_root": project.root,
                    "content_revision": project.content_revision,
                    "user_id": context.user_id,
                    "run_id": context.run_id,
                }
            )
            self.scratch_directories.append(context.artifact_directory)
            yield from self.events

        self._patches = [
            mock.patch.object(agent_server.engine, "run_agent_events", fake_run),
            mock.patch.object(
                agent_server.project_hydration, "hydrate",
                lambda slug, revision, destination: self.project_root,
            ),
            # The startup checks reach AWS; the suite must not. They have their
            # own tests below.
            mock.patch.object(agent_server.workspace, "preflight_model_gateway", lambda: None),
            mock.patch.object(agent_server.checkpointer, "preflight", lambda: None),
            mock.patch.object(agent_server, "get_backend", lambda: None),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

        self.app = agent_server.create_app()

    def client(self):
        """A synchronous client wired straight to the ASGI app, no socket involved.

        TestClient rather than httpx.ASGITransport: the transport the agent
        client uses is a synchronous httpx.Client, and ASGITransport only
        implements the async side.
        """
        return TestClient(self.app, base_url="http://agent")


class AgentServerTests(AgentServerTestCase):
    def test_the_health_check_reports_the_status_the_platform_defines(self):
        """'healthy' is not one of the values AgentCore recognises."""
        with self.client() as client:
            response = client.get("/ping")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "Healthy"})

    def test_the_health_check_reports_busy_while_a_run_is_in_flight(self):
        """AgentCore reclaims a session that has looked idle for fifteen
        minutes. Without this a run longer than that is terminated mid-flight,
        by the platform, with nothing in the application to explain it."""
        seen = []

        def observe_status(context, prompt, project, ship_artifacts=False):
            with self.client() as inner:
                seen.append(inner.get("/ping").json()["status"])
            yield agent_protocol.answer("done")

        with mock.patch.object(agent_server.engine, "run_agent_events", observe_status):
            with self.client() as client:
                client.post("/invocations", json=_payload())

        self.assertEqual(seen, ["HealthyBusy"])

    def test_the_container_reports_idle_again_once_the_run_ends(self):
        with self.client() as client:
            client.post("/invocations", json=_payload())
            self.assertEqual(client.get("/ping").json()["status"], "Healthy")

    def test_a_failed_run_does_not_leave_the_container_looking_busy(self):
        """A leaked busy count would keep the session alive forever, which the
        platform charges for and eventually refuses new sessions over."""
        def explode(context, prompt, project, ship_artifacts=False):
            yield agent_protocol.status("working")
            raise RuntimeError("the graph fell over")

        with mock.patch.object(agent_server.engine, "run_agent_events", explode):
            with self.client() as client:
                client.post("/invocations", json=_payload())
        with self.client() as client:
            self.assertEqual(client.get("/ping").json()["status"], "Healthy")

    def test_a_run_streams_the_agent_events_as_they_are_produced(self):
        with self.client() as client:
            response = client.post("/invocations", json=_payload())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(
            list(agent_protocol.decode(iter(response.text.splitlines()))), self.events
        )

    def test_the_run_is_told_to_hand_its_files_over(self):
        """It ran somewhere the caller cannot see, so the files have to travel."""
        with self.client() as client:
            client.post("/invocations", json=_payload())

        self.assertTrue(self.received[0]["ship_artifacts"])

    def test_project_facts_come_from_the_payload_rather_than_a_database(self):
        with self.client() as client:
            client.post("/invocations", json=_payload(project_name="Renamed", content_revision=9))

        self.assertEqual(self.received[0]["project_name"], "Renamed")
        self.assertEqual(self.received[0]["content_revision"], 9)
        self.assertEqual(self.received[0]["project_root"], self.project_root)

    def test_the_scratch_directory_does_not_outlive_the_run(self):
        with self.client() as client:
            client.post("/invocations", json=_payload())

        self.assertTrue(self.scratch_directories)
        self.assertFalse(self.scratch_directories[0].exists())

    def test_a_payload_sent_without_a_json_content_type_is_still_accepted(self):
        """AgentCore forwards the body without one; a 422 here would be visible
        only in the container's own logs."""
        with self.client() as client:
            response = client.post("/invocations", content=json.dumps(_payload()).encode())

        self.assertEqual(response.status_code, 200)

    def test_a_malformed_payload_is_refused_with_a_reason(self):
        with self.client() as client:
            self.assertEqual(client.post("/invocations", content=b"{{{").status_code, 422)
            self.assertEqual(
                client.post("/invocations", json={"user_id": "u"}).status_code, 422
            )

    def test_a_project_slug_that_is_not_a_slug_is_refused(self):
        """The slug picks the prefix hydration downloads from, so its shape is
        constrained rather than trusted."""
        with self.client() as client:
            response = client.post("/invocations", json=_payload(project_slug="../secrets"))

        self.assertEqual(response.status_code, 422)

    def test_a_run_failing_mid_stream_reports_an_error_event(self):
        """The status line has already been sent by then, so the response cannot
        become a 500. An error event is something the web tier can act on;
        a stream that simply stops leaves it guessing."""
        def explode(context, prompt, project, ship_artifacts=False):
            yield agent_protocol.status("Analyzing the request…")
            raise RuntimeError("the graph fell over")

        with mock.patch.object(agent_server.engine, "run_agent_events", explode):
            with self.client() as client:
                response = client.post("/invocations", json=_payload())

        self.assertEqual(response.status_code, 200)
        decoded = list(agent_protocol.decode(iter(response.text.splitlines())))
        self.assertEqual(
            [event["type"] for event in decoded],
            [agent_protocol.STATUS, agent_protocol.ERROR],
        )
        self.assertIn("the graph fell over", decoded[-1]["message"])

    def test_the_scratch_directory_is_removed_even_when_the_run_fails(self):
        def explode(context, prompt, project, ship_artifacts=False):
            self.scratch_directories.append(context.artifact_directory)
            yield agent_protocol.status("Analyzing the request…")
            raise RuntimeError("the graph fell over")

        with mock.patch.object(agent_server.engine, "run_agent_events", explode):
            with self.client() as client:
                client.post("/invocations", json=_payload())

        self.assertTrue(self.scratch_directories)
        self.assertFalse(self.scratch_directories[0].exists())


class AgentStartupTests(unittest.TestCase):
    """The container must not accept work it cannot do.

    Without these checks a misconfigured container started, answered /ping, was
    marked READY, and failed on every prompt with the reason visible only in its
    own logs -- while /ping kept new sessions arriving to fail the same way.
    """

    def _run_lifespan(self, **overrides):
        checks = {
            "model_gateway": mock.patch.object(
                agent_server.workspace, "preflight_model_gateway", overrides.get("gateway", lambda: None)
            ),
            "checkpointer": mock.patch.object(
                agent_server.checkpointer, "preflight", overrides.get("checkpointer", lambda: None)
            ),
            "sandbox": mock.patch.object(
                agent_server, "get_backend", overrides.get("sandbox", lambda: None)
            ),
        }
        with checks["model_gateway"], checks["checkpointer"], checks["sandbox"]:
            with TestClient(agent_server.create_app()) as client:
                return client.get("/ping").status_code

    def test_a_healthy_container_starts(self):
        self.assertEqual(self._run_lifespan(), 200)

    def test_every_dependency_is_checked_at_startup(self):
        called = []
        self._run_lifespan(
            gateway=lambda: called.append("gateway"),
            checkpointer=lambda: called.append("checkpointer"),
            sandbox=lambda: called.append("sandbox"),
        )
        self.assertEqual(sorted(called), ["checkpointer", "gateway", "sandbox"])

    def test_an_unreadable_gateway_key_stops_the_container_starting(self):
        def broken():
            raise RuntimeError("secret unreadable")

        with self.assertRaises(RuntimeError):
            self._run_lifespan(gateway=broken)

    def test_an_unreachable_checkpoint_store_stops_the_container_starting(self):
        def broken():
            raise RuntimeError("table missing")

        with self.assertRaises(RuntimeError):
            self._run_lifespan(checkpointer=broken)

    def test_an_unusable_sandbox_stops_the_container_starting(self):
        def broken():
            raise RuntimeError("interpreter is not in VPC mode")

        with self.assertRaises(RuntimeError):
            self._run_lifespan(sandbox=broken)

    def test_shutdown_releases_sandbox_sessions(self):
        """A session left open is a microVM still billing."""
        released = []
        with mock.patch.object(
            agent_server.sandbox_runs, "close_all_sessions", lambda: released.append(True)
        ):
            self._run_lifespan()
        self.assertEqual(released, [True])


class HttpTransportTests(AgentServerTestCase):
    """The client and the server, over the transport, with nothing in between."""

    def _agent_events(self, context=None, payload=None):
        def build(*_args, **_kwargs):
            """Stand in for the client the transport builds, bound to the app."""
            return TestClient(self.app, base_url="http://agent")

        environment = {
            "DEEP_AGENTS_AGENT_TRANSPORT": "http",
            "DEEP_AGENTS_AGENT_URL": "http://agent",
        }
        # Stubbed because the real one reads the projects table, which is the web
        # tier's business; what is under test is what happens to it in transit.
        def describe(ctx, prompt):
            if payload is not None:
                return payload
            return _payload(user_id=ctx.user_id, run_id=ctx.run_id, prompt=prompt)

        with mock.patch.dict(os.environ, environment), mock.patch("httpx.Client", build), \
                mock.patch.object(agent_client, "_payload", describe):
            return list(agent_client.agent_events(context or _context(), "How did prices move?"))

    def test_events_arrive_exactly_as_the_agent_produced_them(self):
        self.assertEqual(self._agent_events(), self.events)

    def test_the_prompt_and_identity_reach_the_agent(self):
        self._agent_events(_context(run_id="run-42", user_id="user-9"))

        self.assertEqual(self.received[0]["prompt"], "How did prices move?")
        self.assertEqual(self.received[0]["run_id"], "run-42")
        self.assertEqual(self.received[0]["user_id"], "user-9")

    def test_a_refusal_from_the_server_is_raised_rather_than_read_as_events(self):
        """A rejected payload must not look like a run that produced nothing."""
        with self.assertRaises(AgentTransportError) as caught:
            self._agent_events(payload={"bad": True})
        self.assertIn("422", str(caught.exception))


class RuntimeSessionTests(unittest.TestCase):
    def test_a_short_conversation_id_is_padded_to_what_the_service_accepts(self):
        session = agent_client._runtime_session_id(_context(user_id="u", session_id="s"))
        self.assertGreaterEqual(len(session), 33)

    def test_the_same_conversation_always_gets_the_same_id(self):
        first = agent_client._runtime_session_id(_context(run_id="run-1"))
        second = agent_client._runtime_session_id(_context(run_id="run-2"))
        self.assertEqual(first, second)

    def test_different_conversations_get_different_ids(self):
        self.assertNotEqual(
            agent_client._runtime_session_id(_context(session_id="a")),
            agent_client._runtime_session_id(_context(session_id="b")),
        )


class StubBody:
    """A streaming response body, chunked so line assembly is actually exercised."""

    def __init__(self, payload, chunk_size=7):
        self._chunks = [
            payload[index : index + chunk_size]
            for index in range(0, len(payload), chunk_size)
        ]

    def iter_chunks(self, chunk_size=1024):
        return iter(self._chunks)


class LiveBody:
    """A body with botocore's blocking read semantics, fed a piece at a time.

    The important part is `read(amt)`: it waits until it has exactly `amt` bytes
    or the stream ends, which is what botocore and urllib3 actually do and what
    made a live stream look buffered. `pending` reports how much of the stream
    has not been produced yet, so a test can assert that a line was delivered
    before the run was over rather than measuring the clock.
    """

    def __init__(self, pieces):
        self._pieces = list(pieces)
        self._buffer = b""

    @property
    def pending(self):
        return len(self._pieces)

    def _produce(self):
        if not self._pieces:
            return False
        self._buffer += self._pieces.pop(0)
        return True

    def read(self, amt=None):
        if amt is None:
            while self._produce():
                pass
            out, self._buffer = self._buffer, b""
            return out
        while len(self._buffer) < amt and self._produce():
            pass
        out, self._buffer = self._buffer[:amt], self._buffer[amt:]
        return out

    def iter_chunks(self, chunk_size=1024):
        while True:
            chunk = self.read(chunk_size)
            if not chunk:
                return
            yield chunk


class ReadableLiveBody(LiveBody):
    """The same, but exposing the raw stream's read1() as botocore's does."""

    def __init__(self, pieces):
        super().__init__(pieces)
        outer = self

        class _Raw:
            def read1(self, amt=None):
                if not outer._buffer:
                    outer._produce()
                out, outer._buffer = outer._buffer[:amt], outer._buffer[amt:]
                return out

        self._raw_stream = _Raw()


class StreamingArrivalTests(unittest.TestCase):
    """The defect: the whole response was delivered at once, at the end.

    A response of, say, a kilobyte is smaller than the 1024-byte read the old
    code asked for, so the first read could not return until the stream closed.
    Every event then arrived together and the progress the agent reports as it
    works was useless. These assert on how much of the stream is still
    unproduced when a line comes out, which is deterministic where timing is not.
    """

    PIECES = [
        b"event: status\ndata: {\"type\": \"status\", \"message\": \"first\"}\n\n",
        b"event: status\ndata: {\"type\": \"status\", \"message\": \"second\"}\n\n",
        b"event: answer\ndata: {\"type\": \"answer\", \"text\": \"done\"}\n\n",
    ]

    def test_a_line_is_delivered_before_the_stream_ends(self):
        body = ReadableLiveBody(self.PIECES)
        lines = agent_client._iter_text_lines(body)

        self.assertEqual(next(lines), "event: status")
        self.assertGreater(body.pending, 0, "the whole stream was drained first")

    def test_events_decode_as_they_arrive_rather_than_together(self):
        body = ReadableLiveBody(self.PIECES)
        events = agent_protocol.decode(agent_client._iter_text_lines(body))

        first = next(events)
        self.assertEqual(first["message"], "first")
        self.assertGreater(body.pending, 0, "later events were consumed too early")

    def test_a_body_without_read1_still_does_not_over_wait(self):
        """The fallback path: one byte at a time cannot block for a full buffer."""
        body = LiveBody(self.PIECES)
        lines = agent_client._iter_text_lines(body)

        self.assertEqual(next(lines), "event: status")
        self.assertGreater(body.pending, 0)

    def test_the_whole_stream_still_decodes_completely(self):
        body = ReadableLiveBody(self.PIECES)
        events = list(agent_protocol.decode(agent_client._iter_text_lines(body)))

        self.assertEqual(
            [event["type"] for event in events],
            [agent_protocol.STATUS, agent_protocol.STATUS, agent_protocol.ANSWER],
        )

    def test_a_trailing_line_without_a_newline_is_not_dropped(self):
        body = ReadableLiveBody([b"event: answer\n", b'data: {"type": "answer", "text": "x"}'])
        events = list(agent_protocol.decode(agent_client._iter_text_lines(body)))
        self.assertEqual([event["type"] for event in events], [agent_protocol.ANSWER])


class RuntimeTransportTests(unittest.TestCase):
    def setUp(self):
        self.events = [
            agent_protocol.status("Working…"),
            agent_protocol.answer("Done."),
        ]
        stream = "".join(agent_protocol.encode(event) for event in self.events)
        self.calls = []

        stub = mock.Mock()

        def invoke(**kwargs):
            self.calls.append(kwargs)
            return {"response": StubBody(stream.encode("utf-8"))}

        stub.invoke_agent_runtime.side_effect = invoke
        self.client = stub

    def _run(self):
        environment = {
            "DEEP_AGENTS_AGENT_TRANSPORT": "runtime",
            "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/agent",
            "AGENTCORE_REGION": "us-east-1",
        }
        with mock.patch.dict(os.environ, environment), \
                mock.patch("boto3.client", return_value=self.client), \
                mock.patch.object(agent_client, "_payload", lambda ctx, prompt: _payload()):
            return list(agent_client.agent_events(_context(), "hello"))

    def test_a_chunked_response_is_reassembled_into_events(self):
        self.assertEqual(self._run(), self.events)

    def test_the_content_type_is_stated_so_the_container_parses_the_body(self):
        self._run()
        self.assertEqual(self.calls[0]["contentType"], "application/json")
        self.assertEqual(self.calls[0]["accept"], "text/event-stream")

    def test_the_payload_travels_as_encoded_json(self):
        self._run()
        self.assertEqual(json.loads(self.calls[0]["payload"].decode("utf-8")), _payload())


class StubS3:
    """Enough of the S3 client for the hand-off, recording what it was asked to do."""

    def __init__(self):
        self.objects = {}
        self.uploaded = []
        self.deleted = []

    def upload_file(self, source, bucket, key):
        self.uploaded.append(key)
        self.objects[key] = Path(source).read_bytes()

    def download_file(self, bucket, key, destination):
        Path(destination).write_bytes(self.objects[key])

    def delete_objects(self, Bucket, Delete):  # noqa: N803 - boto3's parameter names
        self.deleted.extend(item["Key"] for item in Delete["Objects"])
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):  # noqa: N803 - boto3's parameter names
        return [
            {"Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)]}
        ]


class ArtifactTransferTests(unittest.TestCase):
    def setUp(self):
        self.source = Path(tempfile.mkdtemp(prefix="ship-"))
        self.destination = Path(tempfile.mkdtemp(prefix="collect-"))
        for directory in (self.source, self.destination):
            self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.s3 = StubS3()
        self.environment = mock.patch.dict(
            os.environ, {"AGENTCORE_BUCKET": "test-bucket", "AGENT_TRANSFER_BUCKET": ""}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = mock.patch("boto3.client", return_value=self.s3)
        self.client.start()
        self.addCleanup(self.client.stop)

    def _write(self, relative, content=b"data"):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_a_run_that_generated_nothing_hands_over_nothing(self):
        """Most runs. Returning a prefix would cost the caller a pointless listing."""
        self.assertIsNone(artifact_transfer.ship(self.source, "run-1"))
        self.assertEqual(self.s3.uploaded, [])

    def test_generated_files_keep_their_layout_under_the_run_prefix(self):
        self._write("chart.png")
        self._write("tables/summary.csv")

        prefix = artifact_transfer.ship(self.source, "run-1")

        self.assertEqual(prefix, "staging/agent/run-1")
        self.assertEqual(
            sorted(self.s3.uploaded),
            ["staging/agent/run-1/chart.png", "staging/agent/run-1/tables/summary.csv"],
        )

    def test_handing_over_without_a_bucket_is_an_error_rather_than_a_silent_loss(self):
        self._write("chart.png")
        with mock.patch.dict(os.environ, {"AGENTCORE_BUCKET": "", "AGENT_TRANSFER_BUCKET": ""}):
            with self.assertRaises(ArtifactTransferError):
                artifact_transfer.ship(self.source, "run-1")

    def test_a_round_trip_reproduces_the_files(self):
        self._write("chart.png", b"\x89PNG")
        self._write("tables/summary.csv", b"a,b\n1,2\n")

        prefix = artifact_transfer.ship(self.source, "run-1")
        collected = artifact_transfer.fetch(prefix, self.destination)

        self.assertEqual(len(collected), 2)
        self.assertEqual((self.destination / "chart.png").read_bytes(), b"\x89PNG")
        self.assertEqual(
            (self.destination / "tables/summary.csv").read_bytes(), b"a,b\n1,2\n"
        )

    def test_a_key_that_would_escape_the_run_directory_is_refused(self):
        self.s3.objects["staging/agent/run-1/../../escaped.txt"] = b"nope"
        self.s3.objects["staging/agent/run-1/fine.txt"] = b"ok"

        collected = artifact_transfer.fetch("staging/agent/run-1", self.destination)

        self.assertEqual([path.name for path in collected], ["fine.txt"])
        self.assertFalse((self.destination.parent / "escaped.txt").exists())

    def test_collected_files_are_discarded_afterwards(self):
        self._write("chart.png")
        prefix = artifact_transfer.ship(self.source, "run-1")

        artifact_transfer.discard(prefix)

        self.assertEqual(self.s3.deleted, ["staging/agent/run-1/chart.png"])

    def test_a_failure_to_discard_is_survivable(self):
        """The bucket's lifecycle rule expires the prefix anyway, so this costs
        storage rather than correctness."""
        self._write("chart.png")
        prefix = artifact_transfer.ship(self.source, "run-1")

        with mock.patch("boto3.client", side_effect=RuntimeError("no credentials")):
            artifact_transfer.discard(prefix)  # must not raise


if __name__ == "__main__":
    unittest.main()
