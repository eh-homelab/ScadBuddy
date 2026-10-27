"""Bambuddy's virtual printers from ScadBuddy (#4747 item 4; plan
2026-10-26-bambuddy-printer-sync Ruling 8): reads, the three writes as operations on the
``bambuddy`` queue, the target as a registry printer, and the access code never in the
clear outside the run."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.virtual_printers import (
    access_code_mac,
    access_code_request,
    open_access_code,
    seal_access_code,
    seal_context,
    virtual_printer_kinds,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.core.secrets import SealError, kek_from_base64
from scadbuddy.printers.component import PRINTERS, Printers
from tests.api.test_send import API, configure
from tests.bambuddy.conftest import recording
from tests.printers.conftest import KEK
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000001"
CODE = "vpc0de12"
#: The KEK after a rotation; `KEK` is then the previous one.
ROTATED = kek_from_base64(base64.b64encode(b"\x11" * 32).decode())


def registry(client: TestClient) -> Printers:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(PRINTERS)


def vp(vp_id: int = 1, target: int | None = 1, **fields: Any) -> dict[str, Any]:
    return {
        **recording("virtual-printers.json")["printers"][0],
        "id": vp_id,
        "target_printer_id": target,
        **fields,
    }


def bambuddy(vps: list[dict[str, Any]], *, auth: bool = False) -> None:
    respx.get(f"{API}/virtual-printers").mock(
        return_value=httpx.Response(200, json={"printers": vps, "models": {"O1C": "H2C"}})
    )
    respx.get(f"{API}/auth/status").mock(
        return_value=httpx.Response(200, json={"auth_enabled": auth, "requires_setup": False})
    )
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    **recording("printers.json")[0],
                    "id": 1,
                    "name": "H2C",
                    "serial_number": SERIAL,
                    "ip_address": None,
                }
            ],
        )
    )


def linked_printer(client: TestClient) -> str:
    printers = registry(client)
    row = printers.store.create_linked(
        serial=SERIAL, name="H2C", model="H2C", bambuddy_printer_id=1
    )
    return row.id


def stored_requests(client: TestClient) -> str:
    with registry(client).store.pool.connection() as conn:
        rows = conn.execute("SELECT request::text AS r FROM operations").fetchall()
    return " ".join(row["r"] for row in rows)


def test_two_seals_of_one_code_differ_and_open_only_in_their_context() -> None:
    context = seal_context(vp_id=3, idempotency_key="k1")
    first = seal_access_code(KEK, CODE, context)
    second = seal_access_code(KEK, CODE, context)
    assert CODE not in first and first != second  # a random IV: no equality oracle
    assert open_access_code(KEK, first, context) == CODE
    assert open_access_code(KEK, second, context) == CODE
    with pytest.raises(SealError):
        open_access_code(KEK, first, seal_context(vp_id=4, idempotency_key="k1"))


@respx.mock
def test_a_code_sealed_before_a_kek_rotation_opens_under_the_previous_key(
    client: TestClient,
) -> None:
    """#4888 finding 1: the route sealed the code under the KEK of its day; the run
    starts after a rotation, with that key as `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE`. The
    write goes through with the code."""
    configure(client)
    bambuddy([])
    created = respx.post(f"{API}/virtual-printers").mock(
        return_value=httpx.Response(200, json=vp(1, None))
    )
    request = {
        "fields": {"name": "X"},
        **access_code_request(KEK, CODE, vp_id=None, idempotency_key="k-rotate"),
    }
    store = registry(client).store
    store.kek, store.previous_kek = ROTATED, KEK
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    kinds = {
        kind.name: kind
        for kind in virtual_printer_kinds(settings_store=state.settings_store, printers=store)
    }
    asyncio.run(kinds["bambuddy_vp_create"].run(request, {}))
    assert json.loads(created.calls.last.request.read())["access_code"] == CODE

    store.previous_kek = None  # without the previous key, the same seal does not open
    with pytest.raises(ApiError) as refused:
        asyncio.run(kinds["bambuddy_vp_create"].run(request, {}))
    assert refused.value.status == 503


def test_open_access_code_tries_the_previous_key_after_the_current_one() -> None:
    context = seal_context(vp_id=3, idempotency_key="k1")
    old = seal_access_code(KEK, CODE, context)
    new = seal_access_code(ROTATED, CODE, context)
    assert open_access_code(ROTATED, old, context, previous=KEK) == CODE
    assert open_access_code(ROTATED, new, context, previous=KEK) == CODE
    with pytest.raises(SealError):
        open_access_code(ROTATED, old, context)


@respx.mock
def test_a_target_that_is_not_a_uuid_is_a_422(client: TestClient) -> None:
    """#4888 finding 2: the bound is in the model (and so the spec), not a 404 later."""
    configure(client)
    registry(client).store.kek = KEK
    bambuddy([])
    created = respx.post(f"{API}/virtual-printers")
    response = client.post(
        "/api/v1/printers/virtual", json={"name": "X", "target": "not-a-uuid"}, headers=press()
    )
    assert response.status_code == 422, response.text
    changed = client.put(
        "/api/v1/printers/virtual/1", json={"target": "not-a-uuid"}, headers=press()
    )
    assert changed.status_code == 422, changed.text
    assert not created.called
    spec = client.get("/openapi.json").json()["components"]["schemas"]
    for model in ("VirtualPrinterCreate", "VirtualPrinterUpdate"):
        options = spec[model]["properties"]["target"]["anyOf"]
        assert {"type": "string", "format": "uuid"} in options


