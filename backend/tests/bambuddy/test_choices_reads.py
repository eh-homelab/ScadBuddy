"""How the print dialog's choices read Bambuddy (#2354, #2361): which reads wait on
which, and which are kept between opens.

A gated route answers only once its partner has been asked, so a read that still waits
on the one before it deadlocks and the test times out, rather than passing slowly."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import ClassVar, cast

import httpx
import pytest
import respx

from scadbuddy.bambuddy import catalogue, choices
from scadbuddy.bambuddy.catalogue import CatalogueStore, cached_catalogue
from scadbuddy.bambuddy.choices import choices_for
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.filaments import gather_plate_options, read_inventory
from scadbuddy.bambuddy.print_source import PrintSource, ReadFile
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from tests.bambuddy.conftest import BASE_URL, recording
from tests.conftest import PgPool

API = f"{BASE_URL}/api/v1"


class _ReadSource:
    """Just what the dialog reads of a source: library file 62, one plate."""

    colours: ClassVar[list[str]] = ["#FF0000"]

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


def _preset_routes() -> tuple[respx.Route, respx.Route]:
    return (
        respx.get(f"{API}/slicer/presets").mock(return_value=_ok("slicer-presets-h2c.json")),
        respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json")),
    )


@respx.mock
async def test_the_printer_reads_do_not_wait_on_the_preset_catalogue(
    bambuddy: BambuddyClient,
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
            bambuddy, _source(), StoredSettings(), remembered=None, printer_id=1
        )

    assert view.printer_id == 1
    assert view.processes["0.4"]
    assert view.filaments.slots


@respx.mock
async def test_the_catalogue_is_read_once_between_opens(bambuddy: BambuddyClient) -> None:
    """#2361: Bambuddy's presets change only on an import there, so a second open
    within the TTL reads none of it again."""
    presets, local = _preset_routes()

    first = await cached_catalogue(bambuddy)
    second = await cached_catalogue(bambuddy)

    assert second == first
    assert presets.call_count == 1
    assert local.call_count == 1


@respx.mock
async def test_an_expired_catalogue_is_served_while_it_is_read_again(
    bambuddy: BambuddyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2978: past the TTL the kept catalogue still answers, and the fresh read runs
    behind it; the open after that read gets what it read."""
    presets, _ = _preset_routes()
    first = await cached_catalogue(bambuddy)

    monkeypatch.setattr(catalogue, "CATALOGUE_TTL", 0.0)
    assert await cached_catalogue(bambuddy) is first
    await catalogue.settle_refreshes()

    assert presets.call_count == 2
    assert await cached_catalogue(bambuddy) is not first


@respx.mock
async def test_an_expired_catalogue_answers_without_waiting_on_bambuddy(
    bambuddy: BambuddyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh hangs until the open has answered: a stale open that waited on it
    would time out here."""
    presets, _ = _preset_routes()
    first = await cached_catalogue(bambuddy)
    answered = asyncio.Event()
    presets.mock(side_effect=_after(answered, "slicer-presets-h2c.json"))

    monkeypatch.setattr(catalogue, "CATALOGUE_TTL", 0.0)
    kept = await asyncio.wait_for(cached_catalogue(bambuddy), 5)
    answered.set()
    await catalogue.settle_refreshes()

    assert kept is first
    assert presets.call_count == 2


@respx.mock
async def test_a_stale_catalogue_is_read_again_once_however_often_it_is_opened(
    bambuddy: BambuddyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    presets, _ = _preset_routes()
    await cached_catalogue(bambuddy)
    answered = asyncio.Event()
    presets.mock(side_effect=_after(answered, "slicer-presets-h2c.json"))

    monkeypatch.setattr(catalogue, "CATALOGUE_TTL", 0.0)
    for _ in range(3):
        await cached_catalogue(bambuddy)
    answered.set()
    await catalogue.settle_refreshes()

    assert presets.call_count == 2


@respx.mock
async def test_a_catalogue_one_replica_read_is_served_to_another(
    bambuddy: BambuddyClient, pg_pool: PgPool
) -> None:
    """#2978: the API runs several processes, and the reopen rarely lands on the one
    that read the catalogue. Another process here is this one with its memory gone."""
    presets, local = _preset_routes()
    store = CatalogueStore(pg_pool)
    first = await cached_catalogue(bambuddy, store)

    catalogue.forget_catalogues()
    again = await cached_catalogue(bambuddy, store)

    assert again == first
    assert presets.call_count == 1
    assert local.call_count == 1


async def test_the_shared_catalogue_never_stores_the_api_key(
    bambuddy: BambuddyClient, pg_pool: PgPool
) -> None:
    with respx.mock:
        _preset_routes()
        await cached_catalogue(bambuddy, CatalogueStore(pg_pool))

    with pg_pool.connection() as conn:
        rows = conn.execute(
            "SELECT key, catalogue::text AS body FROM bambuddy_preset_catalogues"
        ).fetchall()
    assert len(rows) == 1
    assert "s3cret" not in rows[0]["key"] + rows[0]["body"]


@respx.mock
async def test_a_failed_catalogue_read_is_not_kept(bambuddy: BambuddyClient) -> None:
    presets = respx.get(f"{API}/slicer/presets").mock(
        side_effect=[httpx.Response(500), _ok("slicer-presets-h2c.json")]
    )
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))

    with pytest.raises(ApiError):
        await cached_catalogue(bambuddy)
    assert (await cached_catalogue(bambuddy)).process

    assert presets.call_count == 2


@respx.mock
async def test_a_failing_catalogue_still_beats_the_filament_steps_failure(
    bambuddy: BambuddyClient,
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
        await choices_for(bambuddy, _source(), StoredSettings(), remembered=None, printer_id=1)

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
    bambuddy: BambuddyClient, monkeypatch: pytest.MonkeyPatch
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
            bambuddy, _source(), StoredSettings(), remembered=None, printer_id=1
        )

    assert view.last_bed_type is None
    assert view.filaments.slots
