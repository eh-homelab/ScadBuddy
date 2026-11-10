"""Issue #1912 — ``/api/v1/farm/…``: the queue, every archive's outcome, the stats and the
spool inventory, read from Bambuddy and never written to it."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.print_links import JobMatches, PrintLink, PrintSend
from scadbuddy.bambuddy.subject import PrintSubject
from tests.api.test_send import API, configure
from tests.bambuddy.conftest import recording


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


@respx.mock
def test_the_queue_marks_what_scadbuddy_queued(client: TestClient) -> None:
    """#3741: an item a ScadBuddy run queued (its send), or an item a link names by its
    queue item id, carries ``scadbuddy``. An item queued from an archive ScadBuddy
    linked, with no record of the item, carries none: Bambuddy's own reprint of that
    archive is such an item."""
    configure(client)
    # Item 199 waits again here: only an item that can still print carries the mark.
    queue = [
        {**item, "status": "pending"} if item["id"] == 199 else item
        for item in recording("queue.json")
    ]
    respx.get(f"{API}/queue/").mock(return_value=httpx.Response(200, json=queue))
    state = _state(client)
    output = PrintSubject.output("o" * 32)

    async def seed() -> None:
        await state.print_runs.store.insert_accepted(
            "run-3741",
            subject=output,
            key="run-3741",
            slug="keychain",
            workflow_id="print-run-3741",
            workflow_run_id="w",
            retention=None,
        )
        await state.print_links.record_sends(
            output, [PrintSend(queue_item_id=259, plate_id=1, run_id="run-3741")]
        )
        # Item 257 is queued from archive 144, which ScadBuddy linked by the sliced
        # file's hash; nothing records item 257 itself.
        await state.print_links.record(
            PrintSubject.library(1615), PrintLink(archive_id=144, matched_by="content_hash")
        )
        # Item 199's archive was linked by the item, before sends were recorded.
        await state.print_links.record(
            PrintSubject.library(1630),
            PrintLink(archive_id=130, matched_by="queue_item", queue_item_id=199),
        )

    asyncio.run(seed())

    response = client.get("/api/v1/farm/queue")

    assert response.status_code == 200, response.text
    items = {item["id"]: item for item in response.json()}
    assert items[259]["scadbuddy"] == {
        "subject": "output:" + "o" * 32,
        "output_id": "o" * 32,
        "library_file_id": None,
        "slug": "keychain",
        "run_id": "run-3741",
        "archive_id": None,
        "matched_by": "queue_item",
    }
    assert items[199]["scadbuddy"] == {
        "subject": "library:1630",
        "output_id": None,
        "library_file_id": 1630,
        "slug": None,
        "run_id": None,
        "archive_id": 130,
        "matched_by": "queue_item",
    }
    assert items[257]["scadbuddy"] is None
    assert items[205]["scadbuddy"] is None


@respx.mock
def test_only_items_that_can_still_print_are_looked_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5833: an unfiltered read is mostly history. Only the pending and printing items
    can carry the mark, so only they are looked up, and a finished or skipped item
    ScadBuddy sent carries none."""
    configure(client)
    queue = [
        *recording("queue.json"),
        {**recording("queue.json")[1], "id": 260, "status": "pending"},
        {**recording("queue.json")[1], "id": 261, "status": "skipped"},
    ]
    respx.get(f"{API}/queue/").mock(return_value=httpx.Response(200, json=queue))
    links = _state(client).print_links
    asked: list[list[int]] = []
    jobs = links.jobs

    async def spy(*, queue_items: Any = (), lan_starts: Any = ()) -> JobMatches:
        asked.append(sorted(queue_items))
        return await jobs(queue_items=queue_items, lan_starts=lan_starts)

    monkeypatch.setattr(links, "jobs", spy)
    asyncio.run(
        links.record_sends(
            PrintSubject.output("o" * 32),
            [PrintSend(queue_item_id=205), PrintSend(queue_item_id=261)],
        )
    )

    response = client.get("/api/v1/farm/queue")

    assert response.status_code == 200, response.text
    assert asked == [[257, 259, 260]]
    items = {item["id"]: item for item in response.json()}
    assert items[205]["scadbuddy"] is None
    assert items[261]["scadbuddy"] is None


