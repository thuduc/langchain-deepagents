"""FastAPI routers, one module per area of the API."""

from .auth import router as auth_router
from .chat import router as chat_router
from .projects import router as projects_router
from .sessions import router as sessions_router
from .settings import router as settings_router

__all__ = ["auth_router", "chat_router", "projects_router", "sessions_router", "settings_router"]
