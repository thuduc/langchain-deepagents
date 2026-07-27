"""Runtime application settings: model choice and per-project session cap."""

from fastapi import APIRouter, Depends

from deep_agents_app.api.dependencies import get_current_user, require_project_admin
from deep_agents_app.domain import CurrentUser
from deep_agents_app.schemas import SettingsUpdateRequest
from deep_agents_app.services import workspace


router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def get_settings(_: CurrentUser = Depends(get_current_user)):
    """Return the runtime settings shown in the app's Settings dialog."""
    return {"settings": workspace.get_app_settings()}


@router.put("")
def update_settings(
    request: SettingsUpdateRequest,
    user: CurrentUser = Depends(require_project_admin),
):
    """Change the model and session cap for everyone. Administrators only."""
    settings = workspace.save_app_settings(
        request.default_model.strip(), request.max_sessions_per_project
    )
    workspace.add_audit_event(user.id, "settings.update")
    return {"settings": settings}
