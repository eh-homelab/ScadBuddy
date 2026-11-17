"""The per-app assignments cache (#6580): its state is its own, and a read is single-flight."""

from __future__ import annotations

import asyncio

import httpx
import respx

from scadbuddy.filament.assignments_cache import AssignmentsCache
from scadbuddy.filament.bambuddy_pull import try_assignments
from scadbuddy.library.settings_store import StoredSettings

SETTINGS = StoredSettings(bambuddy_url="https://bambuddy.test", bambuddy_api_key="bb_test")
PLACE = {"id": 1, "spool_id": 15, "printer_id": 2, "ams_id": 0, "tray_id": 1}


@respx.mock
async def test_two_caches_do_not_share_what_was_read() -> None:
    route = respx.get("https://bambuddy.test/api/v1/inventory/assignments").mock(
        return_value=httpx.Response(200, json=[PLACE | {"printer_name": "W"}])
    )
    one, two = AssignmentsCache(), AssignmentsCache()
    assert len(await one.get(SETTINGS, try_assignments)) == 1
    assert route.call_count == 1
    assert two.seen is None
    assert len(await two.get(SETTINGS, try_assignments)) == 1
    assert route.call_count == 2


@respx.mock
async def test_callers_during_a_read_wait_for_that_one_read() -> None:
    release = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200, json=[PLACE | {"printer_name": "W"}])

    route = respx.get("https://bambuddy.test/api/v1/inventory/assignments").mock(side_effect=slow)
    cache = AssignmentsCache()
    callers = [asyncio.create_task(cache.get(SETTINGS, try_assignments)) for _ in range(3)]
    await asyncio.sleep(0.1)
    assert not any(c.done() for c in callers)
    release.set()
    results = await asyncio.wait_for(asyncio.gather(*callers), 5)
    assert all(len(r) == 1 for r in results)
    assert route.call_count == 1
