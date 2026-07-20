import sys
import unittest
from pathlib import Path


SDK_ROOT = Path(__file__).resolve().parents[1]
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from deep_agents_app.application import app, create_app  # noqa: E402


EXPECTED_API_OPERATIONS = {
    ("GET", "/api/auth/config"),
    ("POST", "/api/auth/development-login"),
    ("GET", "/api/auth/me"),
    ("GET", "/api/settings"),
    ("PUT", "/api/settings"),
    ("GET", "/api/projects"),
    ("POST", "/api/projects"),
    ("PUT", "/api/projects/{project_id}"),
    ("GET", "/api/projects/{project_id}"),
    ("GET", "/api/projects/{project_id}/isolation"),
    ("DELETE", "/api/projects/{project_id}"),
    ("GET", "/api/projects/{project_id}/contents"),
    ("DELETE", "/api/projects/{project_id}/contents"),
    ("POST", "/api/uploads/preview"),
    ("POST", "/api/projects/import"),
    ("POST", "/api/projects/{project_id}/contents/import"),
    ("GET", "/api/projects/{project_id}/media/{media_path}"),
    ("GET", "/api/projects/{project_id}/sessions"),
    ("POST", "/api/projects/{project_id}/sessions"),
    ("GET", "/api/projects/{project_id}/sessions/{session_id}"),
    ("GET", "/api/projects/{project_id}/sessions/{session_id}/runs"),
    ("DELETE", "/api/projects/{project_id}/sessions/{session_id}"),
    ("GET", "/api/artifacts/{artifact_id}"),
    ("POST", "/api/chat"),
    ("POST", "/api/chat/stream"),
}


class ApiContractTests(unittest.TestCase):
    def test_openapi_operations_remain_stable(self):
        schema = app.openapi()
        operations = {
            (method.upper(), path.replace("{media_path}", "{media_path}"))
            for path, methods in schema["paths"].items()
            for method in methods
        }
        self.assertEqual(operations, EXPECTED_API_OPERATIONS)

    def test_application_factory_returns_independent_apps(self):
        first = create_app()
        second = create_app()
        self.assertIsNot(first, second)
        self.assertEqual(first.title, app.title)
        self.assertEqual(second.title, app.title)


if __name__ == "__main__":
    unittest.main()
