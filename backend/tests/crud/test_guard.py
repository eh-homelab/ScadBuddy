"""The CRUD routers' guard (#5298): refused without the caller's credential in every
mode, and every answer carries ``X-ScadBuddy-Crud``."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.models import Service
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.crud.guard import crud_router

CREDENTIALS = {
    "sbsvc_projects": Service("projects", frozenset({"crud.projects"})),
    "sbsvc_projects_bare": Service("projects", frozenset({"api"})),
    "sbsvc_print": Service("print", frozenset({"crud.print"})),
    "sbsvc_agent": Service("agent", frozenset({"api", "internal.workspace"})),
}


class Credentials:
    def resolve(self, token: str) -> Service | None:
        return CREDENTIALS.get(token)


class Body(BaseModel):
    n: int


def _client() -> TestClient:
    app = FastAPI()
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


def test_the_guard_refuses_without_reading_the_auth_mode() -> None:
    """It reads no setting, so `off`, `observe` and `enforce` answer alike."""
    client = _client()
    none = _get(client, None)
    assert none.status_code == 401
    assert none.json()["type"].endswith("/auth/signed-out")
    dead = _get(client, "sbsvc_dead")
    assert dead.status_code == 401
    for token in ("sbsvc_print", "sbsvc_agent", "sbsvc_projects_bare"):
        refused = _get(client, token)
        assert refused.status_code == 403, token
        assert refused.json()["type"].endswith("/auth/forbidden")
    ok = _get(client, "sbsvc_projects")
    assert ok.status_code == 200
    assert ok.json() == {"ok": True}
    for response in (none, dead, ok, _get(client, "sbsvc_print")):
        assert response.headers["X-ScadBuddy-Crud"] == "1"
        assert response.headers["Cache-Control"] == "no-store"


def test_a_validation_error_and_a_missing_route_carry_the_header() -> None:
    client = _client()
    headers = {"authorization": "Bearer sbsvc_projects"}
    bad = client.post("/api/v1/internal/crud/projects/body", json={"n": "x"}, headers=headers)
    assert bad.status_code == 422
    assert bad.headers["X-ScadBuddy-Crud"] == "1"
    assert client.post(
        "/api/v1/internal/crud/projects/body", json={"n": 2}, headers=headers
    ).json() == {"n": 2}
