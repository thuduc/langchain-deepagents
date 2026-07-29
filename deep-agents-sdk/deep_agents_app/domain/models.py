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
class ProjectContext:
    """Everything about a project the agent needs in order to run.

    Exists so the agent half can work without the application database. The web
    tier reads these from its own tables and passes them along; an agent running
    in a Runtime microVM receives them in the invocation and hydrates `root`
    from object storage. Neither has to guess, and only one of them needs a
    database.

    `content_revision`, `name` and `model` together are also what makes a cached
    graph stale: the first covers content and skill edits, the second a rename,
    the third a settings change. Carrying them here means a cached graph can
    check itself without a query.
    """

    id: str
    name: str
    slug: str
    root: Path
    content_revision: int
    model: str

    @property
    def stamp(self) -> tuple:
        """What a graph built from this project depends on."""
        return (self.name, self.content_revision, self.model)

    @property
    def skills_dir(self) -> Path:
        """Where this project's skill definitions live."""
        return self.root / "skills"


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
