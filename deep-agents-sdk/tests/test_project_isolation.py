import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import jwt


REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = REPO_ROOT / "deep-agents-sdk"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from deep_agents_app import application  # noqa: E402
from deep_agents_app.services import workspace as server  # noqa: E402


class MultiUserIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="deep-agents-test-"))
        self.originals = {
            "DB_PATH": server.DB_PATH,
            "AGENT_CHECKPOINT_DB_PATH": server.AGENT_CHECKPOINT_DB_PATH,
            "WORK_DIR": server.WORK_DIR,
            "ARTIFACTS_DIR": server.ARTIFACTS_DIR,
            "TMP_UPLOADS_DIR": server.TMP_UPLOADS_DIR,
            "PROJECTS_ROOT": server.PROJECTS_ROOT,
            "DEVELOPMENT_LOGIN_ENABLED": server.DEVELOPMENT_LOGIN_ENABLED,
            "DEVELOPMENT_SIGNING_SECRET": server.DEVELOPMENT_SIGNING_SECRET,
        }
        server.DB_PATH = self.tmp_dir / "databases" / "app.db"
        server.AGENT_CHECKPOINT_DB_PATH = self.tmp_dir / "databases" / "agent.db"
        server.WORK_DIR = self.tmp_dir / "generated" / "work"
        server.ARTIFACTS_DIR = self.tmp_dir / "generated" / "artifacts"
        server.TMP_UPLOADS_DIR = self.tmp_dir / "generated" / "uploads"
        server.PROJECTS_ROOT = self.tmp_dir / "projects"
        for path in (
            server.WORK_DIR,
            server.ARTIFACTS_DIR,
            server.TMP_UPLOADS_DIR,
            server.PROJECTS_ROOT,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self._create_project("hpi-analytics", "hpi-analysis")
        self._create_project("nmdb-analytics", "nmdb-analytics")

        server.invalidate_all_agents()
        server._SKILLS_VALIDATED = False
        server.init_db()
        from fastapi.testclient import TestClient

        self.client = TestClient(application.app)
        self.client.__enter__()
        self.user1_headers = self._headers("user-1")
        self.user2_headers = self._headers("user-2")
        self.admin_headers = self._headers("admin-1", ["PROJECT_ADMIN"])

    def tearDown(self):
        self.client.__exit__(None, None, None)
        server.invalidate_all_agents()
        for name, value in self.originals.items():
            setattr(server, name, value)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_project(self, slug, skill_name):
        skill_dir = self.tmp_dir / "projects" / slug / "skills" / skill_name
        skill_dir.mkdir(parents=True)
        (self.tmp_dir / "projects" / slug / "data").mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill_name}\ndescription: Test skill for {slug}.\n---\n",
            encoding="utf-8",
        )

    def _headers(self, subject, roles=None):
        token = jwt.encode(
            {
                "iss": "https://issuer.test",
                "aud": "deep-agents-test",
                "sub": subject,
                "roles": roles or [],
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            "test-secret-with-sufficient-length",
            algorithm="HS256",
        )
        return {"x-fnma-jws-token": token}

    def _user_id(self, headers):
        response = self.client.get("/api/auth/me", headers=headers)
        self.assertEqual(response.status_code, 200)
        return response.json()["user"]["id"]

    def test_api_requires_well_formed_cdx_identity(self):
        self.assertEqual(self.client.get("/api/projects").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/api/projects", headers={"x-fnma-jws-token": "not-a-jwt"}
            ).status_code,
            401,
        )
        missing_subject = jwt.encode(
            {"roles": ["PROJECT_ADMIN"]},
            "untrusted-client-key-that-is-long-enough",
            algorithm="HS256",
        )
        self.assertEqual(
            self.client.get(
                "/api/projects", headers={"x-fnma-jws-token": missing_subject}
            ).status_code,
            401,
        )
        expired = jwt.encode(
            {
                "iss": "ignored-by-the-app",
                "aud": "also-ignored-by-the-app",
                "sub": "expired-user",
                "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
            },
            "untrusted-client-key-that-is-long-enough",
            algorithm="HS256",
        )
        self.assertEqual(
            self.client.get(
                "/api/projects", headers={"x-fnma-jws-token": expired}
            ).status_code,
            200,
        )
        with server.get_db_connection() as conn:
            identity = conn.execute(
                "SELECT issuer, subject FROM users WHERE subject = ?",
                ("expired-user",),
            ).fetchone()
        self.assertEqual(dict(identity), {"issuer": "cdx", "subject": "expired-user"})

    def test_development_login_is_opt_in_and_uses_a_signed_cookie(self):
        server.DEVELOPMENT_LOGIN_ENABLED = False
        self.assertFalse(
            self.client.get("/api/auth/config").json()["development_login_enabled"]
        )
        disabled = self.client.post(
            "/api/auth/development-login",
            json={"subject": "developer", "project_admin": True},
        )
        self.assertEqual(disabled.status_code, 404)

        server.DEVELOPMENT_LOGIN_ENABLED = True
        signing_dir = self.tmp_dir / "development-signing"
        signing_dir.mkdir()
        server.DEVELOPMENT_SIGNING_SECRET = (
            server.load_or_create_development_signing_secret(signing_dir)
        )
        self.assertEqual(
            server.DEVELOPMENT_SIGNING_SECRET,
            server.load_or_create_development_signing_secret(signing_dir),
        )
        self.assertTrue(
            self.client.get("/api/auth/config").json()["development_login_enabled"]
        )
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

        login = self.client.post(
            "/api/auth/development-login",
            json={"subject": "developer", "project_admin": True},
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["user"]["subject"], "developer")
        self.assertTrue(login.json()["user"]["is_project_admin"])
        cookie_header = login.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie_header)
        self.assertIn("samesite=strict", cookie_header)

        server.DEVELOPMENT_SIGNING_SECRET = (
            server.load_or_create_development_signing_secret(signing_dir)
        )
        current = self.client.get("/api/auth/me")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json()["user"]["subject"], "developer")

        development_config = self.client.get("/api/auth/config").json()
        self.assertFalse(development_config["cdx_header_present"])
        self.assertEqual(
            development_config["development_identity"],
            {"subject": "developer", "project_admin": True},
        )

        cdx_identity = self.client.get("/api/auth/me", headers=self.user1_headers)
        self.assertEqual(cdx_identity.status_code, 200)
        self.assertEqual(cdx_identity.json()["user"]["subject"], "user-1")
        self.assertTrue(
            self.client.get("/api/auth/config", headers=self.user1_headers).json()[
                "cdx_header_present"
            ]
        )

        self.client.cookies.set(server.DEVELOPMENT_IDENTITY_COOKIE, "tampered")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_projects_are_shared_and_paths_are_not_exposed(self):
        for headers in (self.user1_headers, self.user2_headers):
            response = self.client.get("/api/projects", headers=headers)
            self.assertEqual(response.status_code, 200)
            projects = response.json()["projects"]
            self.assertEqual(
                [item["id"] for item in projects],
                ["hpi-analytics", "nmdb-analytics"],
            )
            self.assertNotIn("path", projects[0])
            self.assertNotIn("relative_path", projects[0])

    def test_only_project_admin_can_mutate_projects_and_settings(self):
        denied = self.client.post(
            "/api/projects", headers=self.user1_headers, json={"name": "Private"}
        )
        self.assertEqual(denied.status_code, 403)
        created = self.client.post(
            "/api/projects", headers=self.admin_headers, json={"name": "Shared Test"}
        )
        self.assertEqual(created.status_code, 200)
        project_id = created.json()["project"]["id"]
        deleted = self.client.delete(
            f"/api/projects/{project_id}", headers=self.admin_headers
        )
        self.assertEqual(deleted.status_code, 200)

        settings = self.client.get("/api/settings", headers=self.user1_headers).json()[
            "settings"
        ]
        denied_settings = self.client.put(
            "/api/settings", headers=self.user1_headers, json=settings
        )
        self.assertEqual(denied_settings.status_code, 403)

    def test_sessions_and_messages_are_owner_scoped(self):
        created = self.client.post(
            "/api/projects/hpi-analytics/sessions",
            headers=self.user1_headers,
            json={"title": "User one"},
        )
        self.assertEqual(created.status_code, 200)
        session_id = created.json()["session"]["id"]

        self.assertEqual(
            self.client.get(
                f"/api/projects/hpi-analytics/sessions/{session_id}",
                headers=self.user1_headers,
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"/api/projects/hpi-analytics/sessions/{session_id}",
                headers=self.user2_headers,
            ).status_code,
            404,
        )

    def test_non_streaming_chat_records_private_run(self):
        response = self.client.post(
            "/api/chat",
            headers=self.user1_headers,
            json={"project_id": "hpi-analytics", "message": "hello"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["run_id"])
        self.assertGreaterEqual(body["duration_seconds"], 0)
        session_id = body["session_id"]
        session = self.client.get(
            f"/api/projects/hpi-analytics/sessions/{session_id}",
            headers=self.user1_headers,
        ).json()
        self.assertGreaterEqual(session["messages"][-1]["duration_seconds"], 0)
        self.assertEqual(
            self.client.get(
                f"/api/projects/hpi-analytics/sessions/{session_id}",
                headers=self.user2_headers,
            ).status_code,
            404,
        )

    def test_streaming_chat_completes_run(self):
        response = self.client.post(
            "/api/chat/stream",
            headers=self.user1_headers,
            json={"project_id": "hpi-analytics", "message": "stream hello"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: run", response.text)
        self.assertIn("event: final", response.text)
        self.assertIn('"duration_seconds":', response.text)
        user_id = self._user_id(self.user1_headers)
        with server.get_db_connection() as conn:
            row = conn.execute(
                "SELECT status FROM task_runs WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        self.assertEqual(row["status"], "completed")

    def test_active_run_status_is_available_after_navigation(self):
        user_id = self._user_id(self.user1_headers)
        session = server.create_session(user_id, "hpi-analytics", "Still working")
        run = server.create_task_run(
            user_id, "hpi-analytics", session["id"], "long-running request"
        )
        server.update_task_run_activity(run["id"], "Working with project data…")

        sessions = self.client.get(
            "/api/projects/hpi-analytics/sessions", headers=self.user1_headers
        ).json()["sessions"]
        active_session = next(item for item in sessions if item["id"] == session["id"])
        self.assertEqual(active_session["active_run_id"], run["id"])
        self.assertEqual(
            active_session["active_run_status"], "Working with project data…"
        )

        runs = self.client.get(
            f"/api/projects/hpi-analytics/sessions/{session['id']}/runs",
            headers=self.user1_headers,
        ).json()["runs"]
        self.assertEqual(runs[0]["status"], "running")
        self.assertEqual(runs[0]["latest_status"], "Working with project data…")
        self.assertEqual(
            self.client.get(
                f"/api/projects/hpi-analytics/sessions/{session['id']}/runs",
                headers=self.user2_headers,
            ).status_code,
            404,
        )

        server.finish_task_run(run["id"], "completed")
        server.cleanup_run_work(run["context"])

    def test_agent_events_have_user_facing_activity_statuses(self):
        self.assertEqual(
            server.task_status_from_event((), {"name": "model", "input": {}}),
            "Analyzing the request…",
        )
        self.assertEqual(
            server.task_status_from_event(
                ("tools:abc",), {"name": "tools", "input": {}}
            ),
            "A project specialist is working with the data…",
        )

    def test_startup_recovery_fails_interrupted_runs_and_cleans_work(self):
        user_id = self._user_id(self.user1_headers)
        session = server.create_session(user_id, "hpi-analytics", "Interrupted")
        run = server.create_task_run(
            user_id, "hpi-analytics", session["id"], "unfinished work"
        )
        work_file = run["context"].work_directory / "partial.py"
        work_file.write_text("print('partial')\n", encoding="utf-8")

        server.recover_interrupted_runs()

        with server.get_db_connection() as conn:
            row = conn.execute(
                "SELECT status, error_summary FROM task_runs WHERE id = ?",
                (run["id"],),
            ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_summary"], "Server restarted during the run")
        self.assertFalse(run["context"].work_directory.exists())

    def test_session_limit_is_per_user_and_project(self):
        user1_id = self._user_id(self.user1_headers)
        user2_id = self._user_id(self.user2_headers)
        for user_id in (user1_id, user2_id):
            for index in range(3):
                server.create_session(user_id, "hpi-analytics", f"Session {index}")
        server.prune_project_sessions(user1_id, "hpi-analytics", 2)
        self.assertEqual(len(server.list_project_sessions(user1_id, "hpi-analytics", False)), 2)
        self.assertEqual(len(server.list_project_sessions(user2_id, "hpi-analytics", False)), 3)

    def test_checkpoint_thread_ids_include_user_project_and_session(self):
        first = server.session_thread_id("user-a", "hpi-analytics", "same")
        second = server.session_thread_id("user-b", "hpi-analytics", "same")
        self.assertEqual(first, "user:user-a:project:hpi-analytics:session:same")
        self.assertNotEqual(first, second)

    def test_artifacts_are_private_and_deleted_with_session(self):
        user1_id = self._user_id(self.user1_headers)
        session = server.create_session(user1_id, "hpi-analytics", "Artifacts")
        run = server.create_task_run(
            user1_id, "hpi-analytics", session["id"], "create chart"
        )
        context = run["context"]
        work_path = context.work_directory / "generated.py"
        work_path.write_text("print('temporary')\n", encoding="utf-8")
        artifact_path = context.artifact_directory / "chart.png"
        artifact_path.write_bytes(b"not-a-real-png")
        artifact = server.register_run_artifacts(context)[0]
        server.add_chat_message(
            user1_id,
            "hpi-analytics",
            session["id"],
            "assistant",
            "Generated a chart.",
            run["id"],
        )
        server.finish_task_run(run["id"], "completed")

        own = self.client.get(
            f"/api/artifacts/{artifact['id']}", headers=self.user1_headers
        )
        other = self.client.get(
            f"/api/artifacts/{artifact['id']}", headers=self.user2_headers
        )
        other_admin = self.client.get(
            f"/api/artifacts/{artifact['id']}", headers=self.admin_headers
        )
        self.assertEqual(own.status_code, 200)
        self.assertEqual(other.status_code, 404)
        self.assertEqual(other_admin.status_code, 404)

        deleted = self.client.delete(
            f"/api/projects/hpi-analytics/sessions/{session['id']}",
            headers=self.user1_headers,
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(artifact_path.exists())
        self.assertFalse(work_path.exists())
        with server.get_db_connection() as conn:
            session_count = conn.execute(
                "SELECT COUNT(*) AS count FROM chat_sessions WHERE id = ?",
                (session["id"],),
            ).fetchone()["count"]
            self.assertEqual(session_count, 0)
            for table in ("chat_messages", "task_runs", "artifacts"):
                count = conn.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE session_id = ?",
                    (session["id"],),
                ).fetchone()["count"]
                self.assertEqual(count, 0, table)

    def test_python_outputs_are_private_session_artifacts(self):
        user_id = self._user_id(self.user1_headers)
        session = server.create_session(user_id, "nmdb-analytics", "Private output")
        run = server.create_task_run(
            user_id, "nmdb-analytics", session["id"], "create private output"
        )
        context = run["context"]
        source_path = server.get_project_root("nmdb-analytics") / "data" / "input.csv"
        source_path.write_text("name,value\nexample,42\n", encoding="utf-8")
        code = """
import json
import os
from pathlib import Path

source = Path("data/input.csv").read_text(encoding="utf-8")
output = Path("outputs/result.json")
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({"source": source.strip()}), encoding="utf-8")
direct = Path(os.environ["DEEP_AGENTS_RUN_ARTIFACT_DIR"]) / "direct.txt"
direct.write_text(os.environ["DEEP_AGENTS_PROJECT_DIR"], encoding="utf-8")
print(output)
"""
        token = server.CURRENT_RUN_CONTEXT.set(context)
        try:
            result = server.execute_python_code(
                code, server.get_project_root("nmdb-analytics")
            )
        finally:
            server.CURRENT_RUN_CONTEXT.reset(token)

        self.assertIn("outputs/result.json", result)
        self.assertFalse(
            (server.get_project_root("nmdb-analytics") / "outputs").exists()
        )
        artifacts = server.register_run_artifacts(context)
        artifact_names = {item["display_name"] for item in artifacts}
        self.assertEqual(artifact_names, {"direct.txt", "result.json"})
        self.assertTrue((context.artifact_directory / "outputs" / "result.json").is_file())
        self.assertTrue((context.artifact_directory / "direct.txt").is_file())
        self.assertFalse((context.work_directory / "outputs" / "result.json").exists())
        self.assertEqual(
            context.work_directory,
            (
                server.WORK_DIR
                / user_id
                / "nmdb-analytics"
                / session["id"]
                / run["id"]
            ).resolve(),
        )

    def test_absolute_project_outputs_are_recovered_as_private_artifacts(self):
        user_id = self._user_id(self.user1_headers)
        session = server.create_session(user_id, "hpi-analytics", "Recovered output")
        run = server.create_task_run(
            user_id, "hpi-analytics", session["id"], "create output"
        )
        context = run["context"]
        code = """
import os
from pathlib import Path

output = Path(os.environ["DEEP_AGENTS_PROJECT_DIR"]) / "outputs" / "chart.txt"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("chart", encoding="utf-8")
"""
        token = server.CURRENT_RUN_CONTEXT.set(context)
        try:
            server.execute_python_code(code, server.get_project_root("hpi-analytics"))
        finally:
            server.CURRENT_RUN_CONTEXT.reset(token)

        project_output = server.get_project_root("hpi-analytics") / "outputs"
        self.assertFalse(project_output.exists())
        recovered = (
            context.artifact_directory
            / "recovered-project-writes"
            / "outputs"
            / "chart.txt"
        )
        self.assertEqual(recovered.read_text(encoding="utf-8"), "chart")
        artifacts = server.register_run_artifacts(context)
        self.assertEqual([item["display_name"] for item in artifacts], ["chart.txt"])

    def test_internal_download_paths_are_removed_from_new_and_stored_responses(self):
        user_id = self._user_id(self.user1_headers)
        session = server.create_session(user_id, "nmdb-analytics", "Artifacts")
        run = server.create_task_run(
            user_id, "nmdb-analytics", session["id"], "create export"
        )
        context = run["context"]
        csv_path = context.artifact_directory / "heatmap_data.csv"
        image_path = context.artifact_directory / "heatmap.png"
        csv_path.write_text("quarter,value\n2020Q1,1\n", encoding="utf-8")
        image_path.write_bytes(b"not-a-real-png")
        artifacts = server.register_run_artifacts(context)
        raw_response = (
            "The heatmap analysis is complete.\n\n"
            "**Heatmap:**\n\n"
            f"![Heatmap]({image_path})\n\n"
            "**Filtered data CSV:**\n\n"
            f"`{csv_path}`"
        )

        prepared = server.prepare_response_artifacts(
            user_id, "nmdb-analytics", raw_response, artifacts
        )
        self.assertNotIn(str(csv_path), prepared)
        self.assertNotIn(str(image_path), prepared)
        self.assertNotIn("**Heatmap:**", prepared)
        self.assertNotIn("Filtered data CSV", prepared)
        self.assertIn("### Generated artifacts", prepared)
        self.assertIn("[heatmap_data.csv](/api/artifacts/", prepared)
        self.assertIn("![heatmap.png](/api/artifacts/", prepared)
        self.assertEqual(prepared.count("/api/artifacts/"), 2)

        legacy_response = server.append_artifact_links(raw_response, artifacts)
        server.add_chat_message(
            user_id,
            "nmdb-analytics",
            session["id"],
            "assistant",
            legacy_response,
            run["id"],
        )
        server.finish_task_run(run["id"], "completed")
        loaded = self.client.get(
            f"/api/projects/nmdb-analytics/sessions/{session['id']}",
            headers=self.user1_headers,
        )
        self.assertEqual(loaded.status_code, 200)
        stored_content = loaded.json()["messages"][0]["content"]
        self.assertNotIn(str(csv_path), stored_content)
        self.assertNotIn(str(image_path), stored_content)
        self.assertNotIn("Filtered data CSV", stored_content)
        self.assertEqual(stored_content.count("### Generated artifacts"), 1)
        self.assertEqual(stored_content.count("/api/artifacts/"), 2)

    def test_relative_workspace_artifact_paths_are_removed_from_responses(self):
        user_id = self._user_id(self.user1_headers)
        session = server.create_session(user_id, "nmdb-analytics", "Relative artifact")
        run = server.create_task_run(
            user_id, "nmdb-analytics", session["id"], "create relative export"
        )
        context = run["context"]
        relative_output = context.work_directory / "outputs" / "result.csv"
        relative_output.parent.mkdir(parents=True)
        relative_output.write_text("value\n42\n", encoding="utf-8")
        artifacts = server.register_run_artifacts(context)
        prepared = server.prepare_response_artifacts(
            user_id,
            "nmdb-analytics",
            "Analysis complete.\n\nFiltered data CSV: `outputs/result.csv`",
            artifacts,
        )
        self.assertNotIn("outputs/result.csv", prepared)
        self.assertIn("### Generated artifacts", prepared)
        self.assertIn("[result.csv](/api/artifacts/", prepared)

    def test_empty_artifact_placeholder_sections_are_removed(self):
        user_id = self._user_id(self.user1_headers)
        session = server.create_session(user_id, "hpi-analytics", "Chart cleanup")
        run = server.create_task_run(
            user_id, "hpi-analytics", session["id"], "create chart"
        )
        context = run["context"]
        chart_path = context.artifact_directory / "indexed_growth.png"
        chart_path.write_bytes(b"not-a-real-png")
        artifacts = server.register_run_artifacts(context)
        raw_response = (
            "## Findings\n\nMountain outpaced the national index.\n\n"
            "---\n\n## Visualization\n\n"
            "The indexed growth-path chart was generated here:\n\n"
            "```text\n"
            f"{chart_path}\n"
            "```\n\n---\n\n"
            "## Key takeaways\n\nRegional performance diverged."
        )

        prepared = server.prepare_response_artifacts(
            user_id, "hpi-analytics", raw_response, artifacts
        )
        self.assertNotIn(str(chart_path), prepared)
        self.assertNotIn("## Visualization", prepared)
        self.assertNotIn("chart was generated here", prepared)
        self.assertNotIn("```text\n```", prepared)
        self.assertIn("## Findings", prepared)
        self.assertIn("## Key takeaways", prepared)
        self.assertIn("![indexed_growth.png](/api/artifacts/", prepared)

        # Session history normalizes already-prepared responses again, so the
        # cleanup and generated artifact section must remain stable.
        self.assertEqual(
            server.prepare_response_artifacts(
                user_id, "hpi-analytics", prepared, artifacts
            ),
            prepared,
        )

    def test_superseded_chart_attempts_are_not_presented(self):
        user_id = self._user_id(self.user1_headers)
        session = server.create_session(user_id, "hpi-analytics", "Chart selection")
        run = server.create_task_run(
            user_id, "hpi-analytics", session["id"], "create one chart"
        )
        context = run["context"]
        first_chart = context.artifact_directory / "monthly_trends.png"
        final_chart = context.artifact_directory / "monthly_trends_normalized.png"
        first_chart.write_bytes(b"first-chart")
        final_chart.write_bytes(b"final-chart")
        artifacts = server.register_run_artifacts(context)
        raw_response = (
            "## Visualization\n\nThe indexed growth-path chart was generated here:\n\n"
            "```text\n"
            f"{final_chart}\n"
            "```"
        )

        prepared = server.prepare_response_artifacts(
            user_id, "hpi-analytics", raw_response, artifacts
        )
        self.assertNotIn("monthly_trends.png]", prepared)
        self.assertIn("monthly_trends_normalized.png]", prepared)
        self.assertEqual(prepared.count("/api/artifacts/"), 1)

        # A legacy stored response may already contain links for both attempts
        # while its visualization placeholder is empty. Prefer the final image
        # and persist that selection on subsequent history normalization.
        legacy = server.append_artifact_links(
            "## Visualization\n\nThe chart was generated here:\n\n```text\n```",
            artifacts,
        )
        migrated = server.prepare_response_artifacts(
            user_id, "hpi-analytics", legacy, artifacts
        )
        self.assertNotIn("monthly_trends.png]", migrated)
        self.assertIn("monthly_trends_normalized.png]", migrated)
        self.assertEqual(migrated.count("/api/artifacts/"), 1)
        self.assertEqual(
            server.prepare_response_artifacts(
                user_id, "hpi-analytics", migrated, artifacts
            ),
            migrated,
        )

    def test_distinct_unreferenced_images_are_all_presented(self):
        user_id = self._user_id(self.user1_headers)
        session = server.create_session(user_id, "hpi-analytics", "Two charts")
        run = server.create_task_run(
            user_id, "hpi-analytics", session["id"], "create two charts"
        )
        context = run["context"]
        (context.artifact_directory / "growth.png").write_bytes(b"growth")
        (context.artifact_directory / "seasonality.png").write_bytes(b"seasonality")
        artifacts = server.register_run_artifacts(context)
        prepared = server.prepare_response_artifacts(
            user_id,
            "hpi-analytics",
            "## Findings\n\nThe growth and seasonality views show different patterns.",
            artifacts,
        )
        self.assertIn("growth.png]", prepared)
        self.assertIn("seasonality.png]", prepared)
        self.assertEqual(prepared.count("/api/artifacts/"), 2)

    def test_meaningful_visualization_analysis_is_preserved(self):
        user_id = self._user_id(self.user1_headers)
        session = server.create_session(user_id, "hpi-analytics", "Chart narrative")
        run = server.create_task_run(
            user_id, "hpi-analytics", session["id"], "analyze chart"
        )
        context = run["context"]
        chart_path = context.artifact_directory / "trend.png"
        chart_path.write_bytes(b"not-a-real-png")
        artifacts = server.register_run_artifacts(context)
        prepared = server.prepare_response_artifacts(
            user_id,
            "hpi-analytics",
            (
                "## Visualization\n\n"
                "The chart shows the Mountain Division separating from the national trend after 2012.\n\n"
                "Chart saved at: "
                f"{chart_path}"
            ),
            artifacts,
        )
        self.assertIn("## Visualization", prepared)
        self.assertIn("Mountain Division separating", prepared)
        self.assertNotIn("Chart saved at", prepared)
        self.assertNotIn(str(chart_path), prepared)
        self.assertIn("![trend.png](/api/artifacts/", prepared)

    def test_upload_preview_is_admin_only_and_single_user_owned(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("data/example.csv", "a,b\n1,2\n")
        files = {"file": ("project.zip", payload.getvalue(), "application/zip")}

        denied = self.client.post(
            "/api/uploads/preview", headers=self.user1_headers, files=files
        )
        self.assertEqual(denied.status_code, 403)
        preview = self.client.post(
            "/api/uploads/preview", headers=self.admin_headers, files=files
        )
        self.assertEqual(preview.status_code, 200)
        token = preview.json()["upload_token"]
        admin_id = self._user_id(self.admin_headers)
        self.assertTrue(server.upload_path_for_token(admin_id, token).exists())
        with self.assertRaises(Exception):
            server.upload_path_for_token(self._user_id(self.user1_headers), token)

    def test_admin_import_is_transactional_and_preview_is_single_use(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("data/example.csv", "a,b\n1,2\n")
            archive.writestr(
                "skills/example/SKILL.md",
                "---\nname: example\ndescription: Example import.\n---\n",
            )
        files = {"file": ("project.zip", payload.getvalue(), "application/zip")}
        preview = self.client.post(
            "/api/uploads/preview", headers=self.admin_headers, files=files
        ).json()
        imported = self.client.post(
            "/api/projects/import",
            headers=self.admin_headers,
            json={
                "name": "Imported Project",
                "upload_token": preview["upload_token"],
                "mode": "replace",
            },
        )
        self.assertEqual(imported.status_code, 200)
        project_id = imported.json()["project"]["id"]
        self.assertTrue(
            (server.get_project_root(project_id) / "data" / "example.csv").is_file()
        )
        reused = self.client.post(
            "/api/projects/import",
            headers=self.admin_headers,
            json={
                "name": "Reused Token",
                "upload_token": preview["upload_token"],
                "mode": "replace",
            },
        )
        self.assertEqual(reused.status_code, 404)

    def test_zip_traversal_is_rejected(self):
        payload = self.tmp_dir / "unsafe.zip"
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("../outside.txt", "bad")
        with self.assertRaises(Exception):
            server.inspect_zip(payload)

    def test_isolation_audit_is_admin_only_and_passes(self):
        denied = self.client.get(
            "/api/projects/hpi-analytics/isolation", headers=self.user1_headers
        )
        allowed = self.client.get(
            "/api/projects/hpi-analytics/isolation", headers=self.admin_headers
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.json()["audit"]["passed"])

    def test_skill_discovery_is_project_local(self):
        self.assertEqual(
            [item["name"] for item in server.scan_project_skills("hpi-analytics")],
            ["hpi-analysis"],
        )
        self.assertEqual(
            [item["name"] for item in server.scan_project_skills("nmdb-analytics")],
            ["nmdb-analytics"],
        )

    def test_runtime_storage_locations_are_configurable_and_validated(self):
        base = self.tmp_dir / "storage-config"
        projects = base / "projects"
        static = base / "static"
        projects.mkdir(parents=True)
        static.mkdir()
        with patch.dict(
            os.environ,
            {
                "DEEP_AGENTS_DB_DIR": "state/db",
                "DEEP_AGENTS_GENERATED_DIR": "state/generated",
            },
        ):
            paths = server.load_runtime_paths(base, projects, static)
        self.assertEqual(paths.app_database, (base / "state/db/app.db").resolve())
        self.assertEqual(
            paths.agent_database,
            (base / "state/db/agent-checkpoints.db").resolve(),
        )
        with patch.dict(
            os.environ,
            {
                "DEEP_AGENTS_DB_DIR": "projects/db",
                "DEEP_AGENTS_GENERATED_DIR": "state/generated-2",
            },
        ):
            with self.assertRaises(RuntimeError):
                server.load_runtime_paths(base, projects, static)


if __name__ == "__main__":
    unittest.main()
