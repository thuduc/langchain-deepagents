from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from deep_agents_app.api.routers import auth_router, chat_router, projects_router, sessions_router, settings_router
from deep_agents_app.services import workspace


@asynccontextmanager
async def app_lifespan(_: FastAPI):
    workspace.init_db()
    workspace.recover_interrupted_runs()
    try:
        yield
    finally:
        workspace.invalidate_all_agents()


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


def frontend_index():
    index_path = workspace.STATIC_DIR / "dist" / "index.html"
    if not index_path.is_file():
        raise HTTPException(
            status_code=503,
            detail="Frontend build is missing; run 'npm install && npm run build' in deep-agents-sdk/frontend",
        )
    return FileResponse(index_path)


def create_app() -> FastAPI:
    application = FastAPI(title="Deep Agents Project Workspace", lifespan=app_lifespan)
    application.middleware("http")(security_headers)
    application.include_router(auth_router)
    application.include_router(settings_router)
    application.include_router(projects_router)
    application.include_router(sessions_router)
    application.include_router(chat_router)
    application.add_api_route("/", frontend_index, methods=["GET"], include_in_schema=False)
    application.add_api_route("/projects/{frontend_path:path}", frontend_index, methods=["GET"], include_in_schema=False)
    application.mount("/static", StaticFiles(directory=workspace.STATIC_DIR), name="static")
    return application


app = create_app()
