"""Sandbox backend selection.

    DEEP_AGENTS_SANDBOX=local       development only
    DEEP_AGENTS_SANDBOX=agentcore   production

Callers depend on the contract in `base`, never on a concrete backend.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Dict

from deep_agents_app.runtime.sandbox.base import (
    ExecutionResult,
    SandboxBackend,
    SandboxError,
    SandboxSession,
    SessionSpec,
    describe,
)


logger = logging.getLogger(__name__)

DEFAULT_BACKEND = "local"

_backend_lock = threading.Lock()
_backend: SandboxBackend | None = None


def _build_local() -> SandboxBackend:
    from deep_agents_app.runtime.sandbox.local import LocalSandbox

    return LocalSandbox()


def _build_agentcore() -> SandboxBackend:
    from deep_agents_app.runtime.sandbox.agentcore import AgentCoreSandbox

    return AgentCoreSandbox()


BACKENDS: Dict[str, Callable[[], SandboxBackend]] = {
    "local": _build_local,
    "agentcore": _build_agentcore,
}


def configured_backend_name() -> str:
    """The backend named by DEEP_AGENTS_SANDBOX, defaulting to local."""
    return (os.environ.get("DEEP_AGENTS_SANDBOX") or DEFAULT_BACKEND).strip().lower()


def create_backend(name: str | None = None) -> SandboxBackend:
    """Build a backend without caching it. Useful in tests."""
    selected = (name or configured_backend_name()).lower()
    if selected not in BACKENDS:
        raise SandboxError(
            f"Unknown sandbox backend {selected!r}; expected one of "
            f"{', '.join(sorted(BACKENDS))}"
        )
    return BACKENDS[selected]()


def get_backend() -> SandboxBackend:
    """Return the process-wide backend, building and validating it once."""
    global _backend
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is None:
            backend = create_backend()
            backend.preflight()
            if backend.name == "local":
                logger.warning(
                    "Sandbox backend 'local' runs generated code as a child of this "
                    "server process. It is a development aid, not an isolation "
                    "boundary; use 'agentcore' for any shared deployment."
                )
            _backend = backend
    return _backend


def reset_backend() -> None:
    """Drop the cached backend. Intended for tests and configuration reloads."""
    global _backend
    with _backend_lock:
        _backend = None


__all__ = [
    "ExecutionResult",
    "SandboxBackend",
    "SandboxError",
    "SandboxSession",
    "SessionSpec",
    "configured_backend_name",
    "create_backend",
    "describe",
    "get_backend",
    "reset_backend",
]
