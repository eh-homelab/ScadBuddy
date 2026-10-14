"""#977 — what each printer is doing now on /prints, whoever started it: every active
printer's live status from Bambuddy, a job ScadBuddy linked pointing at its print, and
a printer whose status cannot be read as a card saying so."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.models import PrinterStatus
from scadbuddy.bambuddy.print_links import PrintLink
from scadbuddy.bambuddy.subject import PrintSubject
from tests.api.test_send import API, configure
from tests.bambuddy.conftest import recording

pytestmark = pytest.mark.requires_postgres


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
