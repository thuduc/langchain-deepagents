"""Backward-compatible Uvicorn entrypoint.

Application code lives in :mod:`deep_agents_app`; keeping this tiny module means
existing ``uvicorn server:app`` deployment commands continue to work.
"""

from deep_agents_app.application import app

__all__ = ["app"]
