from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet

from deep_agents_app.security import PROJECT_ADMIN_ROLE


@dataclass(frozen=True)
class RunContext:
    user_id: str
    project_id: str
    session_id: str
    run_id: str
    work_directory: Path
    artifact_directory: Path


@dataclass(frozen=True)
class CurrentUser:
    id: str
    issuer: str
    subject: str
    roles: FrozenSet[str]

    @property
    def is_project_admin(self) -> bool:
        return PROJECT_ADMIN_ROLE in self.roles
