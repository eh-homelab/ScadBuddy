"""The CRUD listener (#5298, plan D3): a second port on the same app that answers only
the CRUD routes, the service-token exchange and ``/healthz``."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import crud_areas
from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.models import Service
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.core.settings import Settings
from scadbuddy.crud import listener
from scadbuddy.crud.listener import CrudListenerError, CrudOnly, crud_listener_port
from scadbuddy.main import create_app


@pytest.mark.parametrize(
    ("path", "served"),
    [
        ("/healthz", True),
        ("/api/v1/auth/service-token", True),
        ("/api/v1/internal/crud/render/areas", True),
        ("/api/v1/internal/crud/print/blob-index/k", True),
        ("/api/v1/models", False),
        ("/api/v1/internal/blobs/x", False),
        ("/api/v1/auth/service-token/x", False),
        ("/healthz/x", False),
        ("/api/v1/internal/crud", False),
        ("/api/v1/internal/crud/../../models", False),
        ("/api/v1/internal/crud//render/areas", False),
        ("/", False),
    ],
)
def test_the_filter_lets_only_its_paths_through(path: str, served: bool) -> None:
    app = FastAPI()

    @app.api_route("/{rest:path}", methods=["GET", "POST"])
    def anything() -> dict[str, bool]:
        return {"app": True}

    client = TestClient(CrudOnly(app))
    response = client.get(path)
    if served:
        assert response.json() == {"app": True}
    else:
        assert response.status_code == 404
        assert response.text == "Not Found"
        assert response.headers["content-type"].startswith("text/plain")


class _Credentials:
    def resolve(self, token: str) -> Service | None:
        return {
            "sbsvc_render": Service("render", frozenset({"crud.render"})),
            "sbsvc_print": Service("print", frozenset({"crud.print"})),
        }.get(token)


def test_each_caller_reads_its_own_areas(monkeypatch: pytest.MonkeyPatch) -> None:
    assert set(CRUD_AREAS) == {"projects", "render", "print"}
    monkeypatch.setitem(CRUD_AREAS, "render", ["blob-index"])
    app = FastAPI(dependencies=[Depends(auth_check)])
    app.state.scadbuddy = SimpleNamespace(settings=SimpleNamespace(auth_mode="off"))
    app.include_router(crud_areas.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: None, credentials=lambda: _Credentials())  # type: ignore[arg-type,return-value]
    client = TestClient(app)
    render = {"authorization": "Bearer sbsvc_render"}
    assert client.get("/api/v1/internal/crud/render/areas", headers=render).json() == {
        "areas": ["blob-index"]
    }
    printing = {"authorization": "Bearer sbsvc_print"}
    assert client.get("/api/v1/internal/crud/print/areas", headers=printing).json() == {"areas": []}
    assert client.get("/api/v1/internal/crud/render/areas", headers=printing).status_code == 403


async def test_a_taken_port_fails_the_start() -> None:
    with socket.socket() as taken:
        taken.bind(("0.0.0.0", 0))
        taken.listen()
        app = FastAPI()
        with pytest.raises(CrudListenerError):
            await listener.start(app, taken.getsockname()[1])
        assert crud_listener_port(app) is None


@pytest.mark.requires_postgres
def test_the_crud_port_serves_only_its_routes(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"crud_port": 0}))
    with TestClient(app) as main_port:
        port = crud_listener_port(app)
        assert port is not None and port > 0
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as crud:
            models = crud.get("/api/v1/models")
            assert models.status_code == 404
            assert models.text == "Not Found"
            assert "X-ScadBuddy-Crud" not in models.headers
            assert crud.get("/healthz").status_code == 200
            for caller in ("projects", "render", "print"):
                areas = crud.get(f"/api/v1/internal/crud/{caller}/areas")
                assert areas.status_code == 401, caller
                assert areas.headers["X-ScadBuddy-Crud"] == "1"
            # The exchange is there: a request with no proof is refused by the route.
            assert crud.post("/api/v1/auth/service-token").status_code in (400, 401, 422)
        # The main port still serves the rest, and the CRUD routes too.
        assert main_port.get("/api/v1/models").status_code == 200
        assert main_port.get("/api/v1/internal/crud/render/areas").status_code == 401
    assert crud_listener_port(app) is None
