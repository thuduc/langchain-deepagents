"""Static guards for the module dependency rules.

These use AST analysis rather than imports so they stay fast and free of the
filesystem side effects that importing the application would cause.
"""

import ast
import sys
import unittest
from pathlib import Path


SDK_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = SDK_ROOT / "deep_agents_app"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

PACKAGE = "deep_agents_app"
WORKSPACE_MODULE = "deep_agents_app.services.workspace"

# Rebound by tests to temporary directories. Importing any of these by value
# captures the production path, so a failing test could write to real user data.
MUTABLE_STATE_NAMES = {
    "AGENT_CHECKPOINT_DB_PATH",
    "ARTIFACTS_DIR",
    "DB_PATH",
    "DEVELOPMENT_LOGIN_ENABLED",
    "DEVELOPMENT_SIGNING_SECRET",
    "PROJECTS_ROOT",
    "TMP_UPLOADS_DIR",
    "WORK_DIR",
}


def module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = [PACKAGE, *relative.parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def source_modules() -> dict[str, Path]:
    return {module_name(path): path for path in sorted(PACKAGE_ROOT.rglob("*.py"))}


def top_level_nodes(tree: ast.Module):
    """Yield nodes that execute at import time, skipping function bodies.

    A deferred import inside a function is the documented escape hatch and is
    intentionally not treated as a module-level dependency.
    """
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield node
        for child in ast.iter_child_nodes(node):
            pending.append(child)


def imported_modules(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in top_level_nodes(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith(PACKAGE))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(PACKAGE):
            found.add(node.module)
            found.update(
                f"{node.module}.{alias.name}"
                for alias in node.names
                if alias.name and alias.name[0].islower()
            )
    return found


class ModuleLayeringTests(unittest.TestCase):
    def setUp(self):
        self.modules = source_modules()
        self.trees = {
            name: ast.parse(path.read_text(encoding="utf-8"))
            for name, path in self.modules.items()
        }

    def test_module_level_imports_form_no_cycle(self):
        """Every module must be importable without importing another one first."""
        graph = {
            name: {target for target in imported_modules(tree) if target in self.modules}
            for name, tree in self.trees.items()
        }
        visiting: set[str] = set()
        done: set[str] = set()
        cycles: list[str] = []

        def walk(name: str, trail: list[str]) -> None:
            if name in done:
                return
            if name in visiting:
                start = trail.index(name)
                cycles.append(" -> ".join(trail[start:] + [name]))
                return
            visiting.add(name)
            for target in sorted(graph.get(name, ())):
                walk(target, trail + [target])
            visiting.discard(name)
            done.add(name)

        for name in sorted(graph):
            walk(name, [name])

        self.assertEqual(cycles, [], "module-level import cycles: " + "; ".join(cycles))

    def test_mutable_storage_roots_are_never_imported_by_value(self):
        offenders: list[str] = []
        for name, tree in self.trees.items():
            if name == WORKSPACE_MODULE:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.module != WORKSPACE_MODULE:
                    continue
                for alias in node.names:
                    if alias.name in MUTABLE_STATE_NAMES:
                        offenders.append(f"{name} imports {alias.name} by value")
        self.assertEqual(
            offenders,
            [],
            "read these as attributes of the workspace module instead: " + "; ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
