"""The API's route contexts, built once per process and shared by every test's app.

FastAPI builds each route's dependants again for every app (about 1,130 contexts, 2.5 s,
measured 2026-11-24 on fastapi 0.165.0). Between the tests' apps they differ only in
whose `dependency_overrides` they read, so the shared ones read the serving app's."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.routing import _IncludedRouter

_serving: ContextVar[FastAPI | None] = ContextVar("shared_routes_serving", default=None)


class _ServingOverrides:
    @property
    def dependency_overrides(self) -> dict[Any, Any]:
        app = _serving.get()
        return {} if app is None else app.dependency_overrides


def install() -> None:
    """Share what `FastAPI.include_router(router, prefix=...)` adds (the API tree,
    `main._api_router`) between apps that include the same routers the same way."""
    overrides = _ServingOverrides()
    shared: dict[tuple[Any, ...], _IncludedRouter] = {}
    include, call = FastAPI.include_router, FastAPI.__call__

    def include_router(self: FastAPI, router: APIRouter, **kwargs: Any) -> None:
        children = [r for r in router.routes if isinstance(r, _IncludedRouter)]
        if set(kwargs) != {"prefix"} or not children or len(children) != len(router.routes):
            include(self, router, **kwargs)
            return
        assert not (router.on_startup or router.on_shutdown), "a shared include runs no handlers"
        # A child's contexts follow its routes' version, so a router that gains a route
        # after it was shared is built again.
        key = (
            kwargs["prefix"],
            tuple(d.dependency for d in self.router.dependencies),
            tuple(id(child.original_router) for child in children),
        )
        if key not in shared:
            include(self, router, **kwargs)
            made = self.router.routes[-1]
            assert isinstance(made, _IncludedRouter)
            made.include_context.dependency_overrides_provider = overrides
            shared[key] = made
            return
        self.router.routes.append(shared[key])
        self.router._mark_routes_changed()

    async def served(self: FastAPI, scope: Any, receive: Any, send: Any) -> None:
        token = _serving.set(self)
        try:
            await call(self, scope, receive, send)
        finally:
            _serving.reset(token)

    FastAPI.include_router = include_router  # type: ignore[method-assign]
    FastAPI.__call__ = served  # type: ignore[method-assign]
