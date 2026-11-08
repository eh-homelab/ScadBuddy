"""The storefront's CRUD routes (#5282, plan Task 2): under ``/api/v1/internal/crud/storefront``,
guarded by the ``storefront`` credential, hidden from the schema, and taking the visitor
id and the client address from the edge's headers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import crud_areas, crud_storefront
from scadbuddy.api.crud_storefront import store_client, visitor_id
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.models import Service
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.core.settings import Settings
from scadbuddy.crud.guard import crud_router
from scadbuddy.crud.storefront import EDGE_AREAS
from scadbuddy.main import create_app
from scadbuddy.storefront.wire import CLIENT_HEADER, VISITOR_HEADER
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS

CREDENTIALS = {
    "sbsvc_render": Service("render", frozenset({"crud.render"})),
    "sbsvc_storefront": Service("storefront", frozenset({"crud.storefront"})),
}
VISITOR = "A" * 21 + "_" + "b" * 20 + "-"


class _Credentials:
    def resolve(self, token: str) -> Service | None:
        return CREDENTIALS.get(token)


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture
def render_token() -> str:
    return "sbsvc_render"


@pytest.fixture
def storefront_token() -> str:
    return "sbsvc_storefront"


class _Noted:
    def note(self, *args: object) -> None: ...


def make_client(mode: str = "off") -> TestClient:
    """The CRUD routers behind the real `auth_check` and gate, as `main.py` mounts them,
    with a probe route that takes the visitor and client headers (`GET /draft` is Task 8's)
    and a back-office route (class ``api``)."""
    app = FastAPI(dependencies=[Depends(auth_check)])
    auth = SimpleNamespace(would_refuse=_Noted())
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode=mode),
        components=SimpleNamespace(get=lambda _component: auth),
    )
    probe = crud_router("storefront")
    back_office = APIRouter()

    @back_office.get("/orders")
    def orders() -> list[str]:
        return []

    @probe.get("/draft")
    def draft(
        visitor: Annotated[str, Depends(visitor_id)], client: Annotated[str, Depends(store_client)]
    ) -> dict[str, str]:
        return {"visitor": visitor, "client": client}

    app.include_router(crud_areas.router, prefix="/api/v1")
    app.include_router(crud_storefront.router, prefix="/api/v1")
    app.include_router(probe, prefix="/api/v1")
    app.include_router(back_office, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: None, credentials=lambda: _Credentials())  # type: ignore[arg-type,return-value]
    return TestClient(app)


@pytest.fixture
def client() -> TestClient:
    return make_client()


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
def test_the_storefront_guard_holds_in_every_mode(mode: str, render_token: str) -> None:
    client = make_client(mode)
    url = "/api/v1/internal/crud/storefront/areas"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=bearer(render_token)).status_code == 403
    assert client.get(url, headers=bearer("sbsvc_storefront")).status_code == 200


def test_the_storefront_credential_reaches_no_other_route(storefront_token: str) -> None:
    client = make_client("enforce")
    other = client.get("/api/v1/internal/crud/render/areas", headers=bearer(storefront_token))
    assert other.status_code == 403
    assert other.json()["reason"] == "wrong_caller"
    back_office = client.get("/api/v1/orders", headers=bearer(storefront_token))
    assert back_office.status_code == 403
    assert back_office.json()["reason"] == "out_of_scope"


def test_a_storefront_route_needs_the_storefront_credential(
    client: TestClient, render_token: str, storefront_token: str
) -> None:
    url = "/api/v1/internal/crud/storefront/areas"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=bearer(render_token)).status_code == 403
    r = client.get(url, headers=bearer(storefront_token))
    assert r.status_code == 200 and r.headers["X-ScadBuddy-Crud"] == "1"
    assert r.json() == {"areas": ["listings"]}


def test_the_storefront_routes_are_not_in_the_schema() -> None:
    app = create_app(
        Settings(
            frontend_dir=Path("/nonexistent"),
            database_url=UNUSED_DATABASE_URL,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
        )
    )
    assert not [p for p in app.openapi()["paths"] if "/internal/crud/storefront" in p]
    assert app.url_path_for("crud_storefront_areas") == "/api/v1/internal/crud/storefront/areas"


@pytest.mark.parametrize("token", ["", "short", "x" * 42 + "!", "x" * 44])
def test_a_malformed_visitor_token_is_400(
    client: TestClient, storefront_token: str, token: str
) -> None:
    r = client.get(
        "/api/v1/internal/crud/storefront/draft",
        headers={**bearer(storefront_token), VISITOR_HEADER: token, CLIENT_HEADER: "203.0.113.1"},
    )
    assert r.status_code == 400
    assert r.json()["type"].endswith("/storefront/visitor_id")
    assert r.headers["X-ScadBuddy-Crud"] == "1"


def test_a_missing_visitor_header_is_400(client: TestClient, storefront_token: str) -> None:
    r = client.get(
        "/api/v1/internal/crud/storefront/draft",
        headers={**bearer(storefront_token), CLIENT_HEADER: "203.0.113.1"},
    )
    assert r.status_code == 400
    assert r.json()["type"].endswith("/storefront/visitor_id")


@pytest.mark.parametrize("address", [None, "", "not-an-ip", "203.0.113.1, 198.51.100.2"])
def test_a_client_that_is_not_an_ip_address_is_400(
    client: TestClient, storefront_token: str, address: str | None
) -> None:
    headers = {**bearer(storefront_token), VISITOR_HEADER: VISITOR}
    if address is not None:
        headers[CLIENT_HEADER] = address
    r = client.get("/api/v1/internal/crud/storefront/draft", headers=headers)
    assert r.status_code == 400
    assert r.json()["type"].endswith("/storefront/store_client")


@pytest.mark.parametrize("address", ["203.0.113.1", "2001:db8::1"])
def test_a_well_formed_visitor_and_client_pass(
    client: TestClient, storefront_token: str, address: str
) -> None:
    r = client.get(
        "/api/v1/internal/crud/storefront/draft",
        headers={**bearer(storefront_token), VISITOR_HEADER: VISITOR, CLIENT_HEADER: address},
    )
    assert r.status_code == 200
    assert r.json() == {"visitor": VISITOR, "client": address}


def test_the_other_callers_keep_their_paths(client: TestClient, render_token: str) -> None:
    assert (
        client.get("/api/v1/internal/crud/render/areas", headers=bearer(render_token)).status_code
        == 200
    )
    # The storefront has no path of its own outside `/internal/crud/`.
    storefront = bearer("sbsvc_storefront")
    assert client.get("/api/v1/internal/storefront/areas", headers=storefront).status_code == 404


def test_the_edge_waits_for_every_area_it_calls() -> None:
    """The edge serves only once the API lists each area it calls, and the API lists
    only areas it serves whole."""
    assert {"listings"} == EDGE_AREAS
    assert set(crud_areas.CRUD_AREAS["storefront"]) == EDGE_AREAS


@pytest.mark.parametrize("path", ["/listings", "/listings/00000000-0000-0000-0000-000000000000"])
def test_the_listing_routes_need_the_storefront_credential(
    client: TestClient, render_token: str, path: str
) -> None:
    url = f"/api/v1/internal/crud/storefront{path}"
    headers = {VISITOR_HEADER: VISITOR, CLIENT_HEADER: "203.0.113.1"}
    assert client.get(url, headers=headers).status_code == 401
    refused = client.get(url, headers={**headers, **bearer(render_token)})
    assert refused.status_code == 403 and refused.json()["reason"] == "wrong_caller"
