"""FastAPI application factory, lifespan and security middleware."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from deep_agents_app.api.routers import auth_router, chat_router, projects_router, sessions_router, settings_router
from deep_agents_app.runtime import sandbox_runs
from deep_agents_app.runtime.sandbox import get_backend
from deep_agents_app.services import artifact_store, workspace


@asynccontextmanager
async def app_lifespan(_: FastAPI):
    """Prepare the process at startup and release its resources at shutdown.

    Building the sandbox backend and the artifact store here validates their
    configuration while the process is still starting, so a misconfigured
    deployment fails visibly rather than on a user's first prompt.
    """
    workspace.init_db()
    workspace.recover_interrupted_runs()
    # Both of these preflight on first construction. Doing it at boot means a
    # bad bucket or a missing role fails the deploy, not somebody's first run
    # after it has already done all the work.
    get_backend()
    artifact_store.get_store()
    try:
        yield
    finally:
        sandbox_runs.close_all_sessions()
        workspace.invalidate_all_agents()


async def security_headers(request: Request, call_next):
    """Apply the response headers that constrain what the browser will do.

    The content security policy allows scripts only from this origin, which is
    why the frontend is self-hosted with no CDN. API responses are additionally
    marked private and no-store, and varied on the identity header, so a shared
    cache can never serve one user's data to another.
    """
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
    """Serve the built single-page app, or explain how to build it.

    Returns 503 with the build command when the Vite output is missing, since
    that is a deployment mistake rather than a missing page.
    """
    index_path = workspace.STATIC_DIR / "dist" / "index.html"
    if not index_path.is_file():
        raise HTTPException(
            status_code=503,
            detail="Frontend build is missing; run 'npm install && npm run build' in deep-agents-sdk/frontend",
        )
    return FileResponse(index_path)


def create_app() -> FastAPI:
    """Assemble the application: middleware, routers, SPA routes and statics.

    The `/projects/{path}` route exists so a deep link the user refreshes is
    answered with the SPA shell instead of a 404; client-side routing takes over
    from there.
    """
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
