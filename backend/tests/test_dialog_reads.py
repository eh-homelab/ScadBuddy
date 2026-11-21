"""#4196 lever 4: the print dialog's kept Bambuddy reads (`dialog_reads/reads.py`). Time
moves only by hand, so no result depends on how fast the runner is."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest
import respx
from pydantic import ValidationError

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


async def test_waiters_on_a_failed_read_get_its_error_and_read_no_more() -> None:
    """Callers that wait on a read that fails get its error: the same Bambuddy, the same
    key, so a read of their own would fail the same way (#6622)."""
    gate = asyncio.Event()
    calls = 0

    async def read() -> LibraryFile:
        nonlocal calls
        calls += 1
        await gate.wait()
        raise RuntimeError("Bambuddy did not answer")

    reads = DialogReads(clock=Clock())
    callers = [asyncio.create_task(reads.get(CONFIG, "library_file", 89, read)) for _ in range(3)]
    await asyncio.sleep(0)
    gate.set()
    for caller in callers:
        with pytest.raises(RuntimeError):
            await caller
    assert calls == 1


async def _raised[E: Exception](
    read: Callable[[], Awaitable[LibraryFile]], kind: type[E], callers: int = 3
) -> list[E]:
    """What each of ``callers`` raised when they all asked for one key whose read fails:
    the first is the read's own caller, the rest waited on it."""
    gate = asyncio.Event()

    async def gated() -> LibraryFile:
        await gate.wait()
        return await read()

    reads = DialogReads(clock=Clock())
    tasks = [
        asyncio.create_task(reads.get(CONFIG, "library_file", 89, gated)) for _ in range(callers)
    ]
    await asyncio.sleep(0)
    gate.set()
    errors = []
    for task in tasks:
        with pytest.raises(kind) as raised:
            await task
        errors.append(raised.value)
    return errors


async def _unavailable() -> LibraryFile:
    raise ApiError(503, "Bambuddy did not answer", headers={"Retry-After": "5"})


async def test_each_waiter_raises_its_own_copy_of_the_shared_error() -> None:
    """No two callers share one exception object, so no traceback holds another caller's
    frames; each copy keeps the error's class and fields, and names the shared read's
    error as its cause (#6627)."""
    errors = await _raised(_unavailable, ApiError)
    leader, *waiters = errors
    assert len({id(error) for error in errors}) == 3
    for waiter in waiters:
        assert (waiter.status, waiter.detail, waiter.headers) == (
            503,
            leader.detail,
            leader.headers,
        )
        assert waiter.__cause__ is leader
    tracebacks = [error.__traceback__ for error in errors]
    assert len({id(tb) for tb in tracebacks}) == 3


async def test_an_error_that_cannot_be_copied_reaches_each_waiter_as_itself() -> None:
    """A pydantic ``ValidationError`` (Bambuddy sent a row that does not fit the model)
    cannot be rebuilt from its args, so each waiter raises the shared error itself, never
    a ``TypeError`` from the copy."""

    async def misfit() -> LibraryFile:
        return LibraryFile.model_validate({})

    await _raised(misfit, ValidationError)


async def test_a_waiters_change_to_its_errors_fields_reaches_no_other_waiter() -> None:
    errors = await _raised(_unavailable, ApiError)
    errors[1].headers["Retry-After"] = "60"
    assert [error.headers["Retry-After"] for error in errors] == ["5", "60", "5"]


@respx.mock
async def test_a_transport_failure_reaches_each_waiter_as_the_clients_api_error() -> None:
    """The client maps an httpx failure to an `ApiError` inside the read (#6634), so a
    waiter never sees a raw httpx error: each gets the leader's status, its detail (which
    names the transport error's class) and its ``upstream`` (which holds the transport
    reason, such as "could not connect")."""
    respx.get(f"{API}/library/files/89").mock(side_effect=httpx.ConnectError("refused"))
    async with _client(DialogReads(clock=Clock())) as client:
        errors = await _raised(lambda: client.library_file(89), ApiError)
    leader, *waiters = errors
    assert isinstance(leader.__cause__, httpx.ConnectError)
    assert leader.upstream is not None
    for waiter in waiters:
        assert (waiter.status, waiter.detail) == (leader.status, leader.detail)
        assert waiter.upstream == leader.upstream
        assert waiter.upstream is not leader.upstream
        assert waiter.__cause__ is leader


async def test_waiters_on_a_cancelled_read_share_one_new_read() -> None:
    """A shared read whose own caller left ends with no answer; the callers that waited on
    it share one new read (#6622)."""
    gates = [asyncio.Event(), asyncio.Event()]
    calls = 0

    async def read() -> LibraryFile:
        nonlocal calls
        calls += 1
        await gates[calls - 1].wait()
        return _file()

    reads = DialogReads(clock=Clock())
    first = asyncio.create_task(reads.get(CONFIG, "library_file", 89, read))
    await asyncio.sleep(0)
    waiters = [asyncio.create_task(reads.get(CONFIG, "library_file", 89, read)) for _ in range(3)]
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    for _ in range(3):
        await asyncio.sleep(0)
    gates[1].set()
    assert await asyncio.gather(*waiters) == [_file()] * 3
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
