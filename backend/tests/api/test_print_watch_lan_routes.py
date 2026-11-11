"""#6041 — the routes over a LAN-only printer's print records (#4699 phase 4,
``print_lan_imports``): list them (`GET /print-imports/auto/lan`), dismiss one and link
one to a model. Their ids are the records' own, apart from the archive ids of
`GET /print-imports/auto`."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scadbuddy.print_watch.component import AUTO_IMPORTS
from scadbuddy.print_watch.lan import LanImport
from scadbuddy.print_watch.lan_store import MODEL_GONE, LanImportLinkedError
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.models import PrinterCreate
from tests.api.test_print_history import state

pytestmark = pytest.mark.requires_postgres

STARTED = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
AGENT = {"X-ScadBuddy-Agent-Author": "token:p1", "X-ScadBuddy-Agent-Author-Session": "s1"}


def a_printer(client: TestClient, name: str = "Garage") -> str:
    """A registry printer with no Bambuddy link."""
    printers = state(client).components.get(PRINTERS)
    return printers.store.create(PrinterCreate(serial="01P00A0000000L1", name=name)).id


def record(client: TestClient, printer: str, subtask: str, minutes: int = 0, **fields: Any) -> int:
    """Write a record as the watcher does (`LanImportStore.record`); its id."""
    row = LanImport(
        printer_id=printer,
        subtask_name=subtask,
        started_at=STARTED + timedelta(minutes=minutes),
        **{
            "state": "inbox",
            "source": "printer",
            "library_file_id": 1_000_000_041,
            "filename": f"{subtask}.gcode.3mf",
            "reason": "no model matched with confidence",
            **fields,
        },
    )
    written = asyncio.run(state(client).components.get(AUTO_IMPORTS).lan.record(row))
    assert written.id is not None
    return written.id


def lan(client: TestClient, **query: Any) -> list[dict[str, Any]]:
    response = client.get("/api/v1/print-imports/auto/lan", params=query)
    assert response.status_code == 200, response.text
    rows: list[dict[str, Any]] = response.json()
    return rows


def dismiss(client: TestClient, record_id: int, **headers: str) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/api/v1/print-imports/auto/lan/{record_id}/dismiss", headers=headers
    )
    return response


def link(client: TestClient, record_id: int, slug: str, **headers: str) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/api/v1/print-imports/auto/lan/{record_id}/link", json={"slug": slug}, headers=headers
    )
    return response


def activity(client: TestClient, record_id: int) -> dict[str, Any]:
    response = client.get(
        f"/api/v1/activity/print_auto_import:lan:{record_id}", params={"advanced": 1}
    )
    assert response.status_code == 200, response.text
    row: dict[str, Any] = response.json()["row"]
    return row


def test_the_records_are_listed_newest_first_with_the_printer_s_name(
    client: TestClient, model: str
) -> None:
    printer = a_printer(client)
    first = record(client, printer, "cube")
    second = record(client, printer, "vase", minutes=1)
    linked = record(
        client, printer, "demo", minutes=2, state="linked", slug=model, matched_by="job_name"
    )

    rows = lan(client)
    assert [row["id"] for row in rows] == [linked, second, first]
    assert {row["printer_name"] for row in rows} == {"Garage"}
    assert rows[0]["printer_id"] == printer and rows[0]["subtask_name"] == "demo"
    assert [row["id"] for row in lan(client, state="inbox")] == [second, first]
    assert [row["id"] for row in lan(client, state="linked")] == [linked]
    assert [row["id"] for row in lan(client, slug=model)] == [linked]
    # Paged by the record's own id.
    assert [row["id"] for row in lan(client, limit=1, before=second)] == [first]
    # The archive list does not hold them.
    assert client.get("/api/v1/print-imports/auto").json() == []


def test_a_record_whose_model_is_gone_is_in_the_inbox(client: TestClient, model: str) -> None:
    printer = a_printer(client)
    gone = record(client, printer, "old", state="linked", slug="gone-model", matched_by="job_name")

    [row] = lan(client)
    assert (row["id"], row["state"], row["slug"], row["reason"]) == (
        gone,
        "inbox",
        None,
        MODEL_GONE,
    )
    assert lan(client, state="linked") == []
    assert lan(client, slug="gone-model") == []
    # It can be dismissed, and loses its model.
    dismissed = dismiss(client, gone)
    assert dismissed.status_code == 200, dismissed.text
    assert (dismissed.json()["state"], dismissed.json()["slug"]) == ("dismissed", None)


def test_a_dismiss_leaves_the_inbox_and_rewrites_the_activity_row(
    client: TestClient, model: str
) -> None:
    printer = a_printer(client)
    record_id = record(client, printer, "cube")

    first = dismiss(client, record_id)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "dismissed" and first.json()["dismissed_at"] is not None
    assert first.json()["printer_name"] == "Garage"
    assert lan(client, state="inbox") == []
    assert [row["id"] for row in lan(client, state="dismissed")] == [record_id]
    row = activity(client, record_id)
    assert row["facets"]["state"] == "dismissed"
    assert row["title"].startswith("Dismissed print cube")
    again = dismiss(client, record_id)
    assert (again.status_code, again.json()["state"]) == (200, "dismissed")


def test_a_linked_record_is_not_dismissed_and_no_record_is_404(
    client: TestClient, model: str
) -> None:
    printer = a_printer(client)
    linked = record(client, printer, "demo", state="linked", slug=model, matched_by="job_name")

    refused = dismiss(client, linked)
    assert refused.status_code == 409, refused.text
    assert refused.json()["slug"] == model
    assert [row["state"] for row in lan(client)] == ["linked"]
    assert dismiss(client, 999_999).status_code == 404


def test_a_link_puts_the_print_on_the_model(client: TestClient, model: str) -> None:
    printer = a_printer(client)
    record_id = record(client, printer, "cube")

    linked = link(client, record_id, model)
    assert linked.status_code == 200, linked.text
    body = linked.json()
    assert (body["state"], body["slug"], body["matched_by"], body["reason"]) == (
        "linked",
        model,
        "import",
        None,
    )
    assert body["printer_name"] == "Garage"
    assert [row["id"] for row in lan(client, slug=model)] == [record_id]
    row = activity(client, record_id)
    assert (row["facets"]["state"], row["facets"]["matched_by"]) == ("linked", "import")
    assert row["subject"] == f"model:{model}"
    # The same link again gives the same answer.
    again = link(client, record_id, model)
    assert (again.status_code, again.json()) == (200, body)


def test_a_dismissed_record_can_be_linked(client: TestClient, model: str) -> None:
    printer = a_printer(client)
    record_id = record(client, printer, "cube")
    assert dismiss(client, record_id).status_code == 200

    linked = link(client, record_id, model)
    assert linked.status_code == 200, linked.text
    assert (linked.json()["state"], linked.json()["dismissed_at"]) == ("linked", None)


def test_a_link_is_refused_with_no_file_no_model_no_record_or_another_model(
    client: TestClient, model: str
) -> None:
    printer = a_printer(client)
    no_file = record(
        client,
        printer,
        "cube",
        source=None,
        library_file_id=None,
        filename=None,
        reason="no source had the 3MF (printer: not allowed in Settings)",
    )
    taken = record(
        client, printer, "demo", minutes=1, state="linked", slug=model, matched_by="job_name"
    )
    gone = record(
        client, printer, "old", minutes=2, state="linked", slug="gone-model", matched_by="job_name"
    )

    refused = link(client, no_file, model)
    assert refused.status_code == 409, refused.text
    assert "needs the file" in refused.json()["detail"]
    assert link(client, no_file, "no-such-model").status_code == 404
    assert link(client, 999_999, model).status_code == 404
    # Linked to a model that is there: a link onto another model is refused, and the
    # first link stays.
    store = state(client).components.get(AUTO_IMPORTS).lan
    with pytest.raises(LanImportLinkedError):
        asyncio.run(store.link(taken, "other-model", lambda slug: True))
    assert [row["slug"] for row in lan(client, state="linked")] == [model]
    # A record whose model is gone can be linked again.
    relinked = link(client, gone, model)
    assert relinked.status_code == 200, relinked.text
    assert relinked.json()["slug"] == model


def test_the_assistant_may_dismiss_and_link(client: TestClient, model: str) -> None:
    """Neither reaches a printer or Bambuddy: the tool's tier gates an agent's call."""
    printer = a_printer(client)
    first = record(client, printer, "cube")
    second = record(client, printer, "vase", minutes=1)

    assert dismiss(client, first, **AGENT).status_code == 200
    assert link(client, second, model, **AGENT).status_code == 200
