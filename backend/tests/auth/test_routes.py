"""Every route falls in one class (#4975 phase 3, spec 2026-11-01 §8.1), and what each
class takes."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import BaseRoute

import scadbuddy.api
from scadbuddy.auth.models import (
    ANONYMOUS,
    AuthSession,
    Person,
    PersonalToken,
    Service,
    SessionKind,
)
from scadbuddy.auth.routes import (
    EXCHANGE_ENDPOINTS,
    FLOW_HOST_ENDPOINTS,
    INTERNAL_MODULES,
    PERSON_ENDPOINTS,
    PUBLIC_ENDPOINTS,
    PUBLIC_MODULES,
    SCRIPTLESS_GETS,
    Route,
    endpoint_name,
    route_class,
    would_refuse,
)
from scadbuddy.core.settings import Settings
from scadbuddy.crud.guard import CRUD_PREFIXES, crud_router
from scadbuddy.main import create_app
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS


def _routes() -> list[Route]:
    routes: list[Route] = []
    for info in pkgutil.iter_modules(scadbuddy.api.__path__):
        router = getattr(importlib.import_module(f"scadbuddy.api.{info.name}"), "router", None)
        if isinstance(router, APIRouter):
            routes += [r for r in router.routes if isinstance(r, APIRoute | APIWebSocketRoute)]
    return routes


ROUTES = _routes()


def test_every_route_has_a_class() -> None:
    assert ROUTES
    for route in ROUTES:
        assert route_class(route) in (
            "public",
            "api",
            "person",
            "internal.outputs",
            "internal.library",
            "internal.workspace",
            "transfer",
            "exchange",
            "crud.projects",
            "crud.render",
            "crud.print",
            "crud.storefront",
            "flow.host",
        )


def test_every_named_endpoint_and_module_exists() -> None:
    names = {endpoint_name(route) for route in ROUTES}
    modules = {route.endpoint.__module__ for route in ROUTES}
    assert names >= PUBLIC_ENDPOINTS
    assert names >= PERSON_ENDPOINTS
    assert names >= EXCHANGE_ENDPOINTS
    assert names >= FLOW_HOST_ENDPOINTS
    assert modules >= PUBLIC_MODULES
    assert set(INTERNAL_MODULES) <= modules


def test_every_scriptless_get_is_a_get_route() -> None:
    gets = {
        (route.endpoint.__module__, route.path)
        for route in ROUTES
        if isinstance(route, APIRoute) and "GET" in (route.methods or ())
    }
    assert gets >= SCRIPTLESS_GETS, SCRIPTLESS_GETS - gets


def test_an_endpoint_only_a_person_may_call_is_person() -> None:
    """`_browser_only` (api/flows.py) refuses agents today; the gate's class must too."""
    for route in ROUTES:
        source = inspect.getsource(route.endpoint)
        if "_browser_only(" in source:
            assert route_class(route) == "person", endpoint_name(route)


def test_the_internal_routes_are_internal() -> None:
    for route in ROUTES:
        if route.path.startswith("/internal/"):
            assert route_class(route) != "api", endpoint_name(route)


def _mounted(routes: Sequence[BaseRoute]) -> list[Route]:
    """Every route of the app, through the nested routers FastAPI keeps (>= 0.140)."""
    found: list[Route] = []
    for route in routes:
        if isinstance(route, APIRoute | APIWebSocketRoute):
            found.append(route)
        elif (inner := getattr(route, "routes", None)) is not None:
            found += _mounted(inner)
        elif (router := getattr(route, "original_router", None)) is not None:
            found += _mounted(router.routes)
    return found


def _depends_on_guard(route: Route, caller: str) -> bool:
    return any(
        d.call is not None and d.call.__name__ == f"require_crud_{caller}"
        for d in _all_dependencies(route.dependant)
    )


def _all_dependencies(dependant: Dependant) -> list[Dependant]:
    return [d for sub in dependant.dependencies for d in (sub, *_all_dependencies(sub))]


def test_every_crud_route_has_its_callers_class_and_guard() -> None:
    app = create_app(
        Settings(
            frontend_dir=Path("/nonexistent"),
            database_url=UNUSED_DATABASE_URL,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
        )
    )
    checked: set[str] = set()
    for route in _mounted(app.routes):
        for caller, prefix in CRUD_PREFIXES.items():
            if re.match(rf"(?:/api/v1)?{prefix}/", route.path):
                assert route_class(route) == f"crud.{caller}", route.path
                assert _depends_on_guard(route, caller), route.path
                checked.add(caller)
    # Each caller's `GET …/areas` at least (`api/crud_areas.py`).
    assert checked == set(CRUD_PREFIXES)


