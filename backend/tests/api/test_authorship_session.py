"""Authorship from the session (#4975 phase 5, spec 2026-11-01 §7.3, §8.3): a person's
session names the person, author headers are believed from the agent's credential and,
naming the run's starter only, the projects worker's, and what only a person does refuses
every service."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import SESSION_COOKIE
from scadbuddy.auth.models import principal_for
from scadbuddy.auth.service_credentials import LEGACY_CALLERS, ServiceCredentialStore, TokenReader
from scadbuddy.core.settings import Settings
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order

PUBLIC = "https://scadbuddy.test"
HEADER = {"X-ScadBuddy-Request": "1"}
ISSUER = "https://idp.test"
AUTHOR = {"X-ScadBuddy-Agent-Author": "token:abc"}
#: A flow host route (#5298): the projects worker's credential reaches it.
FLOW_HOST = "/api/v1/settings/print-options"


@pytest.fixture
def mode() -> str:
    return "observe"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path, mode: str) -> Settings:
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(os.urandom(32)).decode())
    return settings.model_copy(
        update={"secret_key_file": key, "public_url": PUBLIC, "auth_mode": mode}
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=PUBLIC, headers=HEADER) as test_client:
        yield test_client


def _count(app: FastAPI, route_cls: str, reason: str) -> float:
    registry = getattr(app.state, STATE_ATTR).metrics.registry
    value = registry.get_sample_value(
        "scadbuddy_auth_would_refuse_total", {"class": route_cls, "reason": reason}
    )
    return value or 0.0


def _sign_in(client: TestClient, app: FastAPI, subject: str = "alice") -> str:
    auth = getattr(app.state, STATE_ATTR).components.get(AUTH)
    token, _ = auth.sessions.create(issuer=ISSUER, subject=subject, kind="login")
    client.cookies.set(SESSION_COOKIE, token)
    return principal_for(ISSUER, subject)


def _bearer(app: FastAPI, settings: Settings, caller: Any) -> dict[str, str]:
    store = getattr(app.state, STATE_ATTR).settings_store
    if caller in LEGACY_CALLERS:
        token = TokenReader(caller, settings.database_url, store.kek).token()
    else:
        # No sealed credential (#5298): the caller exchanges its proof, as in prod.
        issued = ServiceCredentialStore(store.pool).issue(caller, "bootstrap", rate_limited=False)
        token = issued.token
    assert token is not None
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_a_person_s_order_names_them(client: TestClient, app: FastAPI) -> None:
    principal = _sign_in(client, app)
    order = create_order(client, create_customer(client)["id"])
    assert order["created_by"] == {
        "kind": "browser",
        "principal": principal,
        "session": None,
        "run_id": None,
    }


def test_signed_out_is_the_browser_user(client: TestClient) -> None:
    order = create_order(client, create_customer(client)["id"])
    assert order["created_by"] == {
        "kind": "browser",
        "principal": None,
        "session": None,
        "run_id": None,
    }


def test_a_person_s_author_header_is_counted(client: TestClient, app: FastAPI) -> None:
    _sign_in(client, app)
    assert client.get("/api/v1/models", headers=AUTHOR).status_code == 200
    assert _count(app, "api", "author_header") == 1
    client.get("/api/v1/models")
    assert _count(app, "api", "author_header") == 1


def test_only_the_agent_names_anyone(client: TestClient, app: FastAPI, settings: Settings) -> None:
    client.get("/api/v1/models", headers={**AUTHOR, **_bearer(app, settings, "agent")})
    assert _count(app, "api", "author_header") == 0
    # From nobody, or the print worker, the header is what enforce refuses.
    client.get("/api/v1/models", headers=AUTHOR)
    assert _count(app, "api", "author_header") == 1
    client.get("/api/v1/models", headers={**AUTHOR, **_bearer(app, settings, "print")})
    assert _count(app, "api", "author_header") == 2


def test_the_print_and_projects_workers_have_no_api_routes(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    client.get("/api/v1/models", headers=_bearer(app, settings, "print"))
    assert _count(app, "api", "out_of_scope") == 1
    client.get("/api/v1/models", headers=_bearer(app, settings, "projects"))
    assert _count(app, "api", "out_of_scope") == 2
    # #5298: the projects credential's `flow.host` reaches the flow host routes only.
    client.get(FLOW_HOST, headers=_bearer(app, settings, "projects"))
    assert _count(app, "flow.host", "out_of_scope") == 0
    client.get(FLOW_HOST, headers=_bearer(app, settings, "print"))
    assert _count(app, "flow.host", "out_of_scope") == 1


def test_the_projects_worker_needs_the_run_s_token(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    client.get(FLOW_HOST, headers={**AUTHOR, **_bearer(app, settings, "projects")})
    assert _count(app, "flow.host", "wrong_author") == 1
    forged = {"X-ScadBuddy-Run-Token": "some-run.AAAA"}
    client.get(FLOW_HOST, headers={**AUTHOR, **forged, **_bearer(app, settings, "projects")})
    assert _count(app, "flow.host", "wrong_author") == 2


@pytest.mark.parametrize("caller", ["agent", "projects"])
def test_a_service_never_answers_for_a_person(
    client: TestClient, app: FastAPI, settings: Settings, caller: str
) -> None:
    """`_browser_only`: a service's credential is refused even with no author header."""
    response = client.post(
        "/api/v1/workflow-runs/some-run/answer",
        json={"call_id": "c", "answer": "yes"},
        headers=_bearer(app, settings, caller),
    )
    assert response.status_code == 403
    assert response.json()["type"] == "https://scadbuddy.dev/problems/flow-entry-browser-only"


def test_a_person_still_answers(client: TestClient, app: FastAPI) -> None:
    _sign_in(client, app)
    response = client.post(
        "/api/v1/workflow-runs/some-run/answer", json={"call_id": "c", "answer": "yes"}
    )
    assert response.status_code != 403
