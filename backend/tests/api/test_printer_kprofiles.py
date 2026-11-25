"""#7276 Task 7: a printer's K-profiles over ScadBuddy's own LAN link, notes in ScadBuddy.
The printer is scripted by replacing `LanPrinter.request` (P8) under the app's in-process
print worker; Bambuddy is never called (`forbid_bambuddy`)."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.api import printer_kprofiles as kprofile_routes
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.printers.component import PRINTERS, Printers
from scadbuddy.printers.kprofiles import KProfileSet, set_rows
from scadbuddy.printers.lan_commands import extrusion_cali_set
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.workflows import lan_command as lan_command_module
from tests.api.test_send import BASE, configure
from tests.printers.conftest import KEK
from tests.support.lan_script import Scripted, script_printer
from tests.support.no_bambuddy import forbid_bambuddy
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000071"
ROW_04: dict[str, Any] = {
    "cali_idx": 3,
    "extruder_id": 0,
    "nozzle_id": "HS00-0.4",
    "filament_id": "GFA00",
    "name": "PLA Basic",
    "k_value": "0.020000",
    "n_coef": "1.400000",
    "setting_id": "PF1",
}
ROW_06: dict[str, Any] = {**ROW_04, "cali_idx": 5, "nozzle_diameter": "0.6"}
SET: dict[str, Any] = {
    "slot_id": 0,
    "extruder_id": 1,
    "nozzle_id": "HH00-0.4",
    "nozzle_diameter": "0.4",
    "filament_id": "GFA00",
    "name": "High Flow_Bambu PLA Basic",
    "k_value": "0.02",
}
DELETE: dict[str, Any] = {
    "slot_id": 4,
    "extruder_id": 0,
    "nozzle_id": "HS00-0.4",
    "nozzle_diameter": "0.4",
    "filament_id": "GFA00",
    "setting_id": "PF1",
}
ONE_NOZZLE = {"gcode_state": "IDLE", "nozzle_type": "HS", "nozzle_diameter": "0.4"}
TWO_NOZZLES = {
    "gcode_state": "IDLE",
    "device": {
        "nozzle": {
            "info": [
                {"id": 0, "type": "HS", "diameter": "0.4"},
                {"id": 1, "type": "HS", "diameter": "0.4"},
            ]
        }
    },
}


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> Scripted:
    return script_printer(monkeypatch)


def printers_of(client: TestClient) -> Printers:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(PRINTERS)


def lan_printer(
    client: TestClient, report: dict[str, Any] | None = None, *, age: timedelta | None = None
) -> str:
    printers = printers_of(client)
    printers.store.kek = KEK
    row = printers.store.create(PrinterCreate(serial=SERIAL, name="H2C"))
    printers.store.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    printers.reports.put(row.id, report or ONE_NOZZLE, datetime.now(UTC) - (age or timedelta()))
    return row.id


@pytest.fixture
def pid(client: TestClient) -> str:
    return lan_printer(client)


def base(printer_id: str) -> str:
    return f"/api/v1/printers/{printer_id}/kprofiles"


def sent(script: Scripted, command: str) -> list[dict[str, Any]]:
    return [p for p in script.published if p["command"] == command]


def test_the_list_is_the_printers_reply(client: TestClient, scripted: Scripted, pid: str) -> None:
    scripted.reply("extrusion_cali_get", nozzle_diameter="0.4", filaments=[ROW_04, ROW_06])
    response = client.get(base(pid), params={"nozzle_diameter": "0.4"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["nozzle_diameter"] == "0.4"
    assert [p["slot_id"] for p in body["profiles"]] == [3]
    assert body["profiles"][0]["setting_id"] == "PF1"
    assert sent(scripted, "extrusion_cali_get")[0]["nozzle_diameter"] == "0.4"


def test_a_list_with_no_answer_is_502(client: TestClient, scripted: Scripted, pid: str) -> None:
    scripted.silent("extrusion_cali_get")
    response = client.get(base(pid))
    assert response.status_code == 502, response.text
    assert response.json()["code"] == "printer_no_answer"


def test_a_set_sends_one_extrusion_cali_set(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    response = client.post(base(pid), json=SET, headers=press())
    assert response.status_code == 200, response.text
    assert response.json() == {"success": True, "confirmed": True, "message": ""}
    [command] = sent(scripted, "extrusion_cali_set")
    [entry] = command["filaments"]
    assert (entry["cali_idx"], entry["k_value"], entry["tray_id"], entry["ams_id"]) == (
        -1,
        "0.020000",
        0,
        0,
    )
    assert entry["n_coef"] == "0.000000"
    assert re.fullmatch(r"PF\d{17}", entry["setting_id"])
    assert command["nozzle_diameter"] == "0.4"


def test_an_edit_keeps_its_slot_and_setting_id(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    edit = {**SET, "slot_id": 5, "setting_id": "PF123"}
    assert client.post(base(pid), json=edit, headers=press()).status_code == 200
    [entry] = sent(scripted, "extrusion_cali_set")[0]["filaments"]
    assert (entry["cali_idx"], entry["setting_id"]) == (5, "PF123")


def test_set_rows_matches_the_payload(client: TestClient, scripted: Scripted, pid: str) -> None:
    body = {**SET, "slot_id": 2, "setting_id": "PF9"}
    client.post(base(pid), json=body, headers=press())
    rows = set_rows([KProfileSet.model_validate(body)])
    expected = extrusion_cali_set(rows, "0.4", "x")["print"]["filaments"]
    assert sent(scripted, "extrusion_cali_set")[0]["filaments"] == expected


def test_a_batch_sends_every_row_in_one_command(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    rows = [{**SET, "extruder_id": 0}, SET]
    response = client.post(f"{base(pid)}/batch", json={"profiles": rows}, headers=press())
    assert response.status_code == 200, response.text
    [command] = sent(scripted, "extrusion_cali_set")
    assert [e["extruder_id"] for e in command["filaments"]] == [0, 1]
    # Bambuddy's set_kprofiles_batch makes a new id per row (bambu_mqtt.py L7790-L7792 at
    # d3be761f), so the two extruders' rows of one new profile do not share one.
    ids = [e["setting_id"] for e in command["filaments"]]
    assert all(re.fullmatch(r"PF\d{17}", i) for i in ids) and ids[0] != ids[1]


def test_a_delete_on_one_nozzle_sends_the_full_form(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    response = client.post(f"{base(pid)}/delete", json=DELETE, headers=press())
    assert response.status_code == 200, response.text
    [command] = sent(scripted, "extrusion_cali_del")
    assert (command["cali_idx"], command["setting_id"]) == (4, "PF1")


def test_a_delete_on_two_nozzles_sends_the_extruder_form(
    client: TestClient, scripted: Scripted
) -> None:
    pid = lan_printer(client, TWO_NOZZLES)
    body = {
        "slot_id": 4,
        "extruder_id": 1,
        "nozzle_id": "HH00-0.4",
        "nozzle_diameter": "0.4",
        "filament_id": "GFA00",
        "setting_id": "PF1",
    }
    assert client.post(f"{base(pid)}/delete", json=body, headers=press()).status_code == 200
    [command] = sent(scripted, "extrusion_cali_del")
    assert "setting_id" not in command and command["extruder_id"] == 1


def test_a_resent_key_publishes_once(client: TestClient, scripted: Scripted, pid: str) -> None:
    for _ in range(2):
        response = client.post(base(pid), json=SET, headers={"Idempotency-Key": "k1"})
        assert response.status_code == 200, response.text
    assert len(sent(scripted, "extrusion_cali_set")) == 1


def test_a_write_needs_an_idempotency_key(client: TestClient, scripted: Scripted, pid: str) -> None:
    assert client.post(base(pid), json=SET).status_code == 428
    assert scripted.published == []


def test_an_unacknowledged_write_is_unconfirmed(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    scripted.silent("extrusion_cali_set")
    response = client.post(base(pid), json=SET, headers=press())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True and body["confirmed"] is False
    assert "did not acknowledge" in body["message"]


def test_a_refused_write_is_409_printer_refused(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    scripted.refuse("extrusion_cali_set", "invalid tray_id")
    response = client.post(base(pid), json=SET, headers=press())
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "printer_refused"


def test_a_stale_report_is_409_printer_offline(client: TestClient, scripted: Scripted) -> None:
    pid = lan_printer(client, age=timedelta(minutes=5))
    for response in (
        client.get(base(pid)),
        client.post(base(pid), json=SET, headers=press()),
        client.post(f"{base(pid)}/batch", json={"profiles": [SET]}, headers=press()),
        client.post(f"{base(pid)}/delete", json=DELETE, headers=press()),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "printer_offline"
    assert scripted.published == []


class Later(datetime):
    """Ten minutes on: the stored report is stale by then."""

    @classmethod
    def now(cls, tz: Any = None) -> Later:
        return cls.fromtimestamp((datetime.now(tz) + timedelta(minutes=10)).timestamp(), tz)


def test_a_resent_key_answers_after_the_printer_drops_off(
    client: TestClient, scripted: Scripted, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P38: a key whose command exists answers its result, whatever the report says now."""
    first = client.post(base(pid), json=SET, headers={"Idempotency-Key": "k1"})
    assert first.status_code == 200, first.text
    monkeypatch.setattr(kprofile_routes, "datetime", Later)
    again = client.post(base(pid), json=SET, headers={"Idempotency-Key": "k1"})
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert len(sent(scripted, "extrusion_cali_set")) == 1
    other = client.post(base(pid), json=SET, headers={"Idempotency-Key": "k2"})
    assert other.status_code == 409, other.text
    assert other.json()["code"] == "printer_offline"
    assert len(sent(scripted, "extrusion_cali_set")) == 1


