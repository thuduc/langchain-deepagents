from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from deep_agents_app.api.routers import auth_router, chat_router, projects_router, sessions_router, settings_router
from deep_agents_app.runtime import engine
from deep_agents_app.services import sessions as sessions_service
from deep_agents_app.services import uploads as uploads_service
from deep_agents_app.services import workspace


@asynccontextmanager
async def app_lifespan(_: FastAPI):
    workspace.init_db()
    sessions_service.recover_interrupted_runs()
    uploads_service.prune_expired_uploads()
    uploads_service.prune_stale_upload_staging()
    try:
        yield
    finally:
        await engine.aclose_all_agents()


async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'; form-action 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "x-fnma-jws-token, Cookie"
    return response


SPA_RESERVED_PREFIXES = ("api/", "static/")


def frontend_index():
    index_path = workspace.STATIC_DIR / "dist" / "index.html"
    if not index_path.is_file():
        raise HTTPException(
            status_code=503,
            detail="Frontend build is missing; run 'npm install && npm run build' in deep-agents-sdk/frontend",
        )
    return FileResponse(index_path)


def frontend_shell(full_path: str = ""):
    """Serve the single-page application shell for any client-side route.

    Registered last, so every declared API route and the static mount match
    first. A path under a reserved prefix stays a 404: answering a mistyped API
    request with HTML would look to the caller like a successful response.
    """
    if full_path.startswith(SPA_RESERVED_PREFIXES):
        raise HTTPException(status_code=404, detail="Not found")
    return frontend_index()


def create_app() -> FastAPI:
    application = FastAPI(title="Deep Agents Project Workspace", lifespan=app_lifespan)
    application.middleware("http")(security_headers)
    application.include_router(auth_router)
    application.include_router(settings_router)
    application.include_router(projects_router)
    application.include_router(sessions_router)
    application.include_router(chat_router)
    application.mount("/static", StaticFiles(directory=workspace.STATIC_DIR), name="static")
    application.add_api_route(
        "/{full_path:path}", frontend_shell, methods=["GET"], include_in_schema=False
    )
    return application


app = create_app()
