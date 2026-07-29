"""Request and response bodies. Field constraints are the first input check."""

from typing import Optional

from pydantic import BaseModel, Field


class ProjectCreateRequest(BaseModel):
    """A new, empty project."""

    name: str = Field(..., min_length=1)


class ProjectUpdateRequest(BaseModel):
    """A rename. Does not touch project content."""

    name: str = Field(..., min_length=1)


class ProjectImportRequest(BaseModel):
    """Create a project from an uploaded ZIP. `mode` is 'merge' or 'replace'."""

    name: str = Field(..., min_length=1)
    upload_token: str = Field(..., min_length=1)
    mode: str = "merge"


class ContentImportRequest(BaseModel):
    """Import content into an existing project. `mode` is 'merge' or 'replace'."""

    upload_token: str = Field(..., min_length=1)
    mode: str = "merge"


class ProjectFolderCreateRequest(BaseModel):
    """A new folder inside a project."""

    parent_path: str = Field(default="", max_length=2_000)
    name: str = Field(..., min_length=1, max_length=255)


class SessionCreateRequest(BaseModel):
    """A new conversation, optionally pre-titled."""

    title: Optional[str] = None


class SettingsUpdateRequest(BaseModel):
    """Runtime settings. The model must be in the configured allowlist."""

    default_model: str = Field(..., min_length=1)
    max_sessions_per_project: int = Field(..., ge=1, le=100)


class DevelopmentLoginRequest(BaseModel):
    """A local identity to assume. Ignored unless development login is enabled."""

    subject: str = Field(..., min_length=1, max_length=200)
    project_admin: bool = False


class ChatRequest(BaseModel):
    """A prompt. Omitting session_id starts a new conversation."""

    message: str
    project_id: str
    session_id: Optional[str] = None


class AgentInvocationRequest(BaseModel):
    """One run, as the agent tier receives it.

    Every field is identity the web tier has already established. The agent tier
    cannot authenticate anyone -- it has no user session and no database -- so it
    accepts these as given, and is reachable only by the web tier.
    """

    user_id: str = Field(..., min_length=1, max_length=200)
    project_id: str = Field(..., min_length=1, max_length=200)
    session_id: str = Field(..., min_length=1, max_length=200)
    run_id: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=100_000)
    # Project facts the agent cannot look up for itself. The slug is what it
    # hydrates project files from object storage with, so it is constrained to
    # the shape slugify() produces rather than accepted as free text.
    project_name: str = Field(..., min_length=1, max_length=200)
    project_slug: str = Field(..., min_length=1, max_length=200, pattern=r"^[a-z0-9][a-z0-9-]*$")
    content_revision: int = Field(..., ge=0)
    model: str = Field(..., min_length=1, max_length=200)
