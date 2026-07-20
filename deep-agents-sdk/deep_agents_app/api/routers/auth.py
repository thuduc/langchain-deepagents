from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from deep_agents_app.security import (
    AuthenticationError,
    PROJECT_ADMIN_ROLE,
    create_development_token,
    decode_development_token,
)
from deep_agents_app.api.dependencies import get_current_user
from deep_agents_app.domain import CurrentUser
from deep_agents_app.schemas import DevelopmentLoginRequest
from deep_agents_app.services import workspace


router = APIRouter(prefix="/api/auth", tags=["auth"])


def current_user_response(user: CurrentUser) -> Dict[str, Any]:
    return {
        "user": {
            "id": user.id,
            "subject": user.subject,
            "roles": sorted(user.roles),
            "is_project_admin": user.is_project_admin,
        }
    }


@router.get("/config")
def auth_config(
    request: Request,
    x_fnma_jws_token: Optional[str] = Header(default=None, alias="x-fnma-jws-token"),
):
    cdx_header_present = bool(x_fnma_jws_token and x_fnma_jws_token.strip())
    result: Dict[str, Any] = {
        "development_login_enabled": workspace.DEVELOPMENT_LOGIN_ENABLED,
        "cdx_header_present": cdx_header_present,
    }
    if workspace.DEVELOPMENT_LOGIN_ENABLED and not cdx_header_present:
        token = request.cookies.get(workspace.DEVELOPMENT_IDENTITY_COOKIE)
        if token:
            try:
                identity = decode_development_token(token, workspace.DEVELOPMENT_SIGNING_SECRET)
                result["development_identity"] = {
                    "subject": identity.subject,
                    "project_admin": identity.is_project_admin,
                }
            except AuthenticationError:
                pass
    return result


@router.post("/development-login")
def development_login(login: DevelopmentLoginRequest, request: Request, response: Response):
    if not workspace.DEVELOPMENT_LOGIN_ENABLED:
        raise HTTPException(status_code=404, detail="Development login is disabled")
    subject = login.subject.strip()
    if not subject:
        raise HTTPException(status_code=422, detail="User subject is required")
    roles = [PROJECT_ADMIN_ROLE] if login.project_admin else []
    token = create_development_token(
        subject,
        roles,
        workspace.DEVELOPMENT_SIGNING_SECRET,
        workspace.DEVELOPMENT_IDENTITY_LIFETIME_SECONDS,
    )
    response.set_cookie(
        workspace.DEVELOPMENT_IDENTITY_COOKIE,
        token,
        max_age=workspace.DEVELOPMENT_IDENTITY_LIFETIME_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    identity = decode_development_token(token, workspace.DEVELOPMENT_SIGNING_SECRET)
    workspace.init_db()
    return current_user_response(workspace.upsert_current_user(identity))


@router.get("/me")
def auth_me(user: CurrentUser = Depends(get_current_user)):
    return current_user_response(user)
