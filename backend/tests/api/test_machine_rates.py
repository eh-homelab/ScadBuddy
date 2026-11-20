"""`/api/v1/printers/{id}/machine-rate` (#4625, order operations spec 2026-11-07 §6): a
printer's machine rate inputs, replaced whole, and the rate they give with each part."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import psycopg
import respx
from fastapi.testclient import TestClient

from tests.api.test_printers import bambuddy_printer, bambuddy_printers, create
from tests.api.test_send import API, configure
from tests.bambuddy.conftest import recording

INPUTS: dict[str, Any] = {
    "profile": {
        "purchase_price": "1440",
        "life_hours": "3000",
        "watts": "120",
        "kwh_price": "0.25",
        "labour_rate_per_hour": "30",
    },
    "wear_parts": [{"name": "Nozzle", "cost": "14", "life_hours": "200"}],
    "maintenance": [
        {"task": "Lubricate rods", "minutes": "20", "consumables": "2", "interval_hours": "100"}
    ],
}


def path(printer: str) -> str:
    return f"/api/v1/printers/{printer}/machine-rate"


def test_a_printer_with_no_profile_is_the_books_rate(client: TestClient) -> None:
    row = create(client)
    view = client.get(path(row["id"]))
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["inputs"] is None
    assert (body["printer"], body["printer_name"]) == (row["id"], "H2C")
    assert body["breakdown"]["source"] == "price_book"
    assert body["breakdown"]["rate_per_hour"] == "0"


def test_inputs_are_replaced_whole_and_priced(client: TestClient) -> None:
    row = create(client)
    put = client.put(path(row["id"]), json=INPUTS)
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["inputs"]["wear_parts"] == [
        {**INPUTS["wear_parts"][0], "priced_by": "estimate", "renamed_from": None}
    ]
    breakdown = body["breakdown"]
    assert breakdown["source"] == "profile"
    assert breakdown["intervals"] == "typed"
    assert (
        breakdown["maintenance_per_hour"],
        breakdown["wear_per_hour"],
        breakdown["power_per_hour"],
        breakdown["depreciation_per_hour"],
        breakdown["rate_per_hour"],
    ) == ("0.12", "0.07", "0.03", "0.48", "0.7")
    assert client.get(path(row["id"])).json() == body
    assert client.delete(path(row["id"])).status_code == 204
    assert client.get(path(row["id"])).json()["inputs"] is None
    assert client.delete(path(row["id"])).status_code == 404


def test_an_unknown_printer_is_404(client: TestClient) -> None:
    assert client.get(path("nope")).status_code == 404
    assert client.put(path("nope"), json=INPUTS).status_code == 404
    assert client.delete(path("nope")).status_code == 404


def test_bad_inputs_are_422(client: TestClient) -> None:
    row = create(client)
    zero_life = {**INPUTS, "wear_parts": [{"name": "Nozzle", "cost": "14", "life_hours": "0"}]}
    assert client.put(path(row["id"]), json=zero_life).status_code == 422
    twice = {**INPUTS, "wear_parts": INPUTS["wear_parts"] * 2}
    assert client.put(path(row["id"]), json=twice).status_code == 422


@respx.mock
def test_a_linked_printer_reads_bambuddys_intervals(client: TestClient) -> None:
    configure(client)
    bambuddy_printers(bambuddy_printer(1, "01P00A000000001"))
    row = create(client)
    assert (
        client.put(
            f"/api/v1/printers/{row['id']}/bambuddy", json={"bambuddy_printer_id": 1}
        ).status_code
        == 200
    )
    maintenance = respx.get(f"{API}/maintenance/printers/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "printer_id": 1,
                "maintenance_items": [
                    {
                        "maintenance_type_name": "Lubricate rods",
                        "enabled": True,
                        "interval_hours": 50.0,
                        "interval_type": "hours",
                    }
                ],
            },
        )
    )
    body = client.put(path(row["id"]), json=INPUTS).json()
    assert maintenance.called
    assert body["bambuddy_printer_id"] == 1
    assert body["breakdown"]["intervals"] == "bambuddy"
    assert body["breakdown"]["maintenance_per_hour"] == "0.24"

    maintenance.mock(return_value=httpx.Response(503, json={"detail": "down"}))
    stored = client.get(path(row["id"])).json()["breakdown"]
    assert stored["intervals"] == "stored"
    assert stored["maintenance_per_hour"] == "0.24"


# -- #5774: actuals, events and the counter (spec 2026-11-17-machine-rate-actuals §5) -------


def events(printer: str) -> str:
    return f"{path(printer)}/events"


def nozzle(**fields: Any) -> dict[str, Any]:
    return {"kind": "part", "name": "Nozzle", "cost": "14", **fields}


def lan_link(pg_conninfo: str, printer: str, run_seconds: int | None = None) -> None:
    """The printer LAN-linked, and its counter at ``run_seconds`` when given, as the LAN
    watch would leave them."""
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute("UPDATE printers SET lan_host = '10.0.0.9' WHERE id = %s", (printer,))
        if run_seconds is not None:
            conn.execute(
                "INSERT INTO printer_run_hours (printer, run_seconds, counted_to)"
                " VALUES (%s, %s, now())",
                (printer, run_seconds),
            )


def test_put_keeps_the_window_the_offset_and_each_rows_choice(client: TestClient) -> None:
    row = create(client)
    body = {
        **INPUTS,
        "profile": {**INPUTS["profile"], "actuals_window": 1, "lan_hours_offset": "40.5"},
        "wear_parts": [{**INPUTS["wear_parts"][0], "priced_by": "actual"}],
    }
    put = client.put(path(row["id"]), json=body).json()
    assert put["inputs"]["profile"]["actuals_window"] == 1
    assert put["inputs"]["profile"]["lan_hours_offset"] == "40.5"
    assert put["inputs"]["wear_parts"][0]["priced_by"] == "actual"
    wear = put["breakdown"]["wear"][0]
    assert (wear["priced_by"], wear["basis"], wear["confidence"], wear["actual_why"]) == (
        "actual",
        "estimate",
        0,
        "no_events",
    )
    assert put["breakdown"]["confidence"] == 0
    # A client that does not know the fields (the editor before #5774 PR 5, an older
    # agent) leaves them out, and they are kept, under a row's new name too.
    renamed = {**INPUTS["wear_parts"][0], "name": "Hotend", "renamed_from": "Nozzle"}
    kept = client.put(path(row["id"]), json={**INPUTS, "wear_parts": [renamed]}).json()
    assert kept["inputs"]["profile"]["actuals_window"] == 1
    assert kept["inputs"]["profile"]["lan_hours_offset"] == "40.5"
    assert kept["inputs"]["wear_parts"][0]["priced_by"] == "actual"
    bad = {**INPUTS, "profile": {**INPUTS["profile"], "actuals_window": 21}}
    assert client.put(path(row["id"]), json=bad).status_code == 422


def test_a_lan_printers_events_read_scadbuddys_counter(
    client: TestClient, pg_conninfo: str
) -> None:
    row = create(client)
    profile = {**INPUTS["profile"], "actuals_window": 1, "lan_hours_offset": "100"}
    parts = [{**INPUTS["wear_parts"][0], "priced_by": "actual"}]
    client.put(path(row["id"]), json={**INPUTS, "profile": profile, "wear_parts": parts})
    lan_link(pg_conninfo, row["id"])
    # LAN-linked, but the watch has written no report yet: no reading.
    unread = client.post(events(row["id"]), json=nozzle())
    assert unread.status_code == 503, unread.text
    assert unread.json()["code"] == "counter_unavailable"

    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO printer_run_hours (printer, run_seconds, counted_to)"
            " VALUES (%s, 7200, now())",
            (row["id"],),
        )
    first = client.post(events(row["id"]), json=nozzle(source="form:1"))
    assert first.status_code == 201, first.text
    assert (first.json()["hours_at"], first.json()["hours_source"]) == ("102", "lan_watch")
    again = client.post(events(row["id"]), json=nozzle(source="form:1", cost="99"))
    assert again.status_code == 200
    assert again.json() == first.json()
    corrected = client.post(events(row["id"]), json=nozzle(hours_at="402", cost="20"))
    assert corrected.json()["hours_source"] == "lan_watch"
    task = client.post(events(row["id"]), json={"kind": "task", "name": "Lubricate rods"})
    assert task.status_code == 201, task.text

    view = client.get(path(row["id"])).json()
    assert view["counter"]["source"] == "lan_watch"
    assert view["counter"]["hours"] == "102"
    assert view["counter"]["lan_watch"]["run_hours"] == "2"
    assert view["counter"]["lan_watch"]["offset_hours"] == "100"
    # The nozzle ran 300 h for 20: 0.0667 prices it at window 1.
    wear = view["breakdown"]["wear"][0]
    assert (wear["basis"], wear["per_hour"], wear["confidence"]) == ("actual", "0.0667", 100)
    listed = next(r for r in view["actuals"]["rows"] if r["name"] == "Nozzle")
    # The replacement and the one its hours_used is derived from.
    assert [e["hours_at"] for e in listed["events"]] == ["402", "102"]


def test_a_printer_with_no_counter_needs_a_reading(client: TestClient) -> None:
    row = create(client)
    client.put(path(row["id"]), json=INPUTS)
    missing = client.post(events(row["id"]), json=nozzle())
    assert missing.status_code == 422
    assert missing.json()["code"] == "counter_reading_required"
    task = client.post(events(row["id"]), json={"kind": "task", "name": "Lubricate rods"})
    assert task.json()["code"] == "counter_reading_required"
    used = client.post(events(row["id"]), json=nozzle(hours_used="640"))
    assert used.status_code == 201, used.text
    assert (used.json()["hours_at"], used.json()["hours_source"]) == (None, None)
    typed = client.post(events(row["id"]), json=nozzle(hours_at="12.5"))
    assert typed.json()["hours_source"] == "typed"
    view = client.get(path(row["id"])).json()
    assert view["counter"] == {"source": "typed", "hours": None, "lan_watch": None}
    unknown = client.post(events(row["id"]), json={**nozzle(), "name": "Plate"})
    assert (unknown.status_code, unknown.json()["code"]) == (422, "unknown_part")


@respx.mock
def test_a_linked_printers_counter_is_bambuddys(client: TestClient, pg_conninfo: str) -> None:
    configure(client)
    bambuddy_printers(bambuddy_printer(1, "01P00A000000001"))
    row = create(client)
    client.put(f"/api/v1/printers/{row['id']}/bambuddy", json={"bambuddy_printer_id": 1})
    item = {
        "id": 31,
        "maintenance_type_name": "Lubricate rods",
        "enabled": True,
        "interval_hours": 50.0,
        "interval_type": "hours",
        "current_hours": 495.34,
    }
    maintenance = respx.get(f"{API}/maintenance/printers/1").mock(
        return_value=httpx.Response(200, json={"printer_id": 1, "maintenance_items": [item]})
    )
    respx.get(f"{API}/maintenance/items/31/history").mock(return_value=httpx.Response(200, json=[]))
    client.put(path(row["id"]), json=INPUTS)
    # Linked and LAN-linked: ScadBuddy's count is a check beside Bambuddy's.
    lan_link(pg_conninfo, row["id"], run_seconds=3600)
    view = client.get(path(row["id"])).json()
    assert (view["counter"]["source"], view["counter"]["hours"]) == ("bambuddy", "495.3")
    assert view["counter"]["lan_watch"]["hours"] == "1"

    task = client.post(events(row["id"]), json={"kind": "task", "name": "Lubricate rods"})
    assert (task.status_code, task.json()["code"]) == (409, "maintenance_from_bambuddy")
    part = client.post(events(row["id"]), json=nozzle())
    assert part.status_code == 201, part.text
    assert (part.json()["hours_at"], part.json()["hours_source"]) == ("495.3", "bambuddy")

    maintenance.mock(return_value=httpx.Response(503, json={"detail": "down"}))
    down = client.post(events(row["id"]), json=nozzle())
    assert (down.status_code, down.json()["code"]) == (503, "counter_unavailable")
    typed = client.post(events(row["id"]), json=nozzle(hours_at="500"))
    assert typed.json()["hours_source"] == "bambuddy"
    assert client.get(path(row["id"])).json()["counter"]["hours"] is None


def test_events_are_listed_patched_and_deleted(client: TestClient, pg_conninfo: str) -> None:
    row = create(client)
    client.put(path(row["id"]), json=INPUTS)
    made = [
        client.post(
            events(row["id"]), json=nozzle(hours_at=str(h), at=f"2026-11-0{h}T00:00:00Z")
        ).json()
        for h in (1, 2, 3)
    ]
    page = client.get(events(row["id"]), params={"kind": "part", "limit": 2}).json()
    assert [e["id"] for e in page["events"]] == [made[2]["id"], made[1]["id"]]
    rest = client.get(events(row["id"]), params={"cursor": page["next"]}).json()
    assert [e["id"] for e in rest["events"]] == [made[0]["id"]]
    assert client.get(events(row["id"]), params={"cursor": "nope"}).status_code == 422

    one = f"{events(row['id'])}/{made[0]['id']}"
    patched = client.patch(one, json={"cost": "20", "notes": "late"})
    assert (patched.json()["cost"], patched.json()["notes"]) == ("20", "late")
    assert client.patch(one, json={"minutes": "5"}).status_code == 422
    assert client.delete(one).status_code == 204
    assert client.delete(one).status_code == 404
    assert client.patch(one, json={"notes": "x"}).status_code == 404

    imported = uuid.uuid4()
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO printer_maintenance_events (id, printer, task, performed_at, origin,"
            " bambuddy_history_id, source, created_by) VALUES (%s, %s, 'Lubricate rods',"
            " now(), 'bambuddy', 3, 'bambuddy:3', 'import')",
            (imported, row["id"]),
        )
    refused = client.delete(f"{events(row['id'])}/{imported}")
    assert (refused.status_code, refused.json()["code"]) == (409, "imported_event")
    minutes = client.patch(f"{events(row['id'])}/{imported}", json={"minutes": "12"})
    assert minutes.status_code == 200 and minutes.json()["minutes"] == "12"
    assert client.get(events("nope")).status_code == 404


def test_an_imported_events_reading_is_not_cleared(client: TestClient, pg_conninfo: str) -> None:
    row = create(client)
    client.put(path(row["id"]), json=INPUTS)
    imported = uuid.uuid4()
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO printer_maintenance_events (id, printer, task, performed_at, hours_at,"
            " hours_source, origin, bambuddy_history_id, source, created_by) VALUES (%s, %s,"
            " 'Lubricate rods', now(), 40, 'bambuddy', 'bambuddy', 3, 'bambuddy:3', 'import')",
            (imported, row["id"]),
        )
    one = f"{events(row['id'])}/{imported}"
    refused = client.patch(one, json={"hours_at": None})
    assert (refused.status_code, refused.json()["code"]) == (422, "imported_event")
    kept = client.get(events(row["id"])).json()["events"][0]
    assert (kept["hours_at"], kept["hours_source"]) == ("40", "bambuddy")


PLATE_INPUTS: dict[str, Any] = {
    "profile": {"labour_rate_per_hour": "30"},
    "wear_parts": [],
    "maintenance": [{"task": "Clean Build Plate", "minutes": "10", "priced_by": "actual"}],
}


def linked_plate(client: TestClient) -> tuple[str, respx.Route]:
    """A printer linked to Bambuddy printer 1, whose one task is prod's Clean Build Plate
    (item 5), and the route of its log, answering the recorded one (§2.1)."""
    configure(client)
    bambuddy_printers(bambuddy_printer(1, "01P00A000000001"))
    row = create(client)
    client.put(f"/api/v1/printers/{row['id']}/bambuddy", json={"bambuddy_printer_id": 1})
    item = {
        "id": 5,
        "maintenance_type_name": "Clean Build Plate",
        "enabled": True,
        "interval_hours": 25.0,
        "interval_type": "hours",
        "current_hours": 510.3425,
    }
    respx.get(f"{API}/maintenance/printers/1").mock(
        return_value=httpx.Response(200, json={"printer_id": 1, "maintenance_items": [item]})
    )
    log = respx.get(f"{API}/maintenance/items/5/history").mock(
        return_value=httpx.Response(200, json=recording("maintenance-history.json"))
    )
    return row["id"], log


@respx.mock
def test_the_view_and_the_button_import_bambuddys_log(client: TestClient) -> None:
    printer, log = linked_plate(client)
    no_profile = client.post(f"{path(printer)}/import")
    assert (no_profile.status_code, no_profile.json()["code"]) == (409, "no_machine_rate")
    assert not log.called

    view = client.put(path(printer), json=PLATE_INPUTS).json()
    assert log.called
    assert view["actuals"]["events_read_at"] is not None
    plate = view["breakdown"]["maintenance"][0]
    # §3.3's example: 15.00 over 196.6 h.
    assert (plate["basis"], plate["per_hour"], plate["confidence"]) == ("actual", "0.0763", 100)
    row = next(r for r in view["actuals"]["rows"] if r["name"] == "Clean Build Plate")
    assert [e["hours_at"] for e in row["events"]] == ["465.6", "341.9", "304", "269"]
    assert {e["origin"] for e in row["events"]} == {"bambuddy"}

    read = client.post(f"{path(printer)}/import")
    assert read.status_code == 200, read.text
    assert read.json()["actuals"]["events_read_at"] >= view["actuals"]["events_read_at"]
    listed = client.get(events(printer), params={"kind": "task"}).json()["events"]
    assert len(listed) == 5

    log.mock(return_value=httpx.Response(403, json={"detail": "no"}))
    down = client.post(f"{path(printer)}/import")
    assert (down.status_code, down.json()["code"]) == (503, "log_unavailable")
    kept = client.get(path(printer)).json()
    assert kept["actuals"]["events_read_at"] == read.json()["actuals"]["events_read_at"]
    assert len(client.get(events(printer), params={"kind": "task"}).json()["events"]) == 5


def test_the_button_needs_a_linked_printer(client: TestClient) -> None:
    row = create(client)
    client.put(path(row["id"]), json=INPUTS)
    refused = client.post(f"{path(row['id'])}/import")
    assert (refused.status_code, refused.json()["code"]) == (409, "not_linked")
    assert client.post(f"{path('nope')}/import").status_code == 404


def test_the_imports_source_and_a_time_with_no_zone_are_refused(client: TestClient) -> None:
    """#6642 review: ``bambuddy:`` is the import's key (§4), and a time with no zone
    would be read in the server's zone."""
    row = create(client)
    client.put(path(row["id"]), json=INPUTS)
    for taken in ("bambuddy:38", "Bambuddy:38"):
        refused = client.post(events(row["id"]), json=nozzle(hours_at="10", source=taken))
        assert refused.status_code == 422, refused.text
    naive = client.post(events(row["id"]), json=nozzle(hours_at="10", at="2026-11-01T00:00:00"))
    assert naive.status_code == 422, naive.text
    aware = client.post(
        events(row["id"]), json=nozzle(hours_at="10", at="2026-11-01T02:00:00+02:00")
    )
    assert aware.status_code == 201, aware.text
    assert aware.json()["at"].startswith("2026-11-01T00:00:00")
    assert client.get(events(row["id"])).json()["events"][0]["id"] == aware.json()["id"]


