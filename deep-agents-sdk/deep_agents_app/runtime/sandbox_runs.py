"""Sandbox sessions, scoped to a single agent run.

A session opens on the first execute_python of a run and closes when the run
finishes. Nothing is kept warm between runs: the workspace is discarded after
every prompt anyway, and AgentCore bills memory for every second a session is
alive, so an idle one would cost money to cache state nothing reads.

This module deliberately takes plain values rather than importing the workspace
service, which would close an import cycle.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict

from deep_agents_app.domain import RunContext
from deep_agents_app.runtime.sandbox import (
    SandboxSession,
    SessionSpec,
    get_backend,
)


logger = logging.getLogger(__name__)

_sessions: Dict[str, SandboxSession] = {}
_sessions_guard = threading.Lock()


def session_for_run(
    context: RunContext,
    project_slug: str,
    project_data_dir: Path,
    timeout_seconds: int,
    content_revision: int = 0,
) -> SandboxSession:
    """Return this run's session, opening one on first use."""
    with _sessions_guard:
        existing = _sessions.get(context.run_id)
        if existing is not None:
            return existing

    backend = get_backend()
    # Safety net for the replication hook: a run must never read stale data, no
    # matter what happened when the content was edited.
    if not backend.project_data_current(project_slug, content_revision):
        logger.info(
            "Project %s data is not at revision %s in the sandbox backend; syncing",
            project_slug,
            content_revision,
        )
        backend.sync_project_data(project_slug, project_data_dir, content_revision)

    spec = SessionSpec(
        user_id=context.user_id,
        project_id=context.project_id,
        project_slug=project_slug,
        session_id=context.session_id,
        run_id=context.run_id,
        project_data_dir=project_data_dir,
        timeout_seconds=timeout_seconds,
    )
    session = backend.open_session(spec)

    with _sessions_guard:
        # Another thread may have opened one while this call was in flight.
        raced = _sessions.get(context.run_id)
        if raced is not None:
            session.close()
            return raced
        _sessions[context.run_id] = session
    return session


def close_run_session(context: RunContext) -> None:
    """Release the run's session. Safe when the run never executed any code."""
    with _sessions_guard:
        session = _sessions.pop(context.run_id, None)
    if session is not None:
        session.close()


def close_all_sessions() -> None:
    """Release every open session. Used at shutdown."""
    with _sessions_guard:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
