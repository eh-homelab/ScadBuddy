from __future__ import annotations

import importlib
import pkgutil
import re

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Match, compile_path

import scadbuddy.api
from scadbuddy.main import API_PREFIX, ROOT_ROUTE_MODULES, _api_router

#: Stand-ins for a path parameter, including literal segments some routes use, so
#: a parameterised route that would also match a literal one is caught.
SAMPLES = ("x", "1", "search", "check", "import", "latest", "upstream", "versions", "media")


def _route_modules() -> dict[str, APIRouter]:
    routers = {}
    for info in pkgutil.iter_modules(scadbuddy.api.__path__):
        router = getattr(importlib.import_module(f"scadbuddy.api.{info.name}"), "router", None)
        if isinstance(router, APIRouter) and info.name not in ROOT_ROUTE_MODULES:
            routers[info.name] = router
    return routers


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(_api_router())
    return app


def _matches(app: FastAPI, kind: str, path: str, method: str = "GET") -> bool:
    scope = {"type": kind, "path": path, "method": method, "root_path": "", "app": app}
    return any(route.matches(scope)[0] == Match.FULL for route in app.router.routes)


def test_every_route_module_is_mounted() -> None:
    app = _app()
    routers = _route_modules()
    assert {"models", "jobs", "realtime"} <= routers.keys()
    for name, router in routers.items():
        for route in router.routes:
            assert isinstance(route, APIRoute | APIWebSocketRoute), name
            kind = "websocket" if isinstance(route, APIWebSocketRoute) else "http"
            methods = getattr(route, "methods", None) or {"GET"}
            concrete = API_PREFIX + re.sub(r"\{[^}]+\}", "x", route.path)
            for method in methods:
                assert _matches(app, kind, concrete, method), (name, method, route.path)


def test_no_two_modules_match_the_same_request() -> None:
    # The modules are mounted in name order. That is only safe while no request can
    # match routes in two modules; within one module its own order still decides.
    operations = []
    for name, router in _route_modules().items():
        for route in router.routes:
            assert isinstance(route, APIRoute | APIWebSocketRoute), name
            for method in getattr(route, "methods", None) or {"WEBSOCKET"}:
                operations.append((compile_path(route.path)[0], method, route.path, name))
    assert operations
    for _, method, path, name in operations:
        for sample in SAMPLES:
            concrete = re.sub(r"\{[^}]+\}", sample, path)
            owners = {
                other
                for regex, other_method, _, other in operations
                if other_method == method and regex.match(concrete)
            }
            assert owners == {name}, (method, concrete, owners)