def test_the_mac_matches_only_a_resend_of_the_same_request() -> None:
    context = seal_context(vp_id=None, idempotency_key="k1")
    assert context.endswith(":new:k1")
    mac = access_code_mac(KEK, CODE, context, "k1")
    assert CODE not in mac and mac == access_code_mac(KEK, CODE, context, "k1")
    other = seal_context(vp_id=None, idempotency_key="k2")
    assert mac != access_code_mac(KEK, CODE, other, "k2")  # equal codes, other requests


@respx.mock
def test_lists_virtual_printers_with_their_registry_rows(client: TestClient) -> None:
    configure(client)
    target = linked_printer(client)
    bambuddy([vp(1, 1), vp(2, None)], auth=True)

    body = client.get("/api/v1/printers/virtual").json()

    assert body["auth_enabled"] is True and body["models"] == {"O1C": "H2C"}
    first, second = body["printers"]
    assert (first["target"], second["target"]) == (target, None)
    assert first["mode"] == "queue" and "access_code" not in first


@respx.mock
def test_a_failed_virtual_printer_read_is_mapped_and_leaves_no_read_behind(
    client: TestClient,
) -> None:
    """#4890: the list read failing is the route's answer, and the auth read is not left
    running in a client that is closing: it is read after the list, never beside it."""
    configure(client)
    respx.get(f"{API}/virtual-printers").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )
    auth = respx.get(f"{API}/auth/status").mock(
        return_value=httpx.Response(200, json={"auth_enabled": False, "requires_setup": False})
    )
    response = client.get("/api/v1/printers/virtual")
    assert response.status_code == 502, response.text
    assert response.json()["status"] == 502
    assert not auth.called


@respx.mock
def test_diagnostic_passes_bambuddys_checks_through(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/virtual-printers/1/diagnostic").mock(
        return_value=httpx.Response(
            200,
            json={
                "vp_id": 1,
                "vp_name": "H2C Virtual",
                "mode": "queue",
                "overall": "warnings",
                "checks": [{"id": "port_mqtt", "status": "pass", "params": {"port": 8883}}],
            },
        )
    )
    body = client.get("/api/v1/printers/virtual/1/diagnostic").json()
    assert body["overall"] == "warnings" and body["checks"][0]["id"] == "port_mqtt"


@respx.mock
def test_create_maps_the_target_and_never_records_the_code(client: TestClient) -> None:
    configure(client)
    registry(client).store.kek = KEK
    bambuddy([])
    created = respx.post(f"{API}/virtual-printers").mock(
        return_value=httpx.Response(200, json=vp(4, None, name="Any H2C"))
    )

    response = client.post(
        "/api/v1/printers/virtual",
        json={"name": "Any H2C", "mode": "queue", "access_code": CODE},
        headers=press(),
    )

    assert response.status_code == 200, response.text
    assert CODE not in response.text
    sent = json.loads(created.calls.last.request.read())
    assert sent["access_code"] == CODE and sent["target_printer_id"] is None
    assert sent["mode"] == "queue" and "target" not in sent
    assert CODE not in stored_requests(client)


