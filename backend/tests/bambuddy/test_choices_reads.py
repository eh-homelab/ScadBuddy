"""How the print dialog's choices read Bambuddy (#2354, #2361): which reads wait on
which, and which are kept between opens.

A gated route answers only once its partner has been asked, so a read that still waits
on the one before it deadlocks and the test times out, rather than passing slowly."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar, cast

import httpx
import pytest
import respx

from scadbuddy.bambuddy import catalogue, choices
from scadbuddy.bambuddy.catalogue import cached_catalogue
from scadbuddy.bambuddy.choices import choices_for
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.filaments import gather_plate_options, read_inventory
from scadbuddy.bambuddy.print_source import PrintSource, ReadFile
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"


class _ReadSource:
    """Just what the dialog reads of a source: library file 62, one plate."""

    colours: ClassVar[list[str]] = ["#FF0000"]
    is_sliced = False  # #2671: a project to lay out, not a sliced file
    sliced_profile = None

    async def file_to_read(self, client: BambuddyClient) -> ReadFile:
        return ReadFile(62)

    async def plate_ids(self, client: BambuddyClient) -> list[int]:
        return [1]


async def _source() -> PrintSource:
    return cast(PrintSource, _ReadSource())


def _ok(name: str) -> httpx.Response:
    return httpx.Response(200, json=recording(name))


def _after(asked: asyncio.Event, name: str) -> Callable[[httpx.Request], Awaitable[httpx.Response]]:
    """A route that answers only once ``asked`` is set."""

    async def answer(request: httpx.Request) -> httpx.Response:
        await asked.wait()
        return _ok(name)

    return answer


def _marks(asked: asyncio.Event, name: str) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        asked.set()
        return _ok(name)

    return answer


def _filament_routes() -> None:
    respx.get(f"{API}/inventory/spools").mock(return_value=_ok("inventory-spools.json"))
    respx.get(f"{API}/inventory/assignments").mock(return_value=_ok("inventory-assignments.json"))
    respx.get(f"{API}/library/files/62/filament-requirements").mock(
        return_value=_ok("filament-requirements.json")
    )
    respx.get(f"{API}/printers/1").mock(return_value=_ok("printer.json"))
    respx.get(f"{API}/printers/1/inventory-remain").mock(return_value=_ok("inventory-remain.json"))
    respx.get(f"{API}/archives/").mock(return_value=_ok("archives.json"))


def _expire(cache: Path) -> None:
    """Age the kept catalogue past the TTL. Backdated rather than a TTL of 0, which a
    wall clock that steps back (WSL's does) reads as a file not yet written."""
    (kept,) = (cache / catalogue.CATALOGUE_DIRNAME).iterdir()
    os.utime(kept, (0, 0))


def _preset_routes() -> tuple[respx.Route, respx.Route]:
    return (
        respx.get(f"{API}/slicer/presets").mock(return_value=_ok("slicer-presets-h2c.json")),
        respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json")),
    )


@respx.mock
async def test_the_printer_reads_do_not_wait_on_the_preset_catalogue(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#2354: the catalogue took 6 s in production and the second wave waited on it.
    Here it answers only once the printer's status has been asked for."""
    _filament_routes()
    respx.get(f"{API}/printers/").mock(return_value=_ok("printers.json"))
    status_asked = asyncio.Event()
    respx.get(f"{API}/printers/1/status").mock(
        side_effect=_marks(status_asked, "printer-status-rack.json")
    )
    respx.get(f"{API}/slicer/presets").mock(
        side_effect=_after(status_asked, "slicer-presets-h2c.json")
    )
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))

    async with asyncio.timeout(5):
        view = await choices_for(
            bambuddy, _source(), StoredSettings(), remembered=None, printer_id=1, cache=tmp_path
        )

    assert view.printer_id == 1
    assert view.processes["0.4"]
    assert view.filaments.slots