@respx.mock
def test_the_queue_has_no_marks_when_the_records_are_unreadable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3741: a database blip costs the marks, never the queue."""
    configure(client)
    respx.get(f"{API}/queue/").mock(return_value=httpx.Response(200, json=recording("queue.json")))

    async def broken(**_: object) -> None:
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(_state(client).print_links, "jobs", broken)

    response = client.get("/api/v1/farm/queue")

    assert response.status_code == 200, response.text
    assert all(item["scadbuddy"] is None for item in response.json())


@respx.mock
def test_the_queue_passes_its_filters(client: TestClient) -> None:
    configure(client)
    route = respx.get(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue.json"))
    )

    response = client.get("/api/v1/farm/queue", params={"printer_id": 1, "status": "failed"})

    assert response.status_code == 200
    params = route.calls.last.request.url.params
    assert (params["printer_id"], params["status"]) == ("1", "failed")
    items = response.json()
    assert [item["id"] for item in items] == [257, 259, 199, 205]
    assert items[0]["archive_name"] == "Carrot Garden"
    assert items[0]["printer_name"] == "3DP-31B-598"


@respx.mock
@pytest.mark.parametrize(
    ("route", "bambuddy_path"),
    [
        ("/api/v1/farm/queue", "/queue/"),
        ("/api/v1/farm/stats", "/archives/stats"),
        ("/api/v1/farm/archives", "/archives/"),
        ("/api/v1/farm/inventory", "/inventory/spools"),
    ],
)
def test_a_key_without_read_status_names_the_scope(
    client: TestClient, route: str, bambuddy_path: str
) -> None:
    configure(client)
    respx.get(f"{API}{bambuddy_path}").mock(
        return_value=httpx.Response(403, json={"detail": "Missing permission"})
    )

    response = client.get(route)

    assert response.status_code == 409
    assert response.json()["required_scope"] == "Read Status"


def test_without_bambuddy_the_farm_is_not_configured(client: TestClient) -> None:
    response = client.get("/api/v1/farm/stats")

    assert response.status_code == 409
    assert response.json()["type"].endswith("/bambuddy-not-configured")


@respx.mock
def test_stats_take_a_date_window(client: TestClient) -> None:
    configure(client)
    route = respx.get(f"{API}/archives/stats").mock(
        return_value=httpx.Response(200, json=recording("archive-stats.json"))
    )

    response = client.get(
        "/api/v1/farm/stats", params={"date_from": "2026-09-01", "date_to": "2026-09-30"}
    )

    assert response.status_code == 200
    params = route.calls.last.request.url.params
    assert (params["date_from"], params["date_to"]) == ("2026-09-01", "2026-09-30")
    body = response.json()
    assert body["total_prints"] == 104
    assert body["printer_names"] == {"1": "3DP-31B-598"}


@respx.mock
def test_archives_are_every_archive_with_its_outcome_and_no_file_paths(
    client: TestClient,
) -> None:
    configure(client)
    route = respx.get(f"{API}/archives/").mock(
        return_value=httpx.Response(200, json=recording("archives-full.json"))
    )

    response = client.get(
        "/api/v1/farm/archives",
        params={
            "printer_id": 1,
            "project_id": 4,
            "date_from": "2026-10-01",
            "limit": 3,
            "offset": 3,
        },
    )

    assert response.status_code == 200
    params = route.calls.last.request.url.params
    assert dict(params) == {
        "printer_id": "1",
        "project_id": "4",
        "date_from": "2026-10-01",
        "limit": "3",
        "offset": "3",
    }
    rows = response.json()
    assert [row["status"] for row in rows] == ["printing", "completed", "archived"]
    assert rows[1]["print_name"] == "Bambu Bed Scraper"
    assert "file_path" not in rows[0] and "thumbnail_path" not in rows[0]


def test_an_archive_window_is_at_most_two_hundred(client: TestClient) -> None:
    assert client.get("/api/v1/farm/archives", params={"limit": 201}).status_code == 422


@respx.mock
def test_the_inventory_lists_spools_and_loaded_slots(client: TestClient) -> None:
    configure(client)
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

    response = client.get("/api/v1/farm/inventory", params={"include_archived": True})

    assert response.status_code == 200
    assert spools.calls.last.request.url.params["include_archived"] == "true"
    body = response.json()
    assert len(body["spools"]) == 12
    assert [slot["global_tray_id"] for slot in body["slots"]] == [1, 2, 3, 4, 8]
    assert body["slots"][3]["spool_id"] == 7


@respx.mock
@pytest.mark.parametrize("status", ["queued", "pending&printer_id=2", "../archives"])
def test_a_queue_status_bambuddy_does_not_have_is_refused_before_bambuddy(
    client: TestClient, status: str
) -> None:
    """The route forwards only a fixed set of values to its one fixed endpoint, never
    the caller's free text (security review on #1912)."""
    configure(client)
    route = respx.get(f"{API}/queue/").mock(return_value=httpx.Response(200, json=[]))

    response = client.get("/api/v1/farm/queue", params={"status": status})

    assert response.status_code == 422
    assert not route.called


