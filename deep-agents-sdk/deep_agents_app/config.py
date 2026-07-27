"""Runtime storage configuration.

Resolves DEEP_AGENTS_DB_DIR and DEEP_AGENTS_GENERATED_DIR, and refuses to start
if either would overlap the shared projects tree, the public static directory,
or each other.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DB_DIR = "deep-agents-sdk/runtime/databases"
DEFAULT_GENERATED_DIR = "deep-agents-sdk/runtime/generated"


def _resolve_configured_path(base_dir: Path, env_name: str, default: str) -> Path:
    raw = os.environ.get(env_name, default).strip() or default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


@dataclass(frozen=True)
class RuntimePaths:
    """Every filesystem location the server writes to at runtime."""

    database_dir: Path
    generated_dir: Path
    app_database: Path
    agent_database: Path
    sandbox_dir: Path
    artifacts_dir: Path
    uploads_dir: Path

    def ensure(self) -> None:
        """Create each directory and prove it is writable.

        Probing at startup turns a bad volume mount into an immediate, obvious
        boot failure instead of a confusing error on a user's first prompt.
        """
        for path in (
            self.database_dir,
            self.generated_dir,
            self.sandbox_dir,
            self.artifacts_dir,
            self.uploads_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-probe"
            try:
                probe.write_text("ok", encoding="utf-8")
            finally:
                probe.unlink(missing_ok=True)


def load_runtime_paths(base_dir: Path, projects_dir: Path, static_dir: Path) -> RuntimePaths:
    """Resolve and validate the configured storage roots, or refuse to start.

    The checks exist because each overlap is a real hazard: runtime output
    inside PROJECTS_DIR would be picked up as project content, output inside the
    static directory would be served to anyone unauthenticated, and pointing
    either root at a filesystem root or the repository itself would put deletes
    somewhere dangerous.
    """
    base_dir = base_dir.resolve()
    projects_dir = projects_dir.resolve()
    static_dir = static_dir.resolve()
    database_dir = _resolve_configured_path(
        base_dir, "DEEP_AGENTS_DB_DIR", DEFAULT_DB_DIR
    )
    generated_dir = _resolve_configured_path(
        base_dir, "DEEP_AGENTS_GENERATED_DIR", DEFAULT_GENERATED_DIR
    )

    for label, path in (
        ("DEEP_AGENTS_DB_DIR", database_dir),
        ("DEEP_AGENTS_GENERATED_DIR", generated_dir),
    ):
        if path == Path(path.anchor) or path == base_dir:
            raise RuntimeError(f"{label} must point to a dedicated subdirectory")
        if _contains(static_dir, path) or _contains(path, static_dir):
            raise RuntimeError(f"{label} must not overlap the public static directory")
        if _contains(projects_dir, path) or _contains(path, projects_dir):
            raise RuntimeError(f"{label} must not overlap PROJECTS_DIR")

    if _contains(database_dir, generated_dir) or _contains(generated_dir, database_dir):
        raise RuntimeError(
            "DEEP_AGENTS_DB_DIR and DEEP_AGENTS_GENERATED_DIR must not overlap"
        )

    paths = RuntimePaths(
        database_dir=database_dir,
        generated_dir=generated_dir,
        app_database=database_dir / "app.db",
        agent_database=database_dir / "agent-checkpoints.db",
        sandbox_dir=generated_dir / "sandbox",
        artifacts_dir=generated_dir / "artifacts",
        uploads_dir=generated_dir / "uploads",
    )
    paths.ensure()
    return paths


def ensure_child_path(parent: Path, child: Path) -> Path:
    """Resolve `child` and assert it stays inside `parent`.

    The containment check used throughout the app wherever a path is derived
    from user input, so a crafted name or a symlink cannot escape its directory.
    Raises ValueError when it would.
    """
    parent_resolved = parent.resolve()
    child_resolved = child.resolve()
    if child_resolved != parent_resolved and parent_resolved not in child_resolved.parents:
        raise ValueError(f"Path {child_resolved} escapes {parent_resolved}")
    return child_resolved
