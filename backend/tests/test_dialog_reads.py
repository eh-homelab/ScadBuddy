"""#4196 lever 4: the print dialog's kept Bambuddy reads (`dialog_reads/reads.py`). Time
moves only by hand, so no result depends on how fast the runner is."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyConfig
from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.core.problems import ApiError
from scadbuddy.dialog_reads.reads import DIALOG_TTL, DialogClient, DialogReads
from tests.bambuddy.conftest import recording

BASE = "https://bambuddy.example"
API = f"{BASE}/api/v1"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _client(reads: DialogReads, *, base: str = BASE, key: str = "k") -> DialogClient:
    return DialogClient(BambuddyConfig(base_url=base, api_key=key), reads)


def _printers(base: str = BASE) -> respx.Route:
    return respx.get(f"{base}/api/v1/printers/").mock(
        return_value=httpx.Response(200, json=recording("printers.json"))
    )


@respx.mock
async def test_each_kept_read_is_asked_once_and_live_state_every_time() -> None:
    """The printer list, a printer, a library file and its plates are read once for a
    second dialog read; the printer's status is read each time."""
    listed = _printers()
    one = respx.get(f"{API}/printers/1").mock(
        return_value=httpx.Response(200, json=recording("printers.json")[0])
    )
    file = respx.get(f"{API}/library/files/89").mock(
        return_value=httpx.Response(200, json={"id": 89, "filename": "a.3mf", "file_type": "3mf"})
    )
    plates = respx.get(f"{API}/library/files/89/plates").mock(
        return_value=httpx.Response(200, json=recording("library-plates-single.json"))
    )
    status = respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=recording("printer-status-rack.json"))
    )
    reads = DialogReads(clock=Clock())
    for _ in range(2):
        async with _client(reads) as client:
            await client.printers()
            await client.printer(1)
            await client.library_file(89)
            await client.library_plates(89)
            await client.printer_status(1)

    assert [listed.call_count, one.call_count, file.call_count, plates.call_count] == [1] * 4
    assert status.call_count == 2


@respx.mock
async def test_a_read_is_asked_again_once_the_ttl_has_passed() -> None:
    listed = _printers()
    clock = Clock()
    reads = DialogReads(clock=clock)
    async with _client(reads) as client:
        await client.printers()
        clock.now = DIALOG_TTL - 0.001
        await client.printers()
        assert listed.call_count == 1
        clock.now = DIALOG_TTL
        await client.printers()
    assert listed.call_count == 2


@respx.mock
async def test_another_key_or_bambuddy_reads_for_itself() -> None:
    """Kept by the base URL and the key's scope: a key of other rights, or another
    Bambuddy, never gets a read that was made for this one."""
    here, there = _printers(), _printers("https://other.example")
    reads = DialogReads(clock=Clock())
    async with _client(reads) as client:
        await client.printers()
    async with _client(reads, key="other") as client:
        await client.printers()
    async with _client(reads, base="https://other.example") as client:
        await client.printers()
    assert (here.call_count, there.call_count) == (2, 1)


@respx.mock
async def test_a_failed_read_is_not_kept() -> None:
    listed = respx.get(f"{API}/printers/").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json=recording("printers.json")),
        ]
    )
    reads = DialogReads(clock=Clock())
    async with _client(reads) as client:
        with pytest.raises(ApiError):
            await client.printers()
        assert await client.printers()
    assert listed.call_count == 2


@respx.mock
async def test_a_callers_change_to_an_answer_reaches_no_other_caller() -> None:
    _printers()
    reads = DialogReads(clock=Clock())
    async with _client(reads) as client:
        first = await client.printers()
        name = first[0].name
        first[0].name = "changed"
        first.clear()
        again = await client.printers()
    assert again and again[0].name == name


@respx.mock
async def test_the_oldest_read_goes_once_the_cache_is_full() -> None:
    routes = {
        file_id: respx.get(f"{API}/library/files/{file_id}").mock(
            return_value=httpx.Response(
                200, json={"id": file_id, "filename": "a.3mf", "file_type": "3mf"}
            )
        )
        for file_id in (1, 2, 3)
    }
    reads = DialogReads(clock=Clock(), max_entries=2)
    async with _client(reads) as client:
        for file_id in (1, 2, 3, 2, 1):
            await client.library_file(file_id)
    # 3 pushed 1 out; 2 was still kept; 1 was read again.
    assert {file_id: route.call_count for file_id, route in routes.items()} == {1: 2, 2: 1, 3: 1}


CONFIG = BambuddyConfig(base_url=BASE, api_key="k")


def _file(file_id: int = 89) -> LibraryFile:
    return LibraryFile.model_validate({"id": file_id, "filename": "a.3mf", "file_type": "3mf"})


async def test_two_misses_at_once_read_once() -> None:
    """``choices`` and a check sent together share one read (#6618)."""
    gate = asyncio.Event()
    calls = 0

    async def read() -> LibraryFile:
        nonlocal calls
        calls += 1
        await gate.wait()
        return _file()

    reads = DialogReads(clock=Clock())
    first = asyncio.create_task(reads.get(CONFIG, "library_file", 89, read))
    second = asyncio.create_task(reads.get(CONFIG, "library_file", 89, read))
    await asyncio.sleep(0)
    gate.set()
    a, b = await asyncio.gather(first, second)
    assert calls == 1
    assert a == b == _file()
    assert a is not b


async def test_a_waiter_reads_for_itself_when_the_shared_read_fails() -> None:
    gate = asyncio.Event()
    calls = 0

    async def read() -> LibraryFile:
        nonlocal calls
        calls += 1
        if calls == 1:
            await gate.wait()
            raise RuntimeError("Bambuddy did not answer")
        return _file()

    reads = DialogReads(clock=Clock())
    first = asyncio.create_task(reads.get(CONFIG, "library_file", 89, read))
    second = asyncio.create_task(reads.get(CONFIG, "library_file", 89, read))
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(RuntimeError):
        await first
    assert await second == _file()
    assert calls == 2


async def test_a_cancelled_waiter_leaves_the_shared_read_alone() -> None:
    gate = asyncio.Event()

    async def read() -> LibraryFile:
        await gate.wait()
        return _file()

    reads = DialogReads(clock=Clock())
    first = asyncio.create_task(reads.get(CONFIG, "library_file", 89, read))
    second = asyncio.create_task(reads.get(CONFIG, "library_file", 89, read))
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    gate.set()
    assert await first == _file()