# --- #785: one spool's summary, for the filament hover card ------------------------


def mock_inventory() -> tuple[respx.Route, respx.Route]:
    return (
        respx.get(f"{API}/inventory/spools").mock(
            return_value=httpx.Response(200, json=recording("inventory-spools.json"))
        ),
        respx.get(f"{API}/inventory/assignments").mock(
            return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
        ),
    )


@respx.mock
def test_a_spool_summary_counts_its_filament_and_prices_it(client: TestClient) -> None:
    configure(client)
    spools, _ = mock_inventory()

    response = client.get("/api/v1/farm/spools/15/summary", params={"grams": 10})

    assert response.status_code == 200, response.text
    # The archived ones too, so an archived spool's own card still resolves.
    assert spools.calls.last.request.url.params["include_archived"] == "true"
    body = response.json()
    # Insignia Clear PETG: spool 15 has 400 g left, spool 14 is full; both loaded.
    assert body["spool"]["remaining_g"] == 400.0
    assert body["spool"]["cost_per_kg"] == 15.96
    assert body["spool"]["loaded"] == {
        "printer_id": 1,
        "printer_name": "3DP-31B-598",
        "ams_id": 0,
        "tray_id": 3,
    }
    assert body["price_per_g"] == pytest.approx(0.01596)
    assert body["remaining_value"] == pytest.approx(6.384)
    assert (body["grams"], body["grams_cost"]) == (10.0, pytest.approx(0.1596))
    filament = body["filament"]
    assert (filament["material"], filament["brand"], filament["color_name"]) == (
        "PETG",
        "Insignia",
        "Clear",
    )
    assert filament["spool_count"] == 2
    assert sorted(filament["spool_ids"]) == [14, 15]
    assert filament["remaining_g"] == 1400.0
    assert sorted((row["spool_id"], row["tray_id"]) for row in filament["loaded"]) == [
        (14, 2),
        (15, 3),
    ]


@respx.mock
def test_no_grams_of_a_priced_spool_cost_nothing_not_null(client: TestClient) -> None:
    """#2918: ``grams=0`` is a plate that uses none of it, which costs 0.0. Only a
    missing price or a missing ``grams`` is ``null``."""
    configure(client)
    mock_inventory()

    body = client.get("/api/v1/farm/spools/15/summary", params={"grams": 0}).json()

    assert (body["grams"], body["grams_cost"]) == (0.0, 0.0)


@respx.mock
def test_the_price_per_gram_is_rounded_like_the_other_prices(client: TestClient) -> None:
    """#2918: 15.96 per kg is 0.01596 per gram, to six places, not a float's tail."""
    configure(client)
    mock_inventory()

    body = client.get("/api/v1/farm/spools/15/summary").json()

    assert body["price_per_g"] == 0.01596
    assert repr(body["price_per_g"]) == "0.01596"


def _filament_rows(*spools: dict[str, object]) -> list[dict[str, object]]:
    base = recording("inventory-spools.json")[0]
    return [{**base, "archived_at": None, **spool} for spool in spools]


@respx.mock
def test_a_spool_known_by_rgba_and_one_known_by_name_are_different_filaments(
    client: TestClient,
) -> None:
    """#2918: the colour is the ``rgba``, else the colour's name; a spool recorded one way
    and one the other are never assumed to be one colour. Spools with neither are one
    colourless filament of their material, subtype and brand."""
    configure(client)
    rows = _filament_rows(
        {"id": 1, "rgba": "F5547CFF", "color_name": None},
        {"id": 2, "rgba": None, "color_name": "Hot Pink"},
        {"id": 3, "rgba": None, "color_name": None},
        {"id": 4, "rgba": None, "color_name": None},
    )
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=rows))
    respx.get(f"{API}/inventory/assignments").mock(return_value=httpx.Response(200, json=[]))

    def filament(spool_id: int) -> list[int]:
        body = client.get(f"/api/v1/farm/spools/{spool_id}/summary").json()
        return sorted(body["filament"]["spool_ids"])

    assert filament(1) == [1]
    assert filament(2) == [2]
    assert filament(3) == [3, 4]


@respx.mock
def test_without_a_price_every_cost_is_null_not_a_guess(client: TestClient) -> None:
    configure(client)
    mock_inventory()

    body = client.get("/api/v1/farm/spools/1/summary", params={"grams": 10}).json()

    assert body["spool"]["cost_per_kg"] is None
    assert (body["price_per_g"], body["remaining_value"], body["grams_cost"]) == (None, None, None)
    assert body["filament"]["spool_count"] == 1
    assert body["filament"]["loaded"] == []
    assert body["spool"]["loaded"] is None


