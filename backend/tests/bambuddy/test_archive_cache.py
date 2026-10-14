"""#308: archive reads are kept 30 s per Bambuddy and archive, and no longer."""

from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest
import respx

from scadbuddy.bambuddy.archive_cache import TIMELAPSE_TTL, ArchiveCache
from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import recording

BASE = "https://bambuddy.test"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def client(base: str = BASE) -> BambuddyClient:
    return BambuddyClient(BambuddyConfig(base_url=base, api_key="k"))


@respx.mock
async def test_an_archive_is_read_again_after_the_ttl(clock: Clock) -> None:
    route = respx.get(f"{BASE}/api/v1/archives/35").mock(
        return_value=httpx.Response(200, json=recording("archive-detail.json"))
    )
    cache = ArchiveCache(30.0, clock=clock)
    async with client() as bambuddy:
        await cache.archive(bambuddy, 35)
        clock.now = 29.9
        await cache.archive(bambuddy, 35)
        assert route.call_count == 1
        clock.now = 30.0
        await cache.archive(bambuddy, 35)
    assert route.call_count == 2


@respx.mock
async def test_a_deleted_archive_is_an_answer_and_is_kept(clock: Clock) -> None:
    route = respx.get(f"{BASE}/api/v1/archives/35").mock(return_value=httpx.Response(404))
    cache = ArchiveCache(30.0, clock=clock)
    async with client() as bambuddy:
        assert await cache.archive(bambuddy, 35) is None
        assert await cache.archive(bambuddy, 35) is None
    assert route.call_count == 1


@respx.mock
async def test_a_failed_read_is_not_kept(clock: Clock) -> None:
    route = respx.get(f"{BASE}/api/v1/archives/35").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json=recording("archive-detail.json")),
        ]
    )
    cache = ArchiveCache(30.0, clock=clock)
    async with client() as bambuddy:
        with pytest.raises(ApiError):
            await cache.archive(bambuddy, 35)
        assert (await cache.archive(bambuddy, 35)) is not None
    assert route.call_count == 2


@respx.mock
async def test_each_bambuddy_has_its_own_entries(clock: Clock) -> None:
    for base in (BASE, "https://other.test"):
        respx.get(f"{base}/api/v1/archives/35").mock(
            return_value=httpx.Response(200, json=recording("archive-detail.json"))
        )
    cache = ArchiveCache(30.0, clock=clock)
    async with client() as one, client("https://other.test") as two:
        await cache.archive(one, 35)
        await cache.archive(two, 35)
    assert len(respx.calls) == 2


@respx.mock
async def test_the_cache_is_bounded(clock: Clock) -> None:
    respx.get(url__regex=rf"{BASE}/api/v1/archives/\d+$").mock(
        return_value=httpx.Response(200, json=recording("archive-detail.json"))
    )
    cache = ArchiveCache(30.0, clock=clock, max_entries=2)
    async with client() as bambuddy:
        for archive_id in (1, 2, 3):
            await cache.archive(bambuddy, archive_id)
        await cache.archive(bambuddy, 3)
        assert len(respx.calls) == 3, "the newest is still kept"
        await cache.archive(bambuddy, 1)
    assert len(respx.calls) == 4, "the oldest was dropped"


@respx.mock
async def test_concurrent_misses_share_one_read(clock: Clock) -> None:
    """#609 review: a list and a detail opened together do not both read Bambuddy."""
    route = respx.get(f"{BASE}/api/v1/archives/35").mock(
        return_value=httpx.Response(200, json=recording("archive-detail.json"))
    )
    cache = ArchiveCache(30.0, clock=clock)
    async with client() as bambuddy:
        first, second = await asyncio.gather(
            cache.archive(bambuddy, 35), cache.archive(bambuddy, 35)
        )
    assert first == second
    assert route.call_count == 1


@respx.mock
async def test_a_read_in_flight_when_forgotten_is_not_kept(clock: Clock) -> None:
    route = respx.get(f"{BASE}/api/v1/archives/35").mock(
        return_value=httpx.Response(200, json=recording("archive-detail.json"))
    )
    cache = ArchiveCache(30.0, clock=clock)
    async with client() as bambuddy:
        pending = asyncio.ensure_future(cache.archive(bambuddy, 35))
        await asyncio.sleep(0)
        cache.forget(bambuddy, 35)
        await pending
        await cache.archive(bambuddy, 35)
    assert route.call_count == 2


