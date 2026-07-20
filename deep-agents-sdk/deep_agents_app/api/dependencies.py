"""Shared authentication and authorization dependencies."""

from deep_agents_app.services.workspace import get_current_user, require_project_admin

__all__ = ["get_current_user", "require_project_admin"]