@respx.mock
def test_a_target_is_a_registry_printer_and_takes_no_code(client: TestClient) -> None:
    configure(client)
    registry(client).store.kek = KEK
    target = linked_printer(client)
    bambuddy([])
    created = respx.post(f"{API}/virtual-printers").mock(
        return_value=httpx.Response(200, json=vp(5, 1))
    )

    ok = client.post(
        "/api/v1/printers/virtual",
        json={"name": "Queue", "mode": "queue", "target": target},
        headers=press(),
    )
    assert ok.status_code == 200, ok.text
    assert json.loads(created.calls.last.request.read())["target_printer_id"] == 1

    refused = client.post(
        "/api/v1/printers/virtual",
        json={"name": "Queue", "target": target, "access_code": CODE},
        headers=press(),
    )
    assert refused.status_code == 422 and "target's access code" in refused.json()["detail"]


def test_a_code_is_eight_characters(client: TestClient) -> None:
    response = client.post(
        "/api/v1/printers/virtual", json={"name": "X", "access_code": "short"}, headers=press()
    )
    assert response.status_code == 422
    assert "short" not in response.text


@respx.mock
def test_a_refused_write_names_bambuddys_admin_only_scope(client: TestClient) -> None:
    configure(client)
    bambuddy([], auth=True)
    respx.put(f"{API}/virtual-printers/1").mock(
        return_value=httpx.Response(403, json={"detail": "Not enough permissions"})
    )
    response = client.put("/api/v1/printers/virtual/1", json={"enabled": False}, headers=press())
    assert response.status_code == 409, response.text
    problem = response.json()
    assert problem["required_scope"] == "Settings (admin only)"
    assert "auth is on" in problem["detail"]


@respx.mock
def test_bambuddys_own_refusal_is_a_422_with_its_reason(client: TestClient) -> None:
    configure(client)
    bambuddy([])
    respx.put(f"{API}/virtual-printers/1").mock(
        return_value=httpx.Response(400, json={"detail": "Bind IP 0.0.0.0 is already in use"})
    )
    response = client.put(
        "/api/v1/printers/virtual/1", json={"enabled": True, "bind_ip": "0.0.0.0"}, headers=press()
    )
    assert response.status_code == 422, response.text
    assert "already in use" in response.json()["detail"]


@respx.mock
def test_update_sends_only_what_changed_and_the_registry_follows(client: TestClient) -> None:
    configure(client)
    target = linked_printer(client)
    bambuddy([vp(1, 1, mode="proxy")])
    updated = respx.put(f"{API}/virtual-printers/1").mock(
        return_value=httpx.Response(200, json=vp(1, 1, mode="proxy"))
    )

    response = client.put("/api/v1/printers/virtual/1", json={"mode": "proxy"}, headers=press())

    assert response.status_code == 200, response.text
    assert json.loads(updated.calls.last.request.read()) == {"mode": "proxy"}
    rows = registry(client).store.list()
    virtual = next(row for row in rows if row.bambuddy_vp_id == 1)
    assert virtual.vp_mode == "proxy" and virtual.forwards_to == target


@respx.mock
def test_delete_keeps_the_registry_row(client: TestClient) -> None:
    configure(client)
    bambuddy([vp(1, None)])
    client.post("/api/v1/printers/sync-bambuddy")
    respx.get(f"{API}/virtual-printers/1").mock(return_value=httpx.Response(200, json=vp(1, None)))
    gone = respx.delete(f"{API}/virtual-printers/1").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    bambuddy([])

    response = client.delete("/api/v1/printers/virtual/1", headers=press())

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": 1} and gone.called
    # #4889: the row stays, marked no longer listed by the write's own follow.
    (row,) = [row for row in registry(client).store.list() if row.bambuddy_vp_id == 1]
    assert row.bambuddy_active is False


@respx.mock
def test_a_resend_with_the_same_key_and_code_creates_once(client: TestClient) -> None:
    configure(client)
    registry(client).store.kek = KEK
    bambuddy([])
    created = respx.post(f"{API}/virtual-printers").mock(
        return_value=httpx.Response(200, json=vp(4, None, name="Any H2C"))
    )
    key = press()
    body = {"name": "Any H2C", "mode": "queue", "access_code": CODE}

    first = client.post("/api/v1/printers/virtual", json=body, headers=key)
    again = client.post("/api/v1/printers/virtual", json=body, headers=key)

    assert first.status_code == again.status_code == 200, again.text
    assert again.json() == first.json()
    assert created.call_count == 1
    with registry(client).store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id FROM operations WHERE kind = 'bambuddy_vp_create'"
        ).fetchall()
    assert len(rows) == 1


