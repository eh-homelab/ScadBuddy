"""The agent's assistant-cost route (job cost spec 2026-11-05 §4.4, #5330): under
``/api/v1/internal/crud/agent/``, only for the ``agent`` service credential, it writes a
session's spend as one ``assistant`` entry per order, converted at ``pricing_usd_rate``."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.auth.component import AUTH
from scadbuddy.auth.service_credentials import InProcessProvider, ServiceCaller
from tests.api.test_orders_unfulfil import accepted_order

pytestmark = pytest.mark.requires_git

SESSION = "0b0e3c55-6f1e-4a39-9d0e-8a1c2b3d4e5f"


def token(client: TestClient, caller: ServiceCaller = "agent") -> dict[str, str]:
    app: Any = client.app
    credentials = app.state.scadbuddy.components.get(AUTH).credentials
    minted = InProcessProvider(caller, lambda: credentials).token()
    assert minted is not None
    return {"authorization": f"Bearer {minted}"}


def report(client: TestClient, order_id: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
    return client.post(
        f"/api/v1/internal/crud/agent/orders/{order_id}/assistant-cost", json=body, headers=headers
    )


def test_the_agent_reports_a_sessions_spend_once(client: TestClient) -> None:
    order_id, [item_id] = accepted_order(client)
    agent = token(client)
    body = {
        "session_id": SESSION,
        "principal": "person:owner",
        "order_item_id": item_id,
        "cost_usd": "0.5",
    }

    first = report(client, order_id, body, agent)
    again = report(client, order_id, {**body, "cost_usd": "9"}, agent)

    assert (first.status_code, again.status_code) == (201, 200), first.text
    assert first.headers["x-scadbuddy-crud"] == "1"
    entry = first.json()["entry"]
    assert (entry["kind"], entry["amount_minor"], entry["source"]) == (
        "assistant",
        50,
        f"ai_session:{SESSION}",
    )
    by = entry["created_by"]
    assert (by["kind"], by["principal"], by["session"]) == ("agent", "person:owner", SESSION)
    assert again.json() == {"entry": entry, "written": False}
    costs = client.get(f"/api/v1/orders/{order_id}/costs").json()
    (line,) = costs["items"]
    assert [e["amount_minor"] for e in line["cost_entries"]] == [50]


def test_a_spend_under_one_cent_writes_nothing(client: TestClient) -> None:
    order_id, _ = accepted_order(client)
    answer = report(client, order_id, {"session_id": SESSION, "cost_usd": "0.001"}, token(client))
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"entry": None, "written": False}


def test_only_the_agent_credential_reaches_it(client: TestClient) -> None:
    order_id, _ = accepted_order(client)
    body = {"session_id": SESSION, "cost_usd": "1"}
    # The browser's own session (the test client) and another service are refused.
    assert report(client, order_id, body, {}).status_code == 401
    assert report(client, order_id, body, token(client, "print")).status_code == 403
    assert client.get(f"/api/v1/orders/{order_id}/costs").json()["cost_entries"] == []


def test_a_bad_report_is_refused(client: TestClient) -> None:
    order_id, _ = accepted_order(client)
    agent = token(client)
    assert (
        report(client, order_id, {"session_id": "nope", "cost_usd": "1"}, agent).status_code == 422
    )
    assert (
        report(client, order_id, {"session_id": SESSION, "cost_usd": "-1"}, agent).status_code
        == 422
    )
    missing = report(client, "missing", {"session_id": SESSION, "cost_usd": "1"}, agent)
    assert missing.status_code == 404
