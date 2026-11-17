"""Issue #1912 — the farm-context reads: the queue, aggregate stats, every archive with
its outcome. (The spool inventory is ScadBuddy's own since #6262 Task 9.)

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
