"""Every route falls in one class (#4975 phase 3, spec 2026-11-01 §8.1), and what each
class takes."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from datetime import UTC, datetime

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute, APIWebSocketRoute

import scadbuddy.api
from scadbuddy.auth.models import ANONYMOUS, AuthSession, Person, Service, SessionKind
from scadbuddy.auth.routes import (
    EXCHANGE_ENDPOINTS,
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
        )


def test_every_named_endpoint_and_module_exists() -> None:
    names = {endpoint_name(route) for route in ROUTES}
    modules = {route.endpoint.__module__ for route in ROUTES}
    assert names >= PUBLIC_ENDPOINTS
    assert names >= PERSON_ENDPOINTS
    assert names >= EXCHANGE_ENDPOINTS
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


def test_no_header_is_needed_where_none_can_be_sent() -> None:
    assert would_refuse("api", _person(), request_header=False, header_needed=False) is None