@respx.mock
async def test_prime_keeps_what_one_list_read_holds(clock: Clock) -> None:
    """#2359: a page's archives from one list read; the rest are read singly."""
    detail = recording("archive-detail.json")
    listed = respx.get(f"{BASE}/api/v1/archives/").mock(
        return_value=httpx.Response(200, json=[{**detail, "id": 35}, {**detail, "id": 99}])
    )
    single = respx.get(f"{BASE}/api/v1/archives/35").mock(return_value=httpx.Response(500))
    other = respx.get(f"{BASE}/api/v1/archives/36").mock(
        return_value=httpx.Response(200, json={**detail, "id": 36})
    )
    cache = ArchiveCache(30.0, clock=clock)
    async with client() as bambuddy:
        await cache.prime(
            bambuddy, [35, 36], date_from=date(2026, 9, 26), date_to=date(2026, 9, 28), limit=8
        )
        found = await cache.archive(bambuddy, 35)
        assert found is not None and found.id == 35
        assert (await cache.archive(bambuddy, 36)) is not None
        # One missing archive is no cheaper by a list read.
        clock.now = 31.0
        await cache.archive(bambuddy, 36)
        clock.now = 62.0
        await cache.prime(bambuddy, [35], date_from=None, date_to=None, limit=8)
    assert listed.call_count == 1
    params = listed.calls.last.request.url.params
    assert (params["date_from"], params["date_to"], params["limit"]) == (
        "2026-09-26",
        "2026-09-28",
        "8",
    )
    assert not single.called
    assert other.call_count == 2


@respx.mock
async def test_the_printers_are_kept_for_the_ttl(clock: Clock) -> None:
    route = respx.get(f"{BASE}/api/v1/printers/").mock(
        return_value=httpx.Response(200, json=recording("printers.json"))
    )
    cache = ArchiveCache(30.0, clock=clock)
    async with client() as bambuddy:
        assert [p.name for p in await cache.printers(bambuddy)] == ["3DP-31B-598"]
        await cache.printers(bambuddy)
    assert route.call_count == 1


@respx.mock
async def test_a_timelapse_is_kept_for_hours_by_its_path(clock: Clock) -> None:
    """#2360: Bambuddy runs ffmpeg/ffprobe on every read, 20-26 s for one print, and a
    timelapse does not change once made; a different one is a different path."""
    thumbnails = respx.get(f"{BASE}/api/v1/archives/35/timelapse/thumbnails").mock(
        return_value=httpx.Response(200, json=recording("timelapse-thumbnails.json"))
    )
    info = respx.get(f"{BASE}/api/v1/archives/35/timelapse/info").mock(
        return_value=httpx.Response(200, json=recording("timelapse-info.json"))
    )
    cache = ArchiveCache(30.0, clock=clock)
    async with client() as bambuddy:
        frames = await cache.timelapse_frames(bambuddy, 35, "a.mp4")
        await cache.timelapse_info(bambuddy, 35, "a.mp4")
        assert frames[0][1].startswith(b"\xff\xd8"), "decoded to the JPEG"
        clock.now = TIMELAPSE_TTL - 1
        await cache.timelapse_frames(bambuddy, 35, "a.mp4")
        await cache.timelapse_info(bambuddy, 35, "a.mp4")
        assert (thumbnails.call_count, info.call_count) == (1, 1)
        await cache.timelapse_frames(bambuddy, 35, "b.mp4")
        assert thumbnails.call_count == 2
        clock.now = TIMELAPSE_TTL
        await cache.timelapse_frames(bambuddy, 35, "a.mp4")
    assert thumbnails.call_count == 3


@respx.mock
async def test_a_failed_timelapse_read_is_not_kept(clock: Clock) -> None:
    route = respx.get(f"{BASE}/api/v1/archives/35/timelapse/thumbnails").mock(
        return_value=httpx.Response(500)
    )
    cache = ArchiveCache(30.0, clock=clock)
    async with client() as bambuddy:
        with pytest.raises(ApiError):
            await cache.timelapse_frames(bambuddy, 35, "a.mp4")
        route.mock(return_value=httpx.Response(200, json=recording("timelapse-thumbnails.json")))
        assert await cache.timelapse_frames(bambuddy, 35, "a.mp4")
