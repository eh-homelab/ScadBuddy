"""#7154: a printer's K-profiles through Bambuddy, from ScadBuddy (plan
``docs/superpowers/plans/2026-11-23-printer-kprofiles.md``). The routes take SB's registry
printer, the writes are operations on the ``bambuddy`` queue, and Bambuddy is respx only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.models import BambuddyView, PrinterCreate
from scadbuddy.printers.store import PrinterStore
from tests.api.test_send import API, configure
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000071"
ROW: dict[str, Any] = {
    "slot_id": 0,
    "extruder_id": 1,
    "nozzle_id": "HH00-0.4",
    "nozzle_diameter": "0.4",
    "filament_id": "GFA00",
    "name": "High Flow_Bambu PLA Basic",
    "k_value": "0.02",
}
SENT = {"success": True, "confirmed": True, "message": "K-profile added successfully"}


def store(client: TestClient) -> PrinterStore:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(PRINTERS).store


def linked(client: TestClient, bambuddy_id: int = 3) -> str:
    configure(client)
    return (
        store(client)
        .create_linked(serial=SERIAL, name="H2C", model="H2C", bambuddy_printer_id=bambuddy_id)
        .id
    )


def base(printer_id: str) -> str:
    return f"/api/v1/printers/{printer_id}/kprofiles"


@respx.mock
def test_the_list_reads_the_linked_bambuddy_printer(client: TestClient) -> None:
    pid = linked(client, 3)
    route = respx.get(f"{API}/printers/3/kprofiles/").mock(
        return_value=httpx.Response(
            200,
            json={
                "profiles": [{**ROW, "slot_id": 2, "k_value": "0.020000", "setting_id": "PF1"}],
                "nozzle_diameter": "0.6",
            },
        )
    )

    response = client.get(base(pid), params={"nozzle_diameter": "0.6"})

    assert response.status_code == 200, response.text
    assert route.calls.last.request.url.params["nozzle_diameter"] == "0.6"
    body = response.json()
    assert body["nozzle_diameter"] == "0.6"
    assert body["profiles"][0]["setting_id"] == "PF1"
    assert body["profiles"][0]["tray_id"] == -1


@respx.mock
def test_notes_are_read_from_bambuddy(client: TestClient) -> None:
    pid = linked(client)
    respx.get(f"{API}/printers/3/kprofiles/notes").mock(
        return_value=httpx.Response(200, json={"notes": {"PF1": "dry first"}})
    )
    response = client.get(f"{base(pid)}/notes")
    assert response.status_code == 200, response.text
    assert response.json() == {"notes": {"PF1": "dry first"}}


def test_an_unknown_printer_is_a_404(client: TestClient) -> None:
    configure(client)
    missing = "0b7a3a52-0000-4000-8000-000000000000"
    assert client.get(base(missing)).status_code == 404
    response = client.post(base(missing), json=ROW, headers=press())
    assert response.status_code == 404, response.text


def test_an_unlinked_printer_is_a_409(client: TestClient) -> None:
    configure(client)
    pid = store(client).create(PrinterCreate(serial=SERIAL, name="Bare")).id
    response = client.get(base(pid))
    assert response.status_code == 409, response.text
    assert "not linked to a Bambuddy printer" in response.json()["detail"]
    write = client.post(base(pid), json=ROW, headers=press())
    assert write.status_code == 409, write.text


def test_a_virtual_printer_is_a_409(client: TestClient) -> None:
    configure(client)
    row, _, _ = store(client).upsert_virtual(
        1,
        serial=SERIAL,
        name="H2C Virtual",
        model="H2C",
        mode="queue",
        forwards_to=None,
        view=BambuddyView(seen_at=datetime.now(UTC), name="H2C Virtual"),
    )
    response = client.get(base(row.id))
    assert response.status_code == 409, response.text
    assert "virtual printer" in response.json()["detail"]


@respx.mock
def test_set_sends_the_profile_with_six_places(client: TestClient) -> None:
    pid = linked(client, 3)
    route = respx.post(f"{API}/printers/3/kprofiles/").mock(
        return_value=httpx.Response(200, json=SENT)
    )

    response = client.post(base(pid), json=ROW, headers=press())

    assert response.status_code == 200, response.text
    assert response.json() == SENT
    sent = json.loads(route.calls.last.request.content)
    assert sent == {**ROW, "k_value": "0.020000", "setting_id": None}


def test_a_write_needs_an_idempotency_key(client: TestClient) -> None:
    pid = linked(client)
    assert client.post(base(pid), json=ROW).status_code == 428


@pytest.mark.parametrize(
    "field",
    [
        {"k_value": "-0.1"},
        {"k_value": "10"},
        {"k_value": "0.0200001"},
        {"k_value": "abc"},
        {"nozzle_diameter": "0.5"},
        {"nozzle_id": "HH00"},
        {"filament_id": "GF A00"},
        {"name": ""},
        {"name": "a\nb"},
        {"extruder_id": 2},
        {"slot_id": -1},
        {"n_coef": "0.1"},
    ],
)
def test_a_bad_profile_is_refused_before_bambuddy(
    client: TestClient, field: dict[str, Any]
) -> None:
    pid = linked(client)
    response = client.post(base(pid), json={**ROW, **field}, headers=press())
    assert response.status_code == 422, response.text


@respx.mock
def test_the_batch_sends_every_row(client: TestClient) -> None:
    pid = linked(client, 3)
    route = respx.post(f"{API}/printers/3/kprofiles/batch").mock(
        return_value=httpx.Response(
            200, json={"success": True, "confirmed": False, "message": "Added 2 K-profiles"}
        )
    )
    rows = [{**ROW, "extruder_id": 0}, ROW]

    response = client.post(f"{base(pid)}/batch", json={"profiles": rows}, headers=press())

    assert response.status_code == 200, response.text
    assert response.json()["confirmed"] is False
    sent = json.loads(route.calls.last.request.content)
    assert [row["extruder_id"] for row in sent] == [0, 1]
    assert all(row["k_value"] == "0.020000" for row in sent)


@pytest.mark.parametrize(
    "profiles",
    [[], [ROW] * 17, [ROW, {**ROW, "nozzle_diameter": "0.6", "nozzle_id": "HH00-0.6"}]],
)
def test_a_batch_is_one_to_sixteen_rows_of_one_nozzle(
    client: TestClient, profiles: list[dict[str, Any]]
) -> None:
    pid = linked(client)
    response = client.post(f"{base(pid)}/batch", json={"profiles": profiles}, headers=press())
    assert response.status_code == 422, response.text


@respx.mock
def test_delete_sends_bambuddys_delete_body(client: TestClient) -> None:
    pid = linked(client, 3)
    route = respx.delete(f"{API}/printers/3/kprofiles/").mock(
        return_value=httpx.Response(
            200, json={"success": True, "confirmed": True, "message": "K-profile deleted"}
        )
    )
    body = {
        "slot_id": 4,
        "extruder_id": 0,
        "nozzle_id": "HS00-0.4",
        "nozzle_diameter": "0.4",
        "filament_id": "GFA00",
        "setting_id": "PF1",
    }

    response = client.post(f"{base(pid)}/delete", json=body, headers=press())

    assert response.status_code == 200, response.text
    assert json.loads(route.calls.last.request.content) == body


@respx.mock
def test_a_write_bambuddys_auth_refuses_names_the_admin_only_scope(client: TestClient) -> None:
    pid = linked(client, 3)
    respx.post(f"{API}/printers/3/kprofiles/").mock(
        return_value=httpx.Response(403, json={"detail": "Not enough permissions"})
    )
    response = client.post(base(pid), json=ROW, headers=press())
    assert response.status_code == 409, response.text
    problem = response.json()
    assert problem["required_scope"] == "K-profiles (admin only)"
    assert "auth is on" in problem["detail"]


@respx.mock
def test_a_set_is_sent_once_even_when_bambuddy_fails(client: TestClient) -> None:
    """R-7154-5: Bambuddy dedupes no add, so a 5xx is never sent again."""
    pid = linked(client, 3)
    route = respx.post(f"{API}/printers/3/kprofiles/").mock(
        return_value=httpx.Response(500, json={"detail": "Printer rejected the K-profile: x"})
    )
    response = client.post(base(pid), json=ROW, headers=press())
    assert response.status_code >= 500, response.text
    assert route.call_count == 1


@respx.mock
def test_a_note_is_put_and_deleted_by_its_key(client: TestClient) -> None:
    pid = linked(client, 3)
    put = respx.put(f"{API}/printers/3/kprofiles/notes").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "Note saved"})
    )
    deleted = respx.delete(f"{API}/printers/3/kprofiles/notes/name_PLA%20Basic_GFA00").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "Note deleted"})
    )

    saved = client.put(
        f"{base(pid)}/notes",
        json={"setting_id": "name_PLA Basic_GFA00", "note": "dry first"},
        headers=press(),
    )
    gone = client.delete(f"{base(pid)}/notes/name_PLA%20Basic_GFA00", headers=press())

    assert saved.status_code == 200, saved.text
    assert json.loads(put.calls.last.request.content) == {
        "setting_id": "name_PLA Basic_GFA00",
        "note": "dry first",
    }
    assert gone.status_code == 200, gone.text
    assert deleted.called


@pytest.mark.parametrize(
    "body",
    [
        {"setting_id": "", "note": "x"},
        {"setting_id": "a/b", "note": "x"},
        {"setting_id": "PF1", "note": "x" * 2001},
    ],
)
def test_a_bad_note_is_refused(client: TestClient, body: dict[str, Any]) -> None:
    pid = linked(client)
    response = client.put(f"{base(pid)}/notes", json=body, headers=press())
    assert response.status_code == 422, response.text