def test_a_crud_router_route_is_classed_and_guarded() -> None:
    """The check above is empty until the first CRUD route lands; this one is not."""
    router = crud_router("render")

    @router.get("/x")
    def x() -> None: ...

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    routes = [r for r in _mounted(app.routes) if r.path.endswith("/crud/render/x")]
    assert len(routes) == 1
    assert route_class(routes[0]) == "crud.render"
    assert _depends_on_guard(routes[0], "render")
    assert not _depends_on_guard(routes[0], "print")


def test_the_storefront_router_has_its_own_prefix() -> None:
    """#5282: the one caller whose CRUD routes are not under `/internal/crud/`."""
    router = crud_router("storefront")

    @router.get("/x")
    def x() -> None: ...

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    routes = [r for r in _mounted(app.routes) if r.path.endswith("/x")]
    assert [r.path.removeprefix("/api/v1") for r in routes] == ["/internal/storefront/x"]
    assert route_class(routes[0]) == "crud.storefront"
    assert _depends_on_guard(routes[0], "storefront")


AT = datetime(2026, 11, 1, tzinfo=UTC)


def _person(kind: SessionKind = "login") -> Person:
    return Person(
        AuthSession(
            id_hash=b"\0" * 32,
            principal="user:i#s",
            issuer="i",
            subject="s",
            name=None,
            email=None,
            kind=kind,
            created_at=AT,
            last_seen_at=AT,
            idle_expires_at=AT,
            absolute_expires_at=AT,
        )
    )


@pytest.mark.parametrize(
    ("route_cls", "caller", "header", "reason"),
    [
        ("public", ANONYMOUS, False, None),
        ("transfer", ANONYMOUS, False, None),
        # #5494: the exchange's proof is in its body.
        ("exchange", ANONYMOUS, False, None),
        # The render worker's credential reaches no route yet (#5298 adds `crud.render`).
        ("api", Service("render", frozenset({"crud.render"})), False, "out_of_scope"),
        ("internal.outputs", Service("render", frozenset({"crud.render"})), False, "no_credential"),
        ("api", ANONYMOUS, False, "signed_out"),
        ("api", _person(), True, None),
        ("api", _person(), False, "no_request_header"),
        ("api", _person("delegated"), True, None),
        ("person", _person(), True, None),
        ("person", _person("break_glass"), True, None),
        ("person", _person("delegated"), True, "not_a_person"),
        ("person", Service("agent", frozenset({"api"})), True, "not_a_person"),
        ("person", ANONYMOUS, False, "signed_out"),
        ("internal.outputs", ANONYMOUS, False, "no_credential"),
        ("crud.render", ANONYMOUS, False, "no_credential"),
        ("crud.render", _person(), True, "no_credential"),
        ("crud.render", Service("render", frozenset({"crud.render"})), False, None),
        ("crud.render", Service("print", frozenset({"crud.print"})), False, "wrong_caller"),
        ("crud.render", Service("print", frozenset({"crud.render"})), False, "wrong_caller"),
        ("crud.projects", Service("projects", frozenset({"api"})), False, "out_of_scope"),
        # #5298: a flow host route takes everyone an `api` route takes, and the projects
        # credential, which holds `flow.host` and not `api`.
        ("flow.host", Service("projects", frozenset({"flow.host"})), False, None),
        ("api", Service("projects", frozenset({"flow.host"})), False, "out_of_scope"),
        ("flow.host", Service("projects", frozenset({"api"})), False, None),
        ("flow.host", Service("agent", frozenset({"api"})), False, None),
        ("flow.host", Service("print", frozenset({"internal.outputs"})), False, "out_of_scope"),
        ("flow.host", ANONYMOUS, False, "signed_out"),
        ("flow.host", _person(), True, None),
        ("flow.host", _person(), False, "no_request_header"),
        ("flow.host", _person("delegated"), True, None),
        ("internal.outputs", _person(), True, "no_credential"),
        ("internal.outputs", Service("print", frozenset({"internal.outputs"})), False, None),
        (
            "internal.library",
            Service("print", frozenset({"internal.outputs"})),
            False,
            "no_credential",
        ),
    ],
)
def test_would_refuse(route_cls: object, caller: object, header: bool, reason: object) -> None:
    assert (
        would_refuse(route_cls, caller, request_header=header, header_needed=True)  # type: ignore[arg-type]
        == reason
    )


def test_a_personal_token_takes_a_flow_host_route_as_an_api_one() -> None:
    for scope, method, reason in (
        ("read", "GET", None),
        ("read", "POST", "out_of_scope"),
        ("write", "POST", None),
    ):
        token = PersonalToken("t1", "user:i#s", scope)
        for route_cls in ("api", "flow.host"):
            assert (
                would_refuse(
                    route_cls, token, request_header=False, header_needed=True, method=method
                )
                == reason
            ), (route_cls, scope, method)


def test_no_header_is_needed_where_none_can_be_sent() -> None:
    assert would_refuse("api", _person(), request_header=False, header_needed=False) is None
