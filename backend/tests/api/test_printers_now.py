"""#977 — what each printer is doing now on /prints, whoever started it: every active
printer's live status from Bambuddy, a job ScadBuddy linked pointing at its print, and
a printer whose status cannot be read as a card saying so."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.models import PrinterStatus
from scadbuddy.bambuddy.print_links import PrintLink
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.printers.component import PRINTERS, Printers
from scadbuddy.printers.models import LanLink, PrinterCreate
from tests.api.test_send import API, configure
from tests.bambuddy.conftest import recording
from tests.printers.conftest import KEK

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000001"


def printer(printer_id: int, *, active: bool = True) -> dict[str, Any]:
    return {
        **recording("printers.json")[0],
        "id": printer_id,
        "name": f"P{printer_id}",
        "is_active": active,
    }


def status(printer_id: int, **fields: Any) -> dict[str, Any]:
    return {
        **recording("printer-status.json"),
        "id": printer_id,
        "name": f"P{printer_id}",
        **fields,
    }


@respx.mock
def test_every_active_printer_says_what_it_is_doing(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(
            200, json=[printer(1), printer(2), printer(3), printer(4, active=False)]
        )
    )
    # 1: running archive 13, which ScadBuddy linked (the recorded status).
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=status(1)))
    # 2: the 2026-10-01 pause: spaghetti detection and AMS feed resistance, archive 90,
    # started from Bambuddy.
    respx.get(f"{API}/printers/2/status").mock(
        return_value=httpx.Response(
            200,
            json=status(
                2,
                state="PAUSE",
                progress=32.0,
                layer_num=9,
                total_layers=90,
                remaining_time=81,
                subtask_name="Bracket",
                current_archive_id=90,
                hms_errors=[
                    {
                        "code": "0x8042",
                        "full_code": "0C008042",
                        "severity": 2,
                        "description": "Spaghetti detected",
                        "actions": ["RESUME_PRINTING_DEFECTS", "STOP_PRINTING"],
                    },
                    {"code": "0x25", "full_code": "0700220000020025", "description": None},
                ],
            ),
        )
    )
    respx.get(f"{API}/printers/3/status").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )
    never = respx.get(f"{API}/printers/4/status")
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    asyncio.run(
        state.print_links.record(
            PrintSubject.library(1630), PrintLink(archive_id=13, matched_by="queue_item")
        )
    )

    response = client.get("/api/v1/print/printers/now")

    assert response.status_code == 200, response.text
    cards = {card["printer_id"]: card for card in response.json()["printers"]}
    assert set(cards) == {1, 2, 3}
    assert not never.called

    running = cards[1]
    assert running["activity"] == "printing"
    assert running["job_name"] == "Name_Keychain_Font_Basic_Plate_1"
    assert (running["progress"], running["layer"], running["total_layers"]) == (55.0, 40, 79)
    assert running["remaining_minutes"] == 50
    assert (running["archive_id"], running["linked"], running["bambuddy_url"]) == (13, True, None)

    paused = cards[2]
    assert paused["activity"] == "paused"
    assert (paused["archive_id"], paused["linked"]) == (90, False)
    assert paused["bambuddy_url"].endswith("/archives")
    assert [(hms["code"], hms["description"]) for hms in paused["hms"]] == [
        ("0C008042", "Spaghetti detected"),
        ("0700220000020025", None),
    ]

    assert cards[3]["activity"] == "unknown"
    assert cards[3]["error"]
    # #3632: a failed read does not know the plate is clear, so it never says so.
    assert cards[3]["awaiting_plate_clear"] is None


@respx.mock
def test_an_idle_printer_names_no_job(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/printers/").mock(return_value=httpx.Response(200, json=[printer(1)]))
    respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=status(1, state="FINISH", progress=100.0))
    )

    card = client.get("/api/v1/print/printers/now").json()["printers"][0]

    assert card["activity"] == "finished"
    assert card["job_name"] is None and card["progress"] is None and card["archive_id"] is None


DRIFTED = {
    "state": "PAUSE",
    "progress": "n/a",
    "remaining_time": 12.7,
    "layer_num": None,
    "total_layers": "90",
    "hms_errors": [
        {"code": 32834, "full_code": 201359426, "severity": "2", "actions": "STOP"},
        "garbage",
    ],
}


@respx.mock
def test_a_drifted_status_field_reads_as_unknown_not_a_failed_strip(client: TestClient) -> None:
    """#2750 review: an int HMS code, a fractional ``remaining_time`` or a non-dict HMS
    entry reads leniently, so neither /prints nor the print dialog's own status reads
    fail on a field they barely use."""
    configure(client)
    respx.get(f"{API}/printers/").mock(return_value=httpx.Response(200, json=[printer(1)]))
    respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=status(1, **DRIFTED))
    )

    response = client.get("/api/v1/print/printers/now")

    assert response.status_code == 200, response.text
    (card,) = response.json()["printers"]
    assert card["activity"] == "paused" and card["error"] is None
    assert (card["progress"], card["remaining_minutes"], card["total_layers"]) == (None, 12, 90)
    assert [hms["code"] for hms in card["hms"]] == ["201359426"]


def test_the_print_dialogs_status_read_takes_the_same_drifted_payload() -> None:
    """#2967 review: the print decisions read ``PrinterStatus`` directly, so the drifted
    payload must validate there too, not only on /prints."""
    read = PrinterStatus.model_validate(status(1, **DRIFTED))

    assert (read.progress, read.remaining_time, read.total_layers) == (None, 12, 90)
    assert [error.full_code for error in read.hms_errors] == ["201359426"]


@pytest.mark.parametrize(("sent", "read"), [(True, True), (False, False), ("yes", None), (1, None)])
def test_the_plate_flag_is_unknown_unless_it_is_a_bool(sent: Any, read: bool | None) -> None:
    """#3632: a drifted ``awaiting_plate_clear`` is unknown, never a quiet "no"."""
    assert (
        PrinterStatus.model_validate(status(1, awaiting_plate_clear=sent)).awaiting_plate_clear
        is read
    )


