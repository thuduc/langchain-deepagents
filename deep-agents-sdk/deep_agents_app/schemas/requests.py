from typing import Optional

from pydantic import BaseModel, Field


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)


class ProjectUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1)


class ProjectImportRequest(BaseModel):
    name: str = Field(..., min_length=1)
    upload_token: str = Field(..., min_length=1)
    mode: str = "merge"


class ContentImportRequest(BaseModel):
    upload_token: str = Field(..., min_length=1)
    mode: str = "merge"


class SessionCreateRequest(BaseModel):
    title: Optional[str] = None


class SettingsUpdateRequest(BaseModel):
    default_model: str = Field(..., min_length=1)
    max_sessions_per_project: int = Field(..., ge=1, le=100)


class DevelopmentLoginRequest(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    project_admin: bool = False


class ChatRequest(BaseModel):
    message: str
    project_id: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    project_id: str
    session_id: str
    run_id: str
    duration_seconds: float