def test_a_source_names_one_event_across_both_kinds(client: TestClient) -> None:
    row = create(client)
    client.put(path(row["id"]), json=INPUTS)
    part = client.post(events(row["id"]), json=nozzle(hours_at="10", source="form:1"))
    assert part.status_code == 201, part.text
    task = client.post(
        events(row["id"]),
        json={"kind": "task", "name": "Lubricate rods", "hours_at": "20", "source": "form:1"},
    )
    assert (task.status_code, task.json()["code"]) == (409, "source_taken")
    listed = client.get(events(row["id"])).json()["events"]
    assert [e["kind"] for e in listed] == ["part"]


def test_a_retry_after_its_row_is_renamed_or_deleted_returns_the_first_event(
    client: TestClient,
) -> None:
    """A retry is looked up by its ``source`` before its name: the row it named may have
    been renamed or deleted since the first write."""
    row = create(client)
    client.put(path(row["id"]), json=INPUTS)
    first = client.post(events(row["id"]), json=nozzle(hours_at="10", source="form:1"))
    assert first.status_code == 201, first.text
    renamed = {**INPUTS["wear_parts"][0], "name": "Hotend", "renamed_from": "Nozzle"}
    client.put(path(row["id"]), json={**INPUTS, "wear_parts": [renamed]})
    after_rename = client.post(events(row["id"]), json=nozzle(hours_at="10", source="form:1"))
    assert after_rename.status_code == 200, after_rename.text
    assert after_rename.json() == {**first.json(), "name": "Hotend"}
    client.put(path(row["id"]), json={**INPUTS, "wear_parts": []})
    after_delete = client.post(events(row["id"]), json=nozzle(hours_at="10", source="form:1"))
    assert after_delete.status_code == 200, after_delete.text
    assert after_delete.json() == {**first.json(), "name": "Hotend"}