def test_a_status_without_the_plate_flag_reads_as_not_waiting() -> None:
    without = {name: value for name, value in status(1).items() if name != "awaiting_plate_clear"}

    assert PrinterStatus.model_validate(without).awaiting_plate_clear is False


@respx.mock
def test_a_finished_plate_is_on_the_card(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/printers/").mock(return_value=httpx.Response(200, json=[printer(1)]))
    respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(
            200, json=status(1, state="FINISH", awaiting_plate_clear="pending")
        )
    )

    card = client.get("/api/v1/print/printers/now").json()["printers"][0]

    assert (card["activity"], card["awaiting_plate_clear"]) == ("finished", None)


def test_a_number_too_large_for_a_float_reads_as_unknown() -> None:
    """#2967 review: ``float()`` raises OverflowError, not ValueError, on a huge JSON int."""
    read = PrinterStatus.model_validate(status(1, progress=10**400, remaining_time=10**400))

    assert (read.progress, read.remaining_time) == (None, None)


@respx.mock
def test_a_status_body_that_is_not_json_is_that_printers_card(client: TestClient) -> None:
    """#2967 review: a proxy error page in place of JSON is one card, not a failed strip."""
    configure(client)
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=[printer(1), printer(2)])
    )
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=status(1)))
    respx.get(f"{API}/printers/2/status").mock(
        return_value=httpx.Response(200, text="<html>Bad gateway</html>")
    )

    response = client.get("/api/v1/print/printers/now")

    assert response.status_code == 200, response.text
    cards = {card["printer_id"]: card for card in response.json()["printers"]}
    assert cards[1]["activity"] == "printing"
    assert cards[2]["activity"] == "unknown"
    assert "can't read" in cards[2]["error"]


@respx.mock
def test_a_status_in_an_unreadable_shape_is_that_printers_card(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=[printer(1), printer(2)])
    )
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=status(1)))
    # A field the print decisions do model, in a shape nothing coerces.
    respx.get(f"{API}/printers/2/status").mock(
        return_value=httpx.Response(200, json=status(2, nozzles="not a list"))
    )

    response = client.get("/api/v1/print/printers/now")

    assert response.status_code == 200, response.text
    cards = {card["printer_id"]: card for card in response.json()["printers"]}
    assert cards[1]["activity"] == "printing"
    assert cards[2]["activity"] == "unknown"
    assert "can't read" in cards[2]["error"]


# --- the registry (#3823, plan 2026-10-16, Task 6) -------------------------------------


def _registry(client: TestClient) -> Printers:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(PRINTERS)


def _lan_printer(client: TestClient, serial: str = SERIAL, name: str = "Garage") -> str:
    """A LAN-linked row, written straight to the store (the link's probe is Task 3's)."""
    printers = _registry(client)
    printers.store.kek = KEK
    row = printers.store.create(PrinterCreate(serial=serial, name=name))
    printers.store.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    return row.id


@respx.mock
def test_now_prefers_the_lan_report_when_both_links_are_set(client: TestClient) -> None:
    """D4 (hot-path plan, 2026-10-18): a fresh LAN report answers, and Bambuddy's
    status is never read for that printer."""
    configure(client)
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=[{**printer(1), "serial_number": SERIAL}])
    )
    status_read = respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=status(1))
    )
    pid = _lan_printer(client)
    _registry(client).reports.put(pid, {"gcode_state": "IDLE"}, datetime.now(UTC))

    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]

    assert (card["id"], card["printer_id"]) == (pid, 1)
    assert (card["path"], card["activity"]) == ("native", "idle")
    assert "serial" not in card  # I6: the serial stays out of the strip
    assert not status_read.called


@respx.mock
def test_now_reads_bambuddy_for_a_bambuddy_only_printer(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=[{**printer(1), "serial_number": SERIAL}])
    )
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=status(1)))

    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]

    assert (card["printer_id"], card["path"], card["activity"]) == (1, "bambuddy", "printing")