@respx.mock
async def test_the_catalogue_is_read_once_between_opens(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#2361: Bambuddy's presets change only on an import there, so a second open
    within the TTL reads none of it again."""
    presets, local = _preset_routes()

    first = await cached_catalogue(bambuddy, tmp_path)
    second = await cached_catalogue(bambuddy, tmp_path)

    assert second == first
    assert presets.call_count == 1
    assert local.call_count == 1


@respx.mock
async def test_every_replica_reads_the_kept_catalogue(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#2978: kept in one process, the catalogue missed almost every open across the
    API's replicas. A process that has parsed nothing still reads the shared file."""
    presets, _ = _preset_routes()
    first = await cached_catalogue(bambuddy, tmp_path)

    catalogue.forget_catalogues()
    second = await cached_catalogue(bambuddy, tmp_path)

    assert second == first
    assert presets.call_count == 1


@respx.mock
async def test_an_expired_catalogue_answers_at_once_and_is_read_again_behind_it(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#2978: the open does not wait on the fresh read; the next open gets it."""
    _, local = _preset_routes()
    first = await cached_catalogue(bambuddy, tmp_path)
    _expire(tmp_path)
    asked = asyncio.Event()
    respx.get(f"{API}/slicer/presets").mock(side_effect=_after(asked, "slicer-presets-h2c.json"))

    async with asyncio.timeout(5):
        stale = await cached_catalogue(bambuddy, tmp_path)
    assert stale == first
    assert len(catalogue._refreshing) == 1
    # A second open while the read runs starts no other.
    await cached_catalogue(bambuddy, tmp_path)
    assert len(catalogue._refreshing) == 1

    asked.set()
    async with asyncio.timeout(5):
        await asyncio.gather(*catalogue._refreshing.values())
    assert local.call_count == 2
    assert not catalogue._refreshing
    assert catalogue._load(next((tmp_path / catalogue.CATALOGUE_DIRNAME).iterdir()))


@respx.mock
async def test_a_failed_refresh_keeps_the_old_catalogue(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    presets, _ = _preset_routes()
    first = await cached_catalogue(bambuddy, tmp_path)
    _expire(tmp_path)
    presets.mock(return_value=httpx.Response(500))

    assert await cached_catalogue(bambuddy, tmp_path) == first
    await asyncio.gather(*catalogue._refreshing.values())

    assert presets.call_count == 2
    catalogue.forget_catalogues()
    assert await cached_catalogue(bambuddy, tmp_path) == first


@respx.mock
async def test_an_unreadable_kept_catalogue_is_read_again(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    presets, _ = _preset_routes()
    await cached_catalogue(bambuddy, tmp_path)
    (kept,) = (tmp_path / catalogue.CATALOGUE_DIRNAME).iterdir()
    kept.write_text("{", encoding="utf-8")
    catalogue.forget_catalogues()

    assert (await cached_catalogue(bambuddy, tmp_path)).process
    assert presets.call_count == 2


@respx.mock
async def test_a_catalogue_that_cannot_be_kept_still_answers(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """The volume is a cache: a full or read-only one costs the next open a read."""
    _preset_routes()
    (tmp_path / catalogue.CATALOGUE_DIRNAME).write_text("not a directory", encoding="utf-8")

    assert (await cached_catalogue(bambuddy, tmp_path)).process


@respx.mock
async def test_a_failed_catalogue_read_is_not_kept(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    presets = respx.get(f"{API}/slicer/presets").mock(
        side_effect=[httpx.Response(500), _ok("slicer-presets-h2c.json")]
    )
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))

    with pytest.raises(ApiError):
        await cached_catalogue(bambuddy, tmp_path)
    assert not (tmp_path / catalogue.CATALOGUE_DIRNAME).exists()
    assert (await cached_catalogue(bambuddy, tmp_path)).process

    assert presets.call_count == 2


@respx.mock
async def test_a_failing_catalogue_still_beats_the_filament_steps_failure(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """The presets are read beside the second wave now, and their failure still
    surfaces first, as when they were read before it."""
    respx.get(f"{API}/printers/").mock(return_value=_ok("printers.json"))
    respx.get(f"{API}/printers/1/status").mock(return_value=_ok("printer-status-rack.json"))
    respx.get(f"{API}/archives/").mock(return_value=_ok("archives.json"))
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(500))
    respx.get(f"{API}/inventory/assignments").mock(return_value=httpx.Response(500))
    respx.get(f"{API}/slicer/presets").mock(return_value=httpx.Response(500))
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))

    with pytest.raises(ApiError) as raised:
        await choices_for(
            bambuddy,
            _source(),
            StoredSettings(),
            remembered=None,
            printer_id=1,
            cache=tmp_path,
        )

    assert "slicer presets" in raised.value.detail


@respx.mock
async def test_the_spools_and_assignments_are_read_together(bambuddy: BambuddyClient) -> None:
    """#2354: the assignments waited for the spools."""
    asked = asyncio.Event()
    respx.get(f"{API}/inventory/spools").mock(side_effect=_after(asked, "inventory-spools.json"))
    respx.get(f"{API}/inventory/assignments").mock(
        side_effect=_marks(asked, "inventory-assignments.json")
    )

    async with asyncio.timeout(5):
        spools, assignments = await read_inventory(bambuddy)

    assert spools and assignments


@respx.mock
async def test_the_printer_and_its_remaining_filament_are_read_together(
    bambuddy: BambuddyClient,
) -> None:
    """#2354: the printer, then its inventory-remain, then the plate's slots ran one
    after another."""
    _filament_routes()
    asked = asyncio.Event()
    respx.get(f"{API}/printers/1").mock(side_effect=_after(asked, "printer.json"))
    respx.get(f"{API}/printers/1/inventory-remain").mock(
        side_effect=_marks(asked, "inventory-remain.json")
    )

    async with asyncio.timeout(5):
        (options,) = await gather_plate_options(
            bambuddy, library_file_id=62, printer_id=1, plate_ids=[1]
        )

    assert options.slots


@respx.mock
async def test_stalled_archives_cost_the_plate_preselection_not_the_dialog(
    bambuddy: BambuddyClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2354: a hung archives list rode out the client's 30 s and the dialog was a 504."""
    monkeypatch.setattr(choices, "ARCHIVES_BUDGET", 0.05)
    _filament_routes()
    _preset_routes()
    respx.get(f"{API}/printers/").mock(return_value=_ok("printers.json"))
    respx.get(f"{API}/printers/1/status").mock(return_value=_ok("printer-status-rack.json"))
    never = asyncio.Event()
    respx.get(f"{API}/archives/").mock(side_effect=_after(never, "archives.json"))

    async with asyncio.timeout(5):
        view = await choices_for(
            bambuddy, _source(), StoredSettings(), remembered=None, printer_id=1, cache=tmp_path
        )

    assert view.last_bed_type is None
    assert view.filaments.slots
