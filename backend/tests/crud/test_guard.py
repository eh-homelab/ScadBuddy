"""The CRUD routers' guard (#5298): refused without the caller's credential in every
mode, and every answer carries ``X-ScadBuddy-Crud``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from scadbuddy.auth.check import auth_check
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.models import Service
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.crud.guard import crud_router

CREDENTIALS = {
    "sbsvc_projects": Service("projects", frozenset({"crud.projects"})),
    "sbsvc_projects_bare": Service("projects", frozenset({"api"})),
    "sbsvc_print": Service("print", frozenset({"crud.print"})),
    "sbsvc_render": Service("render", frozenset({"crud.render"})),
    "sbsvc_agent": Service("agent", frozenset({"api", "internal.workspace"})),
}


class Credentials:
    def resolve(self, token: str) -> Service | None:
        return CREDENTIALS.get(token)


class Body(BaseModel):
    n: int


class _Noted:
    def note(self, *args: object) -> None: ...


def _client(mode: str = "off") -> TestClient:
    """The router behind the real app-level `auth_check`, as `main.py` mounts it."""
    app = FastAPI(dependencies=[Depends(auth_check)])
    auth = SimpleNamespace(would_refuse=_Noted())
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode=mode),
        components=SimpleNamespace(get=lambda _component: auth),
    )
    router = crud_router("projects")

    @router.get("/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    @router.post("/body")
    def body(item: Body) -> dict[str, int]:
        return {"n": item.n}

    app.include_router(router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: None, credentials=lambda: Credentials())  # type: ignore[arg-type,return-value]
    return TestClient(app)


URL = "/api/v1/internal/crud/projects/ping"


def _get(client: TestClient, token: str | None) -> Any:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return client.get(URL, headers=headers)


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
def test_the_guard_refuses_in_every_mode(mode: str) -> None:
    """The same status, problem type and headers in each mode (D2, D4)."""
    client = _client(mode)
    none = _get(client, None)
    assert none.status_code == 401
    assert none.json()["type"].endswith("/auth/signed-out")
    dead = _get(client, "sbsvc_dead")
    assert dead.status_code == 401
    assert none.headers["WWW-Authenticate"] == 'Bearer realm="scadbuddy"'
    for token in ("sbsvc_print", "sbsvc_render", "sbsvc_agent", "sbsvc_projects_bare"):
        refused = _get(client, token)
        assert refused.status_code == 403, token
        assert refused.json()["type"].endswith("/auth/forbidden")
    ok = _get(client, "sbsvc_projects")
    assert ok.status_code == 200
    assert ok.json() == {"ok": True}
    for response in (none, dead, ok, _get(client, "sbsvc_print"), _get(client, "sbsvc_render")):
        assert response.headers["X-ScadBuddy-Crud"] == "1"
        assert response.headers["Cache-Control"] == "no-store"


def test_a_validation_error_carries_the_header() -> None:
    client = _client()
    headers = {"authorization": "Bearer sbsvc_projects"}
    bad = client.post("/api/v1/internal/crud/projects/body", json={"n": "x"}, headers=headers)
    assert bad.status_code == 422
    assert bad.headers["X-ScadBuddy-Crud"] == "1"
    assert client.post(
        "/api/v1/internal/crud/projects/body", json={"n": 2}, headers=headers
    ).json() == {"n": 2}


def test_a_path_no_route_matches_carries_no_header() -> None:
    """D4 is the routers' own answers; the app's 404 is not one of them."""
    response = _client().get("/api/v1/internal/crud/projects/nope")
    assert response.status_code == 404
    assert "X-ScadBuddy-Crud" not in response.headers
