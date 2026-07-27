"""Domain objects passed between the API, services and runtime layers."""

from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet

from deep_agents_app.security import PROJECT_ADMIN_ROLE


@dataclass(frozen=True)
class RunContext:
    """Identifies one agent run and where its output is staged.

    Set as a contextvar for the duration of a run so the sandbox tool can tell
    which user, project and session it is executing on behalf of. Every field is
    derived from authenticated state, never from a request body.
    """

    user_id: str
    project_id: str
    session_id: str
    run_id: str
    artifact_directory: Path


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated caller, resolved from the CDX token on every request.

    `id` is this application's own user row; `subject` is the identity CDX
    asserted. Authorization reads only `roles`.
    """

    id: str
    issuer: str
    subject: str
    roles: FrozenSet[str]

    @property
    def is_project_admin(self) -> bool:
        """Whether this caller may create, edit or delete shared project content."""
        return PROJECT_ADMIN_ROLE in self.roles