@respx.mock
def test_a_401_is_the_plain_scope_problem(client: TestClient) -> None:
    configure(client)
    bambuddy([])
    respx.put(f"{API}/virtual-printers/1").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid API key"})
    )
    response = client.put("/api/v1/printers/virtual/1", json={"enabled": False}, headers=press())
    assert response.status_code == 409, response.text
    assert "auth is on" not in response.json()["detail"]


@respx.mock
def test_a_code_for_a_vp_that_keeps_its_target_is_refused(client: TestClient) -> None:
    configure(client)
    registry(client).store.kek = KEK
    bambuddy([])
    respx.get(f"{API}/virtual-printers/1").mock(return_value=httpx.Response(200, json=vp(1, 1)))
    changed = respx.put(f"{API}/virtual-printers/1")
    response = client.put("/api/v1/printers/virtual/1", json={"access_code": CODE}, headers=press())
    assert response.status_code == 422, response.text
    assert "target's access code" in response.json()["detail"]
    assert not changed.called


@respx.mock
def test_a_repeat_answered_from_the_record_does_not_sync_again(client: TestClient) -> None:
    configure(client)
    bambuddy([vp(1, None)])
    listed = respx.routes[-1]  # GET /printers/, which every sync reads
    respx.put(f"{API}/virtual-printers/1").mock(
        return_value=httpx.Response(200, json=vp(1, None, enabled=False))
    )
    key = press()
    client.put("/api/v1/printers/virtual/1", json={"enabled": False}, headers=key)
    synced = listed.call_count
    assert synced >= 1
    again = client.put("/api/v1/printers/virtual/1", json={"enabled": False}, headers=key)
    assert again.status_code == 200, again.text
    assert listed.call_count == synced


@respx.mock
def test_bambuddys_validation_error_never_echoes_the_code(client: TestClient) -> None:
    """Review #4784: a FastAPI 422 quotes each field's input; the code must not come
    back in the answer or the operation's record."""
    configure(client)
    registry(client).store.kek = KEK
    bambuddy([])
    respx.post(f"{API}/virtual-printers").mock(
        return_value=httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "type": "string_too_long",
                        "loc": ["body", "access_code"],
                        "msg": "String should have at most 8 characters",
                        "input": CODE,
                    }
                ]
            },
        )
    )
    response = client.post(
        "/api/v1/printers/virtual", json={"name": "X", "access_code": CODE}, headers=press()
    )
    assert response.status_code == 422, response.text
    assert "body.access_code: String should have at most 8 characters" in response.text
    assert CODE not in response.text
    with registry(client).store.pool.connection() as conn:
        rows = conn.execute("SELECT coalesce(error::text, '') AS e FROM operations").fetchall()
    assert all(CODE not in row["e"] for row in rows)


@respx.mock
def test_an_update_that_drops_the_target_may_set_a_code(client: TestClient) -> None:
    """Review #4784: `target: null` with a new code is an untargeted VP's code, so it
    is sent to Bambuddy, and no read of the VP's current target refuses it."""
    configure(client)
    registry(client).store.kek = KEK
    bambuddy([])
    current = respx.get(f"{API}/virtual-printers/1").mock(
        return_value=httpx.Response(200, json=vp(1, 1))
    )
    changed = respx.put(f"{API}/virtual-printers/1").mock(
        return_value=httpx.Response(200, json=vp(1, None))
    )
    response = client.put(
        "/api/v1/printers/virtual/1",
        json={"target": None, "access_code": CODE},
        headers=press(),
    )
    assert response.status_code == 200, response.text
    assert CODE not in response.text
    sent = json.loads(changed.calls.last.request.read())
    assert sent == {"target_printer_id": None, "access_code": CODE}
    assert not current.called


@respx.mock
def test_a_refused_write_never_echoes_bambuddys_values(client: TestClient) -> None:
    """Review #4784: the 403's detail is scrubbed as a 400's is."""
    configure(client)
    registry(client).store.kek = KEK
    bambuddy([], auth=True)
    respx.post(f"{API}/virtual-printers").mock(
        return_value=httpx.Response(
            403, json={"detail": [{"loc": ["body", "access_code"], "msg": "no", "input": CODE}]}
        )
    )
    response = client.post(
        "/api/v1/printers/virtual", json={"name": "X", "access_code": CODE}, headers=press()
    )
    assert response.status_code == 409, response.text
    assert CODE not in response.text