def follow(client: TestClient, pid: str, pending: httpx.Response) -> httpx.Response:
    """The command read route's answer once the command is done."""
    assert pending.status_code == 202, pending.text
    url = f"/api/v1/printers/{pid}/commands/{pending.json()['workflow_id']}"
    for _ in range(100):
        response: httpx.Response = client.get(url)
        if response.status_code != 202:
            return response
        time.sleep(0.1)
    raise AssertionError(f"{url} is still running")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "", None),
        ("POST", "", SET),
        ("POST", "/batch", {"profiles": [SET]}),
        ("POST", "/delete", DELETE),
    ],
    ids=["list", "set", "batch", "delete"],
)
def test_a_followed_command_answers_as_its_route(
    client: TestClient,
    scripted: Scripted,
    pid: str,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """P27, P37: a 202 followed to its end answers the body the route's 200 would have."""
    rows = [{**ROW_04, "nozzle_diameter": "0.4"}, ROW_06]
    scripted.reply("extrusion_cali_get", nozzle_diameter="0.6", filaments=rows)
    params = {"nozzle_diameter": "0.6"} if method == "GET" else None

    def send(key: str) -> httpx.Response:
        headers = {"Idempotency-Key": key} if method == "POST" else None
        url = f"{base(pid)}{path}"
        response: httpx.Response = client.request(
            method, url, json=body, params=params, headers=headers
        )
        return response

    direct = send("direct")
    assert direct.status_code == 200, direct.text
    monkeypatch.setattr(lan_command_module, "LAN_ROUTE_WAIT_S", 0)
    pending = send("slow")
    monkeypatch.setattr(lan_command_module, "LAN_ROUTE_WAIT_S", 10.0)
    followed = follow(client, pid, pending)
    assert followed.status_code == 200, followed.text
    assert followed.json() == direct.json()
    if method == "GET":
        assert [p["slot_id"] for p in followed.json()["profiles"]] == [5]


def test_notes_live_in_scadbuddy(client: TestClient, scripted: Scripted, pid: str) -> None:
    key = "name_PLA Basic_GFA00"
    saved = client.put(f"{base(pid)}/notes", json={"setting_id": key, "note": "dry"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["success"] is True
    assert client.get(f"{base(pid)}/notes").json() == {"notes": {key: "dry"}}
    gone = client.delete(f"{base(pid)}/notes/name_PLA%20Basic_GFA00", headers=press())
    assert gone.status_code == 200, gone.text
    assert client.get(f"{base(pid)}/notes").json() == {"notes": {}}
    assert scripted.published == []


def test_a_blank_note_deletes_it(client: TestClient, pid: str) -> None:
    client.put(f"{base(pid)}/notes", json={"setting_id": "PF1", "note": "x"})
    client.put(f"{base(pid)}/notes", json={"setting_id": "PF1", "note": "  "})
    assert client.get(f"{base(pid)}/notes").json() == {"notes": {}}


@pytest.mark.parametrize(
    "body",
    [
        {"setting_id": "", "note": "x"},
        {"setting_id": "a/b", "note": "x"},
        {"setting_id": "k" * 257, "note": "x"},
        {"setting_id": "PF1", "note": "x" * 2001},
        {"setting_id": "PF1", "note": "a\x00b"},
        {"setting_id": "a\x00b", "note": "x"},
    ],
    ids=["empty-key", "slash", "long-key", "long-note", "nul-note", "nul-key"],
)
def test_a_bad_note_is_422_not_500(client: TestClient, pid: str, body: dict[str, Any]) -> None:
    response = client.put(f"{base(pid)}/notes", json=body)
    assert response.status_code == 422, response.text


def test_a_lone_surrogate_note_is_422(client: TestClient, pid: str) -> None:
    raw = b'{"setting_id": "PF1", "note": "a\\ud800b"}'
    response = client.put(
        f"{base(pid)}/notes", content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422, response.text


def test_a_note_key_with_a_slash_cannot_be_deleted(client: TestClient, pid: str) -> None:
    assert client.delete(f"{base(pid)}/notes/a%2Fb").status_code in (404, 422)
    long = "k" * 257
    assert client.delete(f"{base(pid)}/notes/{long}").status_code == 422


def test_notes_of_an_unknown_printer_are_404(client: TestClient) -> None:
    missing = "0b7a3a52-0000-4000-8000-000000000000"
    assert client.get(f"{base(missing)}/notes").status_code == 404
    put = client.put(f"{base(missing)}/notes", json={"setting_id": "PF1", "note": "x"})
    assert put.status_code == 404


def test_an_unknown_printer_is_a_404(client: TestClient, scripted: Scripted) -> None:
    missing = "0b7a3a52-0000-4000-8000-000000000000"
    assert client.get(base(missing)).status_code == 404
    assert client.post(base(missing), json=SET, headers=press()).status_code == 404


@pytest.fixture
def bb_only(client: TestClient) -> Iterator[str]:
    printers = printers_of(client)
    row = printers.store.create(PrinterCreate(serial=SERIAL, name="Bare"))
    printers.store.link_bambuddy(row.id, 3)
    yield row.id


def test_a_bambuddy_only_printer_is_not_lan_linked(
    client: TestClient, scripted: Scripted, bb_only: str
) -> None:
    for response in (
        client.get(base(bb_only)),
        client.post(base(bb_only), json=SET, headers=press()),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "not_lan_linked"
    assert scripted.published == []


def test_a_printer_linked_both_ways_never_calls_bambuddy(
    client: TestClient, scripted: Scripted, spans: InMemorySpanExporter
) -> None:
    """Bambuddy has a URL and the row its id, and still no route calls it."""
    configure(client)
    pid = lan_printer(client)
    printers_of(client).store.link_bambuddy(pid, 3)
    scripted.reply("extrusion_cali_get", nozzle_diameter="0.4", filaments=[ROW_04])
    with forbid_bambuddy(spans, BASE):
        answers = [
            client.get(base(pid)),
            client.post(base(pid), json=SET, headers=press()),
            client.post(f"{base(pid)}/batch", json={"profiles": [SET]}, headers=press()),
            client.post(f"{base(pid)}/delete", json=DELETE, headers=press()),
            client.put(f"{base(pid)}/notes", json={"setting_id": "k", "note": "n"}),
            client.get(f"{base(pid)}/notes"),
            client.delete(f"{base(pid)}/notes/k", headers=press()),
        ]
    for response in answers:
        assert response.status_code == 200, response.text
