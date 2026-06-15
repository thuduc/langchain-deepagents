import importlib.util
import shutil
import tempfile
import unittest
import warnings
from pathlib import Path

from fastapi import HTTPException


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_PATH = REPO_ROOT / "deep-agents-sdk" / "server.py"

spec = importlib.util.spec_from_file_location("server", SERVER_PATH)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ProjectIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="isolation-db-"))
        self.original_db_path = server.DB_PATH
        self.original_agent_checkpoint_db_path = server.AGENT_CHECKPOINT_DB_PATH
        server.DB_PATH = self.tmp_dir / "checkpoints.db"
        server.AGENT_CHECKPOINT_DB_PATH = self.tmp_dir / "agent_checkpoints.db"
        server._SKILLS_VALIDATED = True
        server._agent_graphs.clear()
        server.init_db()
        self._insert_project(
            "nmdb-analytics",
            "NMDB Analytics",
            REPO_ROOT / "projects" / "nmdb-analytics",
        )

    def tearDown(self):
        server.DB_PATH = self.original_db_path
        server.AGENT_CHECKPOINT_DB_PATH = self.original_agent_checkpoint_db_path
        server._agent_graphs.clear()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _insert_project(self, project_id, name, path):
        now = server.utc_now()
        with server.get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO projects (id, name, slug, path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (project_id, name, project_id, str(path), now, now),
            )

    def test_skill_discovery_is_project_local(self):
        hpi_skills = server.scan_project_skills("hpi-analytics")
        nmdb_skills = server.scan_project_skills("nmdb-analytics")

        self.assertEqual([skill["name"] for skill in hpi_skills], ["hpi-analysis"])
        self.assertEqual([skill["name"] for skill in nmdb_skills], ["nmdb-analytics"])

    def test_project_isolation_audit_passes_for_each_project(self):
        for project_id in ("hpi-analytics", "nmdb-analytics"):
            with self.subTest(project_id=project_id):
                audit = server.project_isolation_audit(project_id)

                self.assertTrue(audit["passed"])
                self.assertTrue(all(audit["checks"].values()))
                self.assertEqual(audit["filesystem_backend"]["virtual_mode"], True)
                self.assertEqual(audit["filesystem_backend"]["root_dir"], audit["project_root"])
                self.assertEqual(audit["skills_source"], [("/skills", audit["project_name"])])
                self.assertNotEqual(
                    audit["checkpointing"]["app_db"],
                    audit["checkpointing"]["agent_checkpoint_db"],
                )
                self.assertIn(
                    f"project:{project_id}:session:",
                    audit["checkpointing"]["sample_thread_id"],
                )
                self.assertIn(
                    f"/static/charts/{project_id}/",
                    audit["artifacts"]["chart_url_prefix"],
                )

    def test_relative_project_paths_cannot_escape_to_another_project(self):
        hpi_root = server.get_project_root("hpi-analytics")

        with self.assertRaises(HTTPException):
            server.relative_project_path(
                hpi_root,
                "../nmdb-analytics/data/DATA_DICTIONARY.md",
            )

    def test_media_resolution_does_not_expose_other_project_files(self):
        hpi_image = (
            REPO_ROOT
            / "projects"
            / "hpi-analytics"
            / "examples"
            / "tx_fl_ny_fhfa_hpi_quarterly_2010_2025.png"
        )

        self.assertIsNone(server.project_media_url("nmdb-analytics", hpi_image))

    def test_checkpoint_thread_ids_are_project_and_session_scoped(self):
        session_id = "same-session-id"

        self.assertEqual(
            server.session_thread_id("hpi-analytics", session_id),
            "project:hpi-analytics:session:same-session-id",
        )
        self.assertEqual(
            server.session_thread_id("nmdb-analytics", session_id),
            "project:nmdb-analytics:session:same-session-id",
        )
        self.assertNotEqual(
            server.session_thread_id("hpi-analytics", session_id),
            server.session_thread_id("nmdb-analytics", session_id),
        )

    def test_isolation_endpoint_returns_audit_report(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from fastapi.testclient import TestClient

            client = TestClient(server.app)

            response = client.get("/api/projects/hpi-analytics/isolation")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["audit"]["passed"])
        self.assertEqual(body["audit"]["project_id"], "hpi-analytics")


if __name__ == "__main__":
    unittest.main()