@respx.mock
def test_an_archived_spool_is_summarised_but_not_counted_as_stock(client: TestClient) -> None:
    configure(client)
    rows = recording("inventory-spools.json")
    for row in rows:
        if row["id"] == 14:
            row["archived_at"] = "2026-10-01T00:00:00"
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=rows))
    respx.get(f"{API}/inventory/assignments").mock(return_value=httpx.Response(200, json=[]))

    archived = client.get("/api/v1/farm/spools/14/summary").json()

    assert archived["spool"]["archived"] is True
    assert archived["filament"]["spool_ids"] == [15]
    assert archived["filament"]["remaining_g"] == 400.0


@respx.mock
def test_an_unknown_spool_is_a_404(client: TestClient) -> None:
    configure(client)
    mock_inventory()

    assert client.get("/api/v1/farm/spools/999/summary").status_code == 404


def test_grams_cannot_be_negative(client: TestClient) -> None:
    assert client.get("/api/v1/farm/spools/1/summary", params={"grams": -1}).status_code == 422


def _item(item_id: int, **changes: Any) -> dict[str, Any]:
    """One item of the recorded queue as ``GET /queue/{id}`` answers it: the same
    schema as a row of the list (maziggy/bambuddy f9080d1,
    ``backend/app/api/routes/print_queue.py`` line 1854, ``PrintQueueItemResponse``)."""
    row: dict[str, Any] = next(row for row in recording("queue.json") if row["id"] == item_id)
    return {**row, **changes}


@respx.mock
def test_one_queue_item_is_one_bambuddy_read_and_carries_its_scadbuddy_job(
    client: TestClient,
) -> None:
    """#3744: the item page reads the item alone, never the whole queue, and a job
    ScadBuddy queued carries ``scadbuddy`` in any state, so a finished one still
    names its output and the archive its print page is at."""
    configure(client)
    one = respx.get(f"{API}/queue/205").mock(
        return_value=httpx.Response(200, json=_item(205, ams_mapping=[4, -1]))
    )
    every = respx.get(f"{API}/queue/").mock(return_value=httpx.Response(200, json=[]))
    output = PrintSubject.output("o" * 32)
    asyncio.run(
        _state(client).print_links.record(
            output, PrintLink(archive_id=107, matched_by="queue_item", queue_item_id=205)
        )
    )

    response = client.get("/api/v1/farm/queue/205")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == 205
    assert body["status"] == "completed"
    assert body["ams_mapping"] == [4, -1]
    assert body["scadbuddy"]["output_id"] == "o" * 32
    assert body["scadbuddy"]["archive_id"] == 107
    assert one.call_count == 1
    assert not every.called


@respx.mock
def test_a_queue_item_bambuddy_queued_has_no_scadbuddy_job(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/queue/257").mock(return_value=httpx.Response(200, json=_item(257)))

    response = client.get("/api/v1/farm/queue/257")

    assert response.status_code == 200, response.text
    assert response.json()["scadbuddy"] is None
    assert response.json()["printer_name"] == _item(257)["printer_name"]


@respx.mock
def test_a_gone_queue_item_is_a_404(client: TestClient) -> None:
    """Bambuddy answers 404 "Queue item not found" for an item it no longer has
    (``print_queue.py`` line 1882 at f9080d1); the page says the item is gone."""
    configure(client)
    respx.get(f"{API}/queue/999").mock(
        return_value=httpx.Response(404, json={"detail": "Queue item not found"})
    )

    response = client.get("/api/v1/farm/queue/999")

    assert response.status_code == 404
    assert response.json()["type"].endswith("/bambuddy-not-found")


@respx.mock
def test_reading_one_queue_item_needs_read_status(client: TestClient) -> None:
    """``GET /queue/{id}`` needs ``queue:read_all`` or ``queue:read_own``
    (``print_queue.py`` lines 1858-1862 at f9080d1), which an API key holds with
    ``can_read_status`` (``backend/app/core/auth.py`` lines 89-91), not ``can_queue``."""
    configure(client)
    respx.get(f"{API}/queue/205").mock(
        return_value=httpx.Response(403, json={"detail": "Missing permission"})
    )

    response = client.get("/api/v1/farm/queue/205")

    assert response.status_code == 409
    assert response.json()["required_scope"] == "Read Status"


@respx.mock
def test_one_queue_item_has_no_mark_when_the_records_are_unreadable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(client)
    respx.get(f"{API}/queue/259").mock(return_value=httpx.Response(200, json=_item(259)))

    async def broken(**_: object) -> None:
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(_state(client).print_links, "jobs", broken)

    response = client.get("/api/v1/farm/queue/259")

    assert response.status_code == 200, response.text
    assert response.json()["scadbuddy"] is None
