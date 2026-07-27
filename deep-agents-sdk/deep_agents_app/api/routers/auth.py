"""Authentication endpoints.

CDX is the production authentication boundary: it validates the user and
overwrites the `x-fnma-jws-token` header, which this application trusts. The
development login here is an opt-in local substitute and is disabled by default.
"""

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
    """Shape a user for the browser. Deliberately excludes raw token claims."""
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
    """Tell the browser how to authenticate, before it has an identity.

    Unauthenticated by necessity: the UI calls this first to decide whether to
    show the development login prompt or rely on the CDX header. It reveals only
    which mechanism is available, never anything about a user.
    """
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
    """Issue a signed local identity cookie. Development only.

    Returns 404 rather than 403 when disabled so a production deployment does
    not advertise that the route exists. The cookie is HTTP-only and strictly
    same-site, and is signed with a secret held only by this server.
    """
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
    """Return the caller's identity, registering them on first sight."""
    return current_user_response(user)
