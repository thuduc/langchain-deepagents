"""Tests for the boundary between the web tier and the agent tier.

Two things are covered here, and they share a theme: both are failures that
produce no error anywhere, which is why they are worth asserting rather than
leaving to be noticed in production.

The transport preflight refuses a configuration whose pieces cannot reach each
other. Hydration is checked for what it *removes*, since a copy that only ever
grows serves content the project no longer has.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SDK_ROOT = Path(__file__).resolve().parents[1]
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

# The workspace service and the engine import each other, and only the workspace
# side can be imported first: it re-exports the engine's names once the engine
# has loaded. The application gets that ordering for free by importing services
# first; a test module reaching straight for the engine has to ask for it.
from deep_agents_app.services import workspace  # noqa: E402, F401
from deep_agents_app.runtime import agent_client, engine, project_hydration  # noqa: E402
from deep_agents_app.runtime.agent_client import AgentTransportError  # noqa: E402
from deep_agents_app.runtime.project_hydration import ProjectHydrationError  # noqa: E402


REMOTE_STORES = {
    "DEEP_AGENTS_CHECKPOINTER": "dynamodb",
    "DEEP_AGENTS_CANCELLATION": "dynamodb",
}


def _environment(**overrides):
    """A clean transport configuration with `overrides` applied."""
    base = {
        "DEEP_AGENTS_AGENT_TRANSPORT": "local",
        "DEEP_AGENTS_AGENT_URL": "",
        "AGENTCORE_RUNTIME_ARN": "",
        "DEEP_AGENTS_CHECKPOINTER": "sqlite",
        "DEEP_AGENTS_CANCELLATION": "local",
    }
    base.update(overrides)
    return mock.patch.dict(os.environ, base)


class TransportPreflightTests(unittest.TestCase):
    def test_local_transport_needs_nothing_else(self):
        with _environment():
            agent_client.preflight()

    def test_unknown_transport_is_rejected(self):
        with _environment(DEEP_AGENTS_AGENT_TRANSPORT="carrier-pigeon"):
            with self.assertRaises(AgentTransportError) as caught:
                agent_client.preflight()
        self.assertIn("carrier-pigeon", str(caught.exception))

    def test_http_transport_requires_its_url(self):
        with _environment(DEEP_AGENTS_AGENT_TRANSPORT="http", **REMOTE_STORES):
            with self.assertRaises(AgentTransportError) as caught:
                agent_client.preflight()
        self.assertIn("DEEP_AGENTS_AGENT_URL", str(caught.exception))

    def test_runtime_transport_requires_its_arn(self):
        with _environment(DEEP_AGENTS_AGENT_TRANSPORT="runtime", **REMOTE_STORES):
            with self.assertRaises(AgentTransportError) as caught:
                agent_client.preflight()
        self.assertIn("AGENTCORE_RUNTIME_ARN", str(caught.exception))

    def test_remote_transport_refuses_a_checkpoint_file_on_this_host(self):
        """The failure this prevents is an agent with amnesia and no error."""
        with _environment(
            DEEP_AGENTS_AGENT_TRANSPORT="http",
            DEEP_AGENTS_AGENT_URL="http://127.0.0.1:8080",
            DEEP_AGENTS_CHECKPOINTER="sqlite",
            DEEP_AGENTS_CANCELLATION="dynamodb",
        ):
            with self.assertRaises(AgentTransportError) as caught:
                agent_client.preflight()
        self.assertIn("DEEP_AGENTS_CHECKPOINTER", str(caught.exception))

    def test_remote_transport_refuses_an_in_process_cancellation_registry(self):
        """The failure this prevents is a stop button that does nothing."""
        with _environment(
            DEEP_AGENTS_AGENT_TRANSPORT="runtime",
            AGENTCORE_RUNTIME_ARN="arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
            DEEP_AGENTS_CHECKPOINTER="dynamodb",
            DEEP_AGENTS_CANCELLATION="local",
        ):
            with self.assertRaises(AgentTransportError) as caught:
                agent_client.preflight()
        self.assertIn("DEEP_AGENTS_CANCELLATION", str(caught.exception))

    def test_a_fully_remote_configuration_is_accepted(self):
        with _environment(
            DEEP_AGENTS_AGENT_TRANSPORT="runtime",
            AGENTCORE_RUNTIME_ARN="arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
            **REMOTE_STORES,
        ):
            agent_client.preflight()


def _gateway(**overrides):
    """A clean model-gateway configuration with `overrides` applied."""
    base = {
        "PORTKEY_API_KEY": "",
        "PORTKEY_API_KEY_SECRET_ARN": "",
        "PORTKEY_PROVIDER_SLUG": "openai-prod",
        "AGENTCORE_REGION": "us-east-1",
    }
    base.update(overrides)
    return mock.patch.dict(os.environ, base)


class StubSecretsManager:
    """Returns one secret, counting how often it was actually asked for."""

    def __init__(self, value, error=None):
        self.value = value
        self.error = error
        self.calls = 0

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3's parameter names
        assert SecretId.startswith("arn:aws:secretsmanager:"), (
            f"asked for something that is not a secret ARN: {SecretId!r}"
        )
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"SecretString": self.value}


class ModelGatewayTests(unittest.TestCase):
    """The provider slug has no default, and the key is never required to sit in
    the environment -- both for the same reason as the settings above: the
    failures are only visible once a prompt reaches the gateway."""

    def setUp(self):
        engine.reset_gateway_key_cache()
        self.addCleanup(engine.reset_gateway_key_cache)

    def test_a_configured_gateway_needs_a_provider(self):
        with _gateway(PORTKEY_API_KEY="pk-test", PORTKEY_PROVIDER_SLUG=""):
            with self.assertRaises(RuntimeError) as caught:
                engine.preflight_model_gateway()
        self.assertIn("PORTKEY_PROVIDER_SLUG", str(caught.exception))

    def test_a_key_in_the_environment_is_used_directly(self):
        with _gateway(PORTKEY_API_KEY="pk-test"):
            engine.preflight_model_gateway()
            self.assertEqual(engine.portkey_api_key(), "pk-test")

    def test_no_gateway_at_all_stays_usable(self):
        """Running without a gateway is a supported development mode: it needs
        neither a provider nor a key."""
        with _gateway(PORTKEY_PROVIDER_SLUG=""):
            self.assertFalse(engine.model_gateway_configured())
            engine.preflight_model_gateway()

    def test_an_arn_alone_counts_as_a_configured_gateway(self):
        with _gateway(PORTKEY_API_KEY_SECRET_ARN="arn:aws:secretsmanager:::secret:k"):
            self.assertTrue(engine.model_gateway_configured())

    def test_the_key_is_read_from_the_secret_when_only_an_arn_is_given(self):
        secrets = StubSecretsManager("pk-from-secret")
        with _gateway(PORTKEY_API_KEY_SECRET_ARN="arn:aws:secretsmanager:::secret:k"), \
                mock.patch("boto3.client", return_value=secrets):
            self.assertEqual(engine.portkey_api_key(), "pk-from-secret")

    def test_a_json_secret_is_accepted_as_well_as_a_bare_string(self):
        """Secrets Manager's own default shape is JSON, so an operator should not
        have to know which one this expects."""
        secrets = StubSecretsManager('{"PORTKEY_API_KEY": "pk-from-json"}')
        with _gateway(PORTKEY_API_KEY_SECRET_ARN="arn:aws:secretsmanager:::secret:k"), \
                mock.patch("boto3.client", return_value=secrets):
            self.assertEqual(engine.portkey_api_key(), "pk-from-json")

    def test_the_environment_wins_so_development_never_reaches_aws(self):
        secrets = StubSecretsManager("pk-from-secret")
        with _gateway(
            PORTKEY_API_KEY="pk-local",
            PORTKEY_API_KEY_SECRET_ARN="arn:aws:secretsmanager:::secret:k",
        ), mock.patch("boto3.client", return_value=secrets):
            self.assertEqual(engine.portkey_api_key(), "pk-local")
        self.assertEqual(secrets.calls, 0)

    def test_the_key_is_not_fetched_again_for_every_run(self):
        secrets = StubSecretsManager("pk-from-secret")
        with _gateway(PORTKEY_API_KEY_SECRET_ARN="arn:aws:secretsmanager:::secret:k"), \
                mock.patch("boto3.client", return_value=secrets):
            for _ in range(5):
                engine.portkey_api_key()
        self.assertEqual(secrets.calls, 1)

    def test_a_rotated_key_is_picked_up_without_restarting(self):
        """The reason for holding a reference rather than a value: if a running
        container had to be restarted to notice, rotation would still be a deploy."""
        secrets = StubSecretsManager("pk-old")
        with _gateway(PORTKEY_API_KEY_SECRET_ARN="arn:aws:secretsmanager:::secret:k"), \
                mock.patch("boto3.client", return_value=secrets):
            self.assertEqual(engine.portkey_api_key(), "pk-old")
            secrets.value = "pk-new"
            with mock.patch.object(engine, "_SECRET_CACHE_SECONDS", -1):
                self.assertEqual(engine.portkey_api_key(), "pk-new")
        self.assertEqual(secrets.calls, 2)

    def test_an_unreadable_secret_fails_the_deploy_rather_than_a_prompt(self):
        secrets = StubSecretsManager("", error=RuntimeError("AccessDeniedException"))
        with _gateway(PORTKEY_API_KEY_SECRET_ARN="arn:aws:secretsmanager:::secret:k"), \
                mock.patch("boto3.client", return_value=secrets):
            with self.assertRaises(RuntimeError) as caught:
                engine.preflight_model_gateway()
        self.assertIn("secretsmanager:GetSecretValue", str(caught.exception))

    def test_a_configured_gateway_with_no_key_anywhere_is_refused(self):
        with _gateway(PORTKEY_API_KEY_SECRET_ARN=""):
            with self.assertRaises(RuntimeError):
                engine.portkey_api_key()


class StubS3Client:
    """Serves a project mirror out of a dict of key -> bytes."""

    def __init__(self, objects):
        self.objects = dict(objects)
        self.downloaded = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):  # noqa: N803 - boto3's parameter names
        assert Bucket == "test-bucket", f"listed the wrong bucket: {Bucket!r}"
        contents = [
            {"Key": key, "Size": len(body)}
            for key, body in sorted(self.objects.items())
            if key.startswith(Prefix)
        ]
        return [{"Contents": contents}]

    def download_file(self, bucket, key, destination):
        self.downloaded.append(key)
        Path(destination).write_bytes(self.objects[key])


class HydrationTests(unittest.TestCase):
    def setUp(self):
        self.destination = Path(tempfile.mkdtemp(prefix="hydration-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.destination, ignore_errors=True))
        project_hydration.forget("demo")
        self.addCleanup(project_hydration.forget, "demo")
        self.environment = mock.patch.dict(
            os.environ, {"AGENTCORE_BUCKET": "test-bucket", "AGENTCORE_REGION": "us-east-1"}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def _hydrate(self, objects, revision):
        """Run one hydration against a stubbed mirror, returning the client."""
        client = StubS3Client(objects)
        with mock.patch("boto3.client", return_value=client):
            root = project_hydration.hydrate("demo", revision, self.destination)
        self.assertEqual(root, self.destination / "demo")
        return client

    def test_content_is_written_under_the_project_root(self):
        self._hydrate(
            {
                "projects/demo/data/prices.csv": b"a,b\n",
                "projects/demo/skills/analysis/SKILL.md": b"---\nname: analysis\n---\n",
            },
            revision=1,
        )
        root = self.destination / "demo"
        self.assertEqual((root / "data/prices.csv").read_bytes(), b"a,b\n")
        self.assertTrue((root / "skills/analysis/SKILL.md").is_file())

    def test_the_revision_marker_is_not_treated_as_project_content(self):
        """It is the upload side's bookkeeping, and skipping it is what keeps
        both sides agreeing on what the mirror carries."""
        self._hydrate(
            {
                "projects/demo/.revision": b"1",
                "projects/demo/data/prices.csv": b"a,b\n",
            },
            revision=1,
        )
        self.assertFalse((self.destination / "demo/.revision").exists())

    def test_a_second_turn_at_the_same_revision_does_not_transfer_again(self):
        objects = {"projects/demo/data/prices.csv": b"a,b\n"}
        self._hydrate(objects, revision=1)
        client = self._hydrate(objects, revision=1)
        self.assertEqual(client.downloaded, [])

    def test_a_file_dropped_from_the_mirror_is_deleted_locally(self):
        """The bug this covers: a warm container went on reading a deleted
        skill, because hydration only ever added files."""
        self._hydrate(
            {
                "projects/demo/data/prices.csv": b"a,b\n",
                "projects/demo/skills/old/SKILL.md": b"---\nname: old\n---\n",
            },
            revision=1,
        )
        root = self.destination / "demo"
        self.assertTrue((root / "skills/old/SKILL.md").is_file())

        self._hydrate({"projects/demo/data/prices.csv": b"a,b\n"}, revision=2)

        self.assertFalse((root / "skills/old/SKILL.md").exists())
        self.assertFalse((root / "skills/old").exists())
        self.assertTrue((root / "data/prices.csv").is_file())

    def test_an_empty_mirror_is_refused_rather_than_emptying_the_copy(self):
        self._hydrate({"projects/demo/data/prices.csv": b"a,b\n"}, revision=1)
        with self.assertRaises(ProjectHydrationError):
            self._hydrate({}, revision=2)
        self.assertTrue((self.destination / "demo/data/prices.csv").is_file())

    def test_a_key_escaping_the_project_is_refused(self):
        client = self._hydrate(
            {
                "projects/demo/data/prices.csv": b"a,b\n",
                "projects/demo/../../escaped.txt": b"nope",
            },
            revision=1,
        )
        self.assertNotIn("projects/demo/../../escaped.txt", client.downloaded)
        self.assertFalse((self.destination.parent / "escaped.txt").exists())


if __name__ == "__main__":
    unittest.main()