@respx.mock
def test_now_reads_bambuddy_when_the_lan_report_is_stale(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=[{**printer(1), "serial_number": SERIAL}])
    )
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=status(1)))
    pid = _lan_printer(client)
    _registry(client).reports.put(
        pid, {"gcode_state": "IDLE"}, datetime.now(UTC) - timedelta(seconds=91)
    )

    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]

    assert (card["id"], card["path"], card["activity"]) == (pid, "bambuddy", "printing")


@respx.mock
def test_now_with_bambuddy_down_still_lists_lan_printers(client: TestClient) -> None:
    """I1: an unreachable Bambuddy costs only what only it can say."""
    configure(client)
    respx.get(f"{API}/printers/").mock(return_value=httpx.Response(503))
    lan_only = _lan_printer(client, serial="01P00A000000002", name="Attic")
    _registry(client).reports.put(lan_only, {"gcode_state": "IDLE"}, datetime.now(UTC))
    both = _lan_printer(client)
    _registry(client).store.link_bambuddy(both, 1)
    _registry(client).reports.put(both, {"gcode_state": "RUNNING"}, datetime.now(UTC))
    bambuddy_only = _registry(client).store.ensure_bambuddy(
        2, serial="01P00A000000003", name="P2", model=None
    )

    response = client.get("/api/v1/print/printers/now")

    assert response.status_code == 200, response.text
    cards = {card["id"]: card for card in response.json()["printers"]}
    assert (cards[lan_only]["path"], cards[lan_only]["activity"]) == ("native", "idle")
    assert (cards[both]["path"], cards[both]["activity"]) == ("native", "printing")
    gone = cards[bambuddy_only.id]
    assert (gone["path"], gone["activity"], gone["printer_id"]) == ("bambuddy", "unknown", 2)
    assert gone["error"]


@respx.mock
def test_a_merged_printer_keeps_its_id_on_the_strip(client: TestClient) -> None:
    """I7: once Bambuddy reports the serial, the synthetic row's card becomes the row
    holding the serial, and nothing keeps the synthetic id."""
    configure(client)
    listing = respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=[{**printer(1), "serial_number": ""}])
    )
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=status(1)))
    (synthetic,) = client.get("/api/v1/print/printers/now").json()["printers"]
    mine = _registry(client).store.create(PrinterCreate(serial=SERIAL, name="Mine"))
    listing.mock(return_value=httpx.Response(200, json=[{**printer(1), "serial_number": SERIAL}]))

    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]

    assert card["id"] == mine.id != synthetic["id"]
    assert _registry(client).store.get(synthetic["id"]) is None


@respx.mock
def test_every_bambuddy_printer_gets_a_registry_row(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(
            200, json=[{**printer(1), "serial_number": SERIAL}, {**printer(2), "serial_number": ""}]
        )
    )
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=status(1)))
    respx.get(f"{API}/printers/2/status").mock(return_value=httpx.Response(200, json=status(2)))

    first = client.get("/api/v1/print/printers/now").json()["printers"]
    again = client.get("/api/v1/print/printers/now").json()["printers"]

    assert [card["id"] for card in first] == [card["id"] for card in again]
    serials = [_registry(client).store.require(card["id"]).serial for card in first]
    assert [card["printer_id"] for card in first] == [1, 2]
    assert serials == [SERIAL, "bambuddy-2"]
    assert len(_registry(client).store.list()) == 2


def test_now_uses_report_for_lan_only_printer(client: TestClient) -> None:
    pid = _lan_printer(client)
    _registry(client).reports.put(
        pid,
        {"gcode_state": "RUNNING", "mc_percent": 30, "subtask_name": "Bracket", "layer_num": 3},
        datetime.now(UTC),
    )

    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]

    assert (card["id"], card["printer_id"], card["path"]) == (pid, None, "native")
    assert (card["activity"], card["job_name"], card["progress"]) == ("printing", "Bracket", 30.0)


def test_a_stale_report_is_offline(client: TestClient) -> None:
    """Review Focus 5: past `STALE_AFTER` without a report the card is offline."""
    pid = _lan_printer(client)
    _registry(client).reports.put(
        pid, {"gcode_state": "RUNNING"}, datetime.now(UTC) - timedelta(seconds=91)
    )

    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]

    assert (card["path"], card["activity"]) == ("native", "offline")


def test_now_without_bambuddy_configured_still_lists_lan_printers(client: TestClient) -> None:
    reported = _lan_printer(client)
    silent = _lan_printer(client, serial="01P00A000000002", name="Attic")
    _registry(client).reports.put(reported, {"gcode_state": "IDLE"}, datetime.now(UTC))

    response = client.get("/api/v1/print/printers/now")

    assert response.status_code == 200, response.text
    cards = {card["id"]: card for card in response.json()["printers"]}
    assert (cards[reported]["path"], cards[reported]["activity"]) == ("native", "idle")
    assert (cards[silent]["path"], cards[silent]["activity"]) == ("none", "offline")
