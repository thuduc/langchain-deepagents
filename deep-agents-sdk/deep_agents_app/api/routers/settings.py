from fastapi import APIRouter, Depends

from deep_agents_app.api.dependencies import get_current_user, require_project_admin
from deep_agents_app.domain import CurrentUser
from deep_agents_app.runtime import engine
from deep_agents_app.schemas import SettingsUpdateRequest
from deep_agents_app.services import sessions as sessions_service
from deep_agents_app.services import workspace


router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def get_settings(_: CurrentUser = Depends(get_current_user)):
    return {"settings": workspace.get_app_settings()}


@router.put("")
def update_settings(
    request: SettingsUpdateRequest,
    user: CurrentUser = Depends(require_project_admin),
):
    settings, model_changed = workspace.save_app_settings(
        request.default_model.strip(), request.max_sessions_per_project
    )
    if model_changed:
        engine.invalidate_all_agents()
    # Lowering the retention cap is the one write path that may delete chats.
    sessions_service.prune_all_project_sessions(settings["max_sessions_per_project"])
    workspace.add_audit_event(user.id, "settings.update")
    return {"settings": settings}
