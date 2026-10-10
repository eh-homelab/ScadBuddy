"""Issue #1912 — the farm-context reads: the queue, aggregate stats, every archive with
its outcome, and the spool inventory with each loaded slot's remaining grams.

Recorded from the live Bambuddy 1.2.6b1 (recordings/README.md). Bambuddy gates every one
of these reads on ``can_read_status`` (``auth.py``'s ``QUEUE_READ``, ``STATS_READ``,
``ARCHIVES_READ`` and ``INVENTORY_READ`` all map to it)."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import SCOPE_PROBLEM, Scope
from scadbuddy.bambuddy.farm import inventory_view
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"


@respx.mock
async def test_the_queue_is_filtered_by_printer_and_status(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue.json"))
    )

    items = await bambuddy.queue(printer_id=1, status="pending")

    params = route.calls.last.request.url.params
    assert (params["printer_id"], params["status"]) == ("1", "pending")
    assert route.calls.last.request.headers["X-API-Key"] == "s3cret"
    printing = items[0]
    assert (printing.id, printing.status, printing.archive_name) == (
        257,
        "printing",
        "Carrot Garden",
    )
    assert printing.printer_name == "3DP-31B-598"
    assert printing.filament_used_grams is not None
    assert printing.created_at is not None


@respx.mock
async def test_an_unfiltered_queue_sends_no_filters(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/queue/").mock(return_value=httpx.Response(200, json=[]))

    assert await bambuddy.queue() == []
    assert dict(route.calls.last.request.url.params) == {}


@respx.mock
@pytest.mark.parametrize(
    ("path", "read"),
    [
        ("/queue/", lambda client: client.queue()),
        ("/archives/stats", lambda client: client.archive_stats()),
        ("/archives/", lambda client: client.archive_outcomes()),
    ],
)
async def test_a_key_without_read_status_names_the_scope(
    bambuddy: BambuddyClient, path: str, read: object
) -> None:
    respx.get(f"{API}{path}").mock(
        return_value=httpx.Response(403, json={"detail": "Missing permission"})
    )

    with pytest.raises(ApiError) as caught:
        await read(bambuddy)  # type: ignore[operator]

    assert caught.value.type == SCOPE_PROBLEM
    assert caught.value.extensions["required_scope"] == Scope.READ_STATUS.value


@respx.mock
async def test_stats_take_a_date_window(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/archives/stats").mock(
        return_value=httpx.Response(200, json=recording("archive-stats.json"))
    )

    stats = await bambuddy.archive_stats(date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))

    params = route.calls.last.request.url.params
    assert (params["date_from"], params["date_to"]) == ("2026-09-01", "2026-09-30")
    assert stats.total_prints == 104
    assert (stats.successful_prints, stats.failed_prints, stats.cancelled_prints) == (59, 22, 23)
    assert stats.prints_by_printer == {"1": 104}
    assert stats.printer_names == {"1": "3DP-31B-598"}


@respx.mock
async def test_unwindowed_stats_send_no_dates(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/archives/stats").mock(
        return_value=httpx.Response(200, json=recording("archive-stats.json"))
    )

    await bambuddy.archive_stats()

    assert dict(route.calls.last.request.url.params) == {}


@respx.mock
async def test_archive_outcomes_read_every_archive_with_its_outcome(
    bambuddy: BambuddyClient,
) -> None:
    route = respx.get(f"{API}/archives/").mock(
        return_value=httpx.Response(200, json=recording("archives-full.json"))
    )

    rows = await bambuddy.archive_outcomes(
        printer_id=1, project_id=4, date_from=date(2026, 10, 1), limit=3, offset=6
    )

    params = route.calls.last.request.url.params
    assert dict(params) == {
        "printer_id": "1",
        "project_id": "4",
        "date_from": "2026-10-01",
        "limit": "3",
        "offset": "6",
    }
    assert [row.status for row in rows] == ["printing", "completed", "archived"]
    assert rows[1].print_name == "Bambu Bed Scraper"
    assert rows[1].filament_used_grams is not None


@respx.mock
async def test_the_inventory_joins_spools_to_their_slots_and_remaining_grams(
    bambuddy: BambuddyClient,
) -> None:
    spools = respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=recording("inventory-spools.json"))
    )
    respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
    )
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=recording("printers.json"))
    )
    respx.get(f"{API}/printers/1/inventory-remain").mock(
        return_value=httpx.Response(200, json=recording("inventory-remain.json"))
    )

    view = await inventory_view(bambuddy, include_archived=True)

    assert spools.calls.last.request.url.params["include_archived"] == "true"
    assert len(view.spools) == 12
    loaded = next(spool for spool in view.spools if spool.id == 9)
    assert loaded.loaded is not None
    assert (loaded.loaded.printer_id, loaded.loaded.ams_id, loaded.loaded.tray_id) == (1, 0, 1)
    assert loaded.remaining_g == 1000.0
    shelf = [spool for spool in view.spools if spool.loaded is None]
    assert len(shelf) == 12 - 5
    slot = next(slot for slot in view.slots if slot.global_tray_id == 4)
    assert (slot.printer_id, slot.printer_name, slot.ams_id, slot.tray_id) == (
        1,
        "3DP-31B-598",
        1,
        0,
    )
    assert slot.spool_id == 7
    assert slot.remaining_g == pytest.approx(877.49, abs=0.01)
    assert (slot.brand, slot.material, slot.color_name, slot.rgba) == (
        "Inland",
        "PLA",
        "White",
        "E3E5E5FF",
    )
    assert slot.extruder == 0
    assert len(view.slots) == 5


@respx.mock
async def test_the_inventory_of_one_printer_still_places_spools_loaded_elsewhere(
    bambuddy: BambuddyClient,
) -> None:
    """``printer_id`` narrows the slots, never the placements: a spool loaded in another
    printer must not read as free (review on #1996)."""
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=recording("inventory-spools.json"))
    )
    elsewhere = {
        "id": 99,
        "spool_id": 2,
        "printer_id": 5,
        "printer_name": "Other",
        "ams_id": 0,
        "tray_id": 2,
    }
    assignments = respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=[*recording("inventory-assignments.json"), elsewhere])
    )
    printer = respx.get(f"{API}/printers/1").mock(
        return_value=httpx.Response(200, json=recording("printer.json"))
    )
    remain = respx.get(f"{API}/printers/1/inventory-remain").mock(
        return_value=httpx.Response(200, json=recording("inventory-remain.json"))
    )

    view = await inventory_view(bambuddy, printer_id=1)

    assert printer.called and remain.called
    assert "printer_id" not in assignments.calls.last.request.url.params
    assert {slot.printer_id for slot in view.slots} == {1}
    spool = next(spool for spool in view.spools if spool.id == 2)
    assert spool.loaded is not None
    assert (spool.loaded.printer_id, spool.loaded.printer_name) == (5, "Other")


@respx.mock
async def test_an_inactive_printer_is_not_asked_for_its_slots(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/inventory/assignments").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(
            200, json=[{"id": 2, "name": "Old", "model": "X1C", "is_active": False}]
        )
    )

    view = await inventory_view(bambuddy)

    assert view.slots == []
