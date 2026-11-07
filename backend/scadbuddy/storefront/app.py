"""The edge's FastAPI app (#5282). It mounts no `/api/v1` route and never proxies one."""

from __future__ import annotations

from typing import Protocol

from fastapi import FastAPI

from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.storefront.routes import router
from scadbuddy.storefront.settings import StorefrontSettings


class StorefrontCrud(Protocol):
    """The CRUD service's storefront routes, as the edge calls them (the client is the
    next task's)."""


def create_app(settings: StorefrontSettings, *, crud: StorefrontCrud | None = None) -> FastAPI:
    app = FastAPI(title="ScadBuddy storefront", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.crud = crud
    install_problem_handlers(app)
    app.include_router(router)
    return app
