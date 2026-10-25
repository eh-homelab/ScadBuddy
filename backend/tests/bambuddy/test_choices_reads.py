"""How the print dialog's choices and its check read Bambuddy (#2354, #2361, #4196):
which reads wait on which, and which are kept between opens.

A gated route answers only once its partner has been asked, so a read that still waits
on the one before it deadlocks and the test times out, rather than passing slowly."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import hmac
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import ClassVar, cast

import httpx
import pytest
import respx

from scadbuddy.bambuddy import catalogue, choices
from scadbuddy.bambuddy.catalogue import cached_catalogue
from scadbuddy.bambuddy.choices import choices_for
from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM, is_transient
from scadbuddy.bambuddy.filaments import gather_plate_options, read_inventory
from scadbuddy.bambuddy.print_run import PrintRunRequest, check_print
from scadbuddy.bambuddy.print_source import PrintSource, ReadFile
from scadbuddy.core.problems import ApiError
from scadbuddy.core.secrets import Kek
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.slicing.catalogue import catalogue_for
from scadbuddy.slicing.profiles import Profiles
from tests.bambuddy.conftest import BASE_URL, recording
from tests.slicing.conftest import PROFILES

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


async def _refreshes_done() -> None:
    """Wait for this process's background refreshes: the one place these tests reach
    into the module's running reads (#4717 review)."""
    await asyncio.gather(*catalogue._refreshing.values())


class _HeldPresets:
    """/slicer/presets held until ``release`` is set, counting the reads as they
    start: respx counts a call only once it has answered."""

    def __init__(self, answer: Callable[[], httpx.Response]) -> None:
        self.started = 0
        self.asked = asyncio.Event()
        self.release = asyncio.Event()
        self._answer = answer

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.started += 1
        self.asked.set()
        await self.release.wait()
        return self._answer()


def _count_shields(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], Awaitable[None]]:
    """A wait until ``n`` opens are waiting on the shared first read (#3809 review). An open
    awaits the shield the moment it makes it, so counting shields counts waiting opens,
    with no guess at how long that takes."""
    shields = 0
    real = asyncio.shield

    def counting(arg: Awaitable[catalogue.Catalogue]) -> asyncio.Future[catalogue.Catalogue]:
        nonlocal shields
        shields += 1
        return real(arg)

    monkeypatch.setattr(asyncio, "shield", counting)

    async def until(n: int) -> None:
        async with asyncio.timeout(5):
            while shields < n:
                await asyncio.sleep(0)

    return until


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
            bambuddy,
            _source(),
            StoredSettings(),
            remembered=None,
            printer_id=1,
            cache=tmp_path,
            catalogue_key=None,
        )

    assert view.printer_id == 1
    assert view.processes["0.4"]
    assert view.filaments.slots


@respx.mock
async def test_a_native_catalogue_reads_no_presets_from_bambuddy(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#3818 tier A: given the image's profiles, the dialog's presets are theirs, and
    neither preset route is asked (respx refuses any route not mocked here)."""
    _filament_routes()
    respx.get(f"{API}/printers/").mock(return_value=_ok("printers.json"))
    respx.get(f"{API}/printers/1/status").mock(return_value=_ok("printer-status-rack.json"))

    view = await choices_for(
        bambuddy,
        _source(),
        StoredSettings(),
        remembered=None,
        printer_id=1,
        cache=tmp_path,
        catalogue_key=None,
        native=Profiles(PROFILES),
    )

    assert view.processes["0.4"] == ["0.20mm Standard @BBL H2C"]
    assert [row.name for row in view.filament_presets["0.4"]] == ["Bambu PLA Basic @BBL H2C"]
    assert not (tmp_path / catalogue.CATALOGUE_DIRNAME).exists()


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
        await _refreshes_done()
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
    await _refreshes_done()

    assert presets.call_count == 2
    catalogue.forget_catalogues()
    assert await cached_catalogue(bambuddy, tmp_path) == first


@respx.mock
async def test_a_refresh_whose_local_presets_read_fails_keeps_the_old_catalogue(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#3422: either read failing fails the refresh. A refresh that kept the presets
    without the local ones would share a smaller catalogue as a fresh one."""
    _, local = _preset_routes()
    first = await cached_catalogue(bambuddy, tmp_path)
    (kept,) = (tmp_path / catalogue.CATALOGUE_DIRNAME).iterdir()
    _expire(tmp_path)
    local.mock(return_value=httpx.Response(500))

    assert await cached_catalogue(bambuddy, tmp_path) == first
    await _refreshes_done()

    assert local.call_count == 2
    assert kept.stat().st_mtime == 0  # not rewritten
    catalogue.forget_catalogues()
    assert await cached_catalogue(bambuddy, tmp_path) == first


@respx.mock
async def test_a_kept_catalogue_is_named_by_an_hmac_under_the_kek(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#3313: a listing of the shared volume names no hash a guessed API key can be
    checked against without the KEK."""
    _preset_routes()
    kek = Kek(id="k1", key=b"\x01" * 32)
    key = catalogue.catalogue_name_key(kek)
    assert key is not None and catalogue.catalogue_name_key(None) is None
    assert catalogue.catalogue_name_key(Kek(id="k1", key=b"\x01" * 32)) == key

    await cached_catalogue(bambuddy, tmp_path, name_key=key)

    (kept,) = (tmp_path / catalogue.CATALOGUE_DIRNAME).iterdir()
    message = f"{bambuddy.config.base_url}\0{bambuddy.config.api_key or ''}".encode()
    assert kept.name == f"v1-{hmac.new(key, message, hashlib.sha256).hexdigest()[:32]}.json"
    assert kept.name != catalogue._path(tmp_path, bambuddy.config).name


@respx.mock
async def test_a_keep_removes_the_files_left_unwritten_past_the_orphan_age(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#3423: a file for another URL, key or version, or a crashed write, goes once it
    has sat unwritten for CATALOGUE_ORPHAN_AGE; a recent one stays."""
    _preset_routes()
    directory = tmp_path / catalogue.CATALOGUE_DIRNAME
    directory.mkdir()
    old = time.time() - catalogue.CATALOGUE_ORPHAN_AGE - 60
    orphans = [directory / "v1-0123456789abcdef0123456789abcdef.json", directory / ".v0-x.json.1f"]
    for orphan in orphans:
        orphan.write_text("{}", encoding="utf-8")
        os.utime(orphan, (old, old))
    # The file this Bambuddy's catalogue was kept in under the KEK before a rotation:
    # named under another key, it is never read again and goes the same way (#3816 review).
    rotated = catalogue._path(tmp_path, bambuddy.config, b"k" * 32)
    orphans.append(rotated)
    rotated.write_text("{}", encoding="utf-8")
    os.utime(rotated, (old, old))
    recent = directory / "v1-fedcba9876543210fedcba9876543210.json"
    recent.write_text("{}", encoding="utf-8")

    await cached_catalogue(bambuddy, tmp_path)

    kept = catalogue._path(tmp_path, bambuddy.config)
    assert kept != rotated
    assert sorted(directory.iterdir()) == sorted([kept, recent])


def test_forget_catalogues_drops_a_refresh_whose_event_loop_has_closed() -> None:
    """#3311: cancelling a task that has started on a loop since closed raises."""
    loop = asyncio.new_event_loop()
    task = loop.create_task(asyncio.sleep(3600))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    catalogue._refreshing[Path("left-by-an-earlier-test")] = task
    try:
        catalogue.forget_catalogues()
    finally:
        catalogue._refreshing.clear()
    assert catalogue._refreshing == {}


@respx.mock
async def test_an_unreadable_kept_catalogue_is_read_again(
    bambuddy: BambuddyClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """And says why, by the error's type alone (#4196): the file's text stays out."""
    presets, _ = _preset_routes()
    await cached_catalogue(bambuddy, tmp_path)
    (kept,) = (tmp_path / catalogue.CATALOGUE_DIRNAME).iterdir()
    kept.write_text('{"leaked-file-text"', encoding="utf-8")
    catalogue.forget_catalogues()
    caplog.set_level(logging.INFO, logger=catalogue.__name__)
    # The first read's own miss (nothing kept yet) is logged too, where INFO already is.
    caplog.clear()

    assert (await cached_catalogue(bambuddy, tmp_path)).process
    assert presets.call_count == 2
    # Two misses: the open's, and the first read's own look before it reads (#3809 review).
    misses = [r for r in caplog.records if "no kept preset catalogue" in r.getMessage()]
    assert [(r.__dict__["error"], r.__dict__["errno"], r.__dict__["file"]) for r in misses] == [
        ("ValidationError", None, kept.name)
    ] * 2
    # The extras too, which the default formatter leaves out of `caplog.text`.
    assert not any(
        "leaked-file-text" in str(value) for r in misses for value in r.__dict__.values()
    )
    assert "leaked-file-text" not in caplog.text


def test_a_missing_kept_catalogue_says_its_errno(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=catalogue.__name__)

    assert catalogue._load(tmp_path / "absent.json") is None
    (record,) = [r for r in caplog.records if "no kept preset catalogue" in r.getMessage()]
    assert record.__dict__["error"] == "FileNotFoundError"
    assert record.__dict__["errno"] == errno.ENOENT


@respx.mock
async def test_an_open_that_misses_the_kept_file_answers_what_this_process_read(
    bambuddy: BambuddyClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#4528: a check on prod waited on a fresh read with the kept file on the shared
    volume (why its load missed is not known yet; two replicas had just renamed theirs in).
    A process that has read the catalogue answers that at once, as it would an expired
    copy, and reads again behind it; the miss is still logged. The fresh read is held
    until the open has answered, so the open cannot have waited on it."""
    _preset_routes()
    first = await cached_catalogue(bambuddy, tmp_path)
    (kept,) = (tmp_path / catalogue.CATALOGUE_DIRNAME).iterdir()
    kept.unlink()
    held = _HeldPresets(lambda: _ok("slicer-presets-h2c.json"))
    respx.get(f"{API}/slicer/presets").mock(side_effect=held)
    caplog.set_level(logging.INFO, logger=catalogue.__name__)

    started = time.perf_counter()
    async with asyncio.timeout(5):
        read = await cached_catalogue(bambuddy, tmp_path)
    print(f"#4528: the open answered in {(time.perf_counter() - started) * 1000:.0f} ms")

    assert read == first
    assert not held.release.is_set()
    assert any("no kept preset catalogue" in r.getMessage() for r in caplog.records)
    # Read again behind it, and kept for the next open.
    assert len(catalogue._refreshing) == 1
    held.release.set()
    async with asyncio.timeout(5):
        await _refreshes_done()
    assert held.started == 1
    assert kept.exists()


@respx.mock
async def test_a_miss_that_persists_counts_its_opens_in_a_row(
    bambuddy: BambuddyClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#4623 review: answered from memory, a file that stays unreadable would otherwise
    log the same miss on every open; the count says it persists, and a read resets it."""
    _preset_routes()
    # Kept by the first open (whose two looks missed), read back by the second.
    for _ in range(2):
        await cached_catalogue(bambuddy, tmp_path)
    (kept,) = (tmp_path / catalogue.CATALOGUE_DIRNAME).iterdir()
    aside = kept.with_name("aside")
    caplog.set_level(logging.INFO, logger=catalogue.__name__)
    # Only this test's own looks: where INFO is already captured (as in CI, which logs
    # at INFO), the first open's two misses were counted too.
    caplog.clear()

    async def open_while_away() -> None:
        """An open that answers the held copy; the refresh it starts keeps a file, which
        goes again, so the file stays away."""
        await cached_catalogue(bambuddy, tmp_path)
        async with asyncio.timeout(5):
            await _refreshes_done()
        kept.unlink(missing_ok=True)

    # Each open answers the held copy while the file is away, and reads it once back.
    kept.rename(aside)
    await open_while_away()
    await open_while_away()
    aside.rename(kept)
    await cached_catalogue(bambuddy, tmp_path)
    kept.rename(aside)
    await cached_catalogue(bambuddy, tmp_path)

    misses = [r for r in caplog.records if "no kept preset catalogue" in r.getMessage()]
    assert [r.__dict__["misses_in_a_row"] for r in misses] == [1, 2, 1]
    assert all(r.__dict__["held"] for r in misses)


@respx.mock
async def test_a_process_that_has_read_nothing_still_waits_on_its_first_read(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#4528's fallback is only what this process read: with nothing anywhere the open
    waits on the read (#2906)."""
    held = _HeldPresets(lambda: _ok("slicer-presets-h2c.json"))
    respx.get(f"{API}/slicer/presets").mock(side_effect=held)
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))

    opening = asyncio.ensure_future(cached_catalogue(bambuddy, tmp_path))
    async with asyncio.timeout(5):
        await held.asked.wait()
    assert not opening.done()
    held.release.set()

    async with asyncio.timeout(5):
        assert (await opening).process
    assert held.started == 1


@respx.mock
async def test_a_refresh_that_cannot_be_kept_still_answers_the_next_open(
    bambuddy: BambuddyClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4623 review: the fresh read was held under no file's mtime, so when its keep
    failed the next open parsed the older file back and answered that instead."""
    _preset_routes()
    first = await cached_catalogue(bambuddy, tmp_path)
    _expire(tmp_path)
    fresh = replace(first, process=first.process[:1])

    async def read_fresh(client: BambuddyClient) -> catalogue.Catalogue:
        return fresh

    def full(path: Path, kept: catalogue.Catalogue) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(catalogue, "_catalogue", read_fresh)
    monkeypatch.setattr(catalogue, "_keep", full)

    assert await cached_catalogue(bambuddy, tmp_path) == first
    async with asyncio.timeout(5):
        await _refreshes_done()

    assert await cached_catalogue(bambuddy, tmp_path) == fresh


@respx.mock
async def test_a_malformed_refresh_keeps_the_old_catalogue_and_logs_only_its_type(
    bambuddy: BambuddyClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#3446: a body pydantic refuses escaped the task, and its error quoted the body."""
    presets, _ = _preset_routes()
    first = await cached_catalogue(bambuddy, tmp_path)
    _expire(tmp_path)
    presets.mock(return_value=httpx.Response(200, json={"cloud": "leaked-body-text"}))

    assert await cached_catalogue(bambuddy, tmp_path) == first
    await _refreshes_done()

    (record,) = [r for r in caplog.records if "refresh" in r.getMessage()]
    assert record.__dict__["error"] == "ValidationError"
    assert "leaked-body-text" not in caplog.text
    catalogue.forget_catalogues()
    assert await cached_catalogue(bambuddy, tmp_path) == first


@respx.mock
async def test_a_refresh_still_running_is_stopped_at_shutdown(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#3446: a pending refresh was destroyed with the loop, with warnings."""
    presets, _ = _preset_routes()
    await cached_catalogue(bambuddy, tmp_path)
    _expire(tmp_path)
    presets.mock(side_effect=_after(asyncio.Event(), "slicer-presets-h2c.json"))
    await cached_catalogue(bambuddy, tmp_path)
    (task,) = catalogue._refreshing.values()

    async with asyncio.timeout(5):
        await catalogue.stop_refreshes()

    assert task.cancelled()
    assert not catalogue._refreshing


@respx.mock
async def test_a_write_that_fails_leaves_no_partial_file(
    bambuddy: BambuddyClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3447: nothing sweeps the directory, so a half-written file stayed for good."""
    _preset_routes()

    def full(self: Path, target: Path) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", full)

    assert (await cached_catalogue(bambuddy, tmp_path)).process
    assert list((tmp_path / catalogue.CATALOGUE_DIRNAME).iterdir()) == []


@respx.mock
async def test_only_the_last_parsed_catalogue_is_held(
    bambuddy: BambuddyClient, config: BambuddyConfig, tmp_path: Path
) -> None:
    """#3447: after a change of Bambuddy key the old one stayed in every replica."""
    _preset_routes()
    # Each is kept by its first open and parsed back from its file by its second.
    for _ in range(2):
        await cached_catalogue(bambuddy, tmp_path)
    async with BambuddyClient(replace(config, api_key="rotated")) as rotated:
        for _ in range(2):
            await cached_catalogue(rotated, tmp_path)

    (held,) = catalogue._parsed
    assert held == catalogue._path(tmp_path, replace(config, api_key="rotated"))


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
async def test_opens_with_nothing_kept_share_one_read(
    bambuddy: BambuddyClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2906: with no catalogue kept yet (a fresh volume, a new key), opens at once
    each read /slicer/presets (~6 s, 1 MB). They now wait on one read."""
    held = _HeldPresets(lambda: _ok("slicer-presets-h2c.json"))
    respx.get(f"{API}/slicer/presets").mock(side_effect=held)
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))
    waiting = _count_shields(monkeypatch)

    opens = [asyncio.ensure_future(cached_catalogue(bambuddy, tmp_path)) for _ in range(3)]
    await waiting(3)
    held.release.set()
    async with asyncio.timeout(5):
        first, *rest = await asyncio.gather(*opens)

    assert rest == [first, first]
    assert held.started == 1
    assert not catalogue._reading


@respx.mock
async def test_an_open_given_up_on_does_not_cancel_the_read_the_others_wait_on(
    bambuddy: BambuddyClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2906: choices_for cancels its catalogue read when another read fails first;
    that must not take the shared read from the opens still waiting on it."""
    held = _HeldPresets(lambda: _ok("slicer-presets-h2c.json"))
    respx.get(f"{API}/slicer/presets").mock(side_effect=held)
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))
    waiting = _count_shields(monkeypatch)

    given_up = asyncio.ensure_future(cached_catalogue(bambuddy, tmp_path))
    still = asyncio.ensure_future(cached_catalogue(bambuddy, tmp_path))
    await waiting(2)
    given_up.cancel()
    held.release.set()

    async with asyncio.timeout(5):
        assert (await still).process
    assert given_up.cancelled()
    assert held.started == 1
    # Kept for the next open, though the one that started the read gave up on it.
    assert catalogue._load(next((tmp_path / catalogue.CATALOGUE_DIRNAME).iterdir()))


@respx.mock
async def test_a_shared_read_that_fails_fails_every_open_and_is_not_kept(
    bambuddy: BambuddyClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = _HeldPresets(lambda: httpx.Response(500))
    presets = respx.get(f"{API}/slicer/presets").mock(side_effect=held)
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))
    waiting = _count_shields(monkeypatch)

    opens = [asyncio.ensure_future(cached_catalogue(bambuddy, tmp_path)) for _ in range(2)]
    await waiting(2)
    held.release.set()
    async with asyncio.timeout(5):
        outcomes = await asyncio.gather(*opens, return_exceptions=True)

    assert all(isinstance(outcome, ApiError) for outcome in outcomes)
    assert held.started == 1
    assert not catalogue._reading
    # The next open reads again.
    presets.mock(side_effect=None, return_value=_ok("slicer-presets-h2c.json"))
    assert (await cached_catalogue(bambuddy, tmp_path)).process


@respx.mock
async def test_a_first_read_started_after_another_kept_the_file_reads_that_file(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#3809 review: an open whose load missed just before another read kept the file
    starts a read of its own; that read takes the kept file rather than ask again."""
    presets, _ = _preset_routes()
    await cached_catalogue(bambuddy, tmp_path)
    path = next((tmp_path / catalogue.CATALOGUE_DIRNAME).iterdir())

    assert (await catalogue._first_read(bambuddy.config, path)).process
    assert presets.call_count == 1


@respx.mock
async def test_shutdown_stops_a_first_read_too(bambuddy: BambuddyClient, tmp_path: Path) -> None:
    """#3809 review: a first read still running at shutdown is cancelled and awaited, as
    the refreshes are, so its client closes."""
    held = _HeldPresets(lambda: _ok("slicer-presets-h2c.json"))
    respx.get(f"{API}/slicer/presets").mock(side_effect=held)
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))
    opening = asyncio.ensure_future(cached_catalogue(bambuddy, tmp_path))
    async with asyncio.timeout(5):
        await held.asked.wait()
    (reading,) = catalogue._reading.values()

    await catalogue.stop_refreshes()

    assert reading.cancelled()
    opening.cancel()
    await asyncio.gather(opening, return_exceptions=True)


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
            catalogue_key=None,
        )

    assert "slicer presets" in raised.value.detail


@respx.mock
async def test_a_failing_catalogue_beats_the_filament_steps_failure_with_no_printer(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#2907: with no printer to open on, the catalogue and the filament step are read
    on the other branch, and its failure still surfaces first there too."""
    respx.get(f"{API}/printers/").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(500))
    respx.get(f"{API}/inventory/assignments").mock(return_value=httpx.Response(500))
    respx.get(f"{API}/library/files/62/filament-requirements").mock(
        return_value=_ok("filament-requirements.json")
    )
    respx.get(f"{API}/slicer/presets").mock(return_value=httpx.Response(500))
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))

    with pytest.raises(ApiError) as raised:
        await choices_for(
            bambuddy,
            _source(),
            StoredSettings(),
            remembered=None,
            printer_id=None,
            cache=tmp_path,
            catalogue_key=None,
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
            bambuddy,
            _source(),
            StoredSettings(),
            remembered=None,
            printer_id=1,
            cache=tmp_path,
            catalogue_key=None,
        )

    assert view.last_bed_type is None
    assert view.filaments.slots


class _CheckSource(_ReadSource):
    """A source whose download starts and never ends, unless it is cancelled."""

    def __init__(self) -> None:
        self.downloading = asyncio.Event()
        self.cancelled = False

    async def states_nozzles(self, client: BambuddyClient) -> bool:
        self.downloading.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return False


@respx.mock
async def test_the_checks_download_and_spools_do_not_wait_on_the_printer(
    bambuddy: BambuddyClient,
) -> None:
    """#4196: the check read the printer, then the 3MF, then the spools, one after
    another. Here the printer list answers only once the download and the spools have
    been asked for, and it lists no printer 1, so the check refuses and drops the
    download and the assignments read it started."""
    source = _CheckSource()
    spools_asked = asyncio.Event()
    respx.get(f"{API}/inventory/spools").mock(
        side_effect=_marks(spools_asked, "inventory-spools.json")
    )
    assignments_dropped = False

    async def assignments_never(request: httpx.Request) -> httpx.Response:
        nonlocal assignments_dropped
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assignments_dropped = True
            raise
        raise AssertionError("never answered")

    respx.get(f"{API}/inventory/assignments").mock(side_effect=assignments_never)

    async def no_printers(request: httpx.Request) -> httpx.Response:
        await source.downloading.wait()
        await spools_asked.wait()
        return httpx.Response(200, json=[])

    respx.get(f"{API}/printers/").mock(side_effect=no_printers)
    request = PrintRunRequest.model_validate(
        {
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
            "choices": {"nozzles": [{"size": "0.4"}], "tier": "standard"},
            "printer_id": 1,
        }
    )

    async with asyncio.timeout(5):
        check = await check_print(bambuddy, cast(PrintSource, source), StoredSettings(), request)

    assert check.errors == ["Bambuddy has no printer 1. Pick another printer."]
    assert source.cancelled
    assert assignments_dropped


class _CheckDone(_ReadSource):
    async def states_nozzles(self, client: BambuddyClient) -> bool:
        return False


class _DownloadFails(_ReadSource):
    async def states_nozzles(self, client: BambuddyClient) -> bool:
        raise ValueError("the download failed")


@respx.mock
@pytest.mark.parametrize("download_fails", [True, False])
async def test_the_checks_early_reads_fail_in_the_order_they_were_read_in(
    bambuddy: BambuddyClient, download_fails: bool
) -> None:
    """#4196: read beside `prepare_run`, the download and the spools still fail the
    check as they did when read after it, the download's failure first."""
    respx.get(f"{API}/printers/").mock(return_value=_ok("printers.json"))
    respx.get(f"{API}/printers/1/status").mock(return_value=_ok("printer-status-rack.json"))
    _preset_routes()
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(500))
    respx.get(f"{API}/inventory/assignments").mock(return_value=_ok("inventory-assignments.json"))
    source = _DownloadFails() if download_fails else _CheckDone()
    request = PrintRunRequest.model_validate(
        {
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
            "choices": {"nozzles": [{"size": "0.4"}], "tier": "standard"},
            "printer_id": 1,
        }
    )

    expected, said = (
        (ValueError, "the download failed") if download_fails else (ApiError, "filament spools")
    )
    with pytest.raises(expected, match=said):
        await check_print(bambuddy, cast(PrintSource, source), StoredSettings(), request)


def _sidecar_unanswered() -> httpx.Response:
    """/slicer/presets as Bambuddy answered it while its slicer sidecar timed out
    (#4717): ``_fetch_bundled_presets`` leaves the standard tier empty with no error or
    status, and ``_enrich_cloud_metadata``, with no standard row to copy from, leaves
    every cloud row's ``compatible_printers`` empty."""
    body = recording("slicer-presets-h2c.json")
    body["standard"] = {"printer": [], "process": [], "filament": []}
    for slot in body["cloud"].values():
        for row in slot:
            row["compatible_printers"] = []
    return httpx.Response(200, json=body)


@respx.mock
async def test_a_catalogue_read_without_its_standard_tier_is_bambuddy_unavailable(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#4717: a read whose cloud tier lists presets but whose standard tier lists none is
    a sidecar that did not answer, not a catalogue; kept, it refused every process as
    "not a process Bambuddy has". It is a 503 that says why, and nothing is kept."""
    presets = respx.get(f"{API}/slicer/presets").mock(
        side_effect=[_sidecar_unanswered(), _ok("slicer-presets-h2c.json")]
    )
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))

    with pytest.raises(ApiError) as raised:
        await cached_catalogue(bambuddy, tmp_path)

    assert raised.value.status == 503
    assert raised.value.type == UNAVAILABLE_PROBLEM
    assert is_transient(raised.value)
    assert "standard" in raised.value.detail
    assert "not configured" in raised.value.detail
    assert not (tmp_path / catalogue.CATALOGUE_DIRNAME).exists()
    assert (await cached_catalogue(bambuddy, tmp_path)).process
    assert presets.call_count == 2


@respx.mock
async def test_the_runs_fresh_read_without_its_standard_tier_is_bambuddy_unavailable(
    bambuddy: BambuddyClient,
) -> None:
    """#4717: the run reads the catalogue fresh, and the same answer fails it the same way."""
    respx.get(f"{API}/slicer/presets").mock(return_value=_sidecar_unanswered())
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))

    with pytest.raises(ApiError) as raised:
        await catalogue_for(bambuddy, native=None)

    assert raised.value.type == UNAVAILABLE_PROBLEM


@respx.mock
async def test_preset_names_still_read_without_the_standard_tier(
    bambuddy: BambuddyClient,
) -> None:
    """#4717: the native slicer reads only names, which need no standard tier, so a read
    the sidecar did not answer still names a cloud ref."""
    respx.get(f"{API}/slicer/presets").mock(return_value=_sidecar_unanswered())
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))
    cloud = recording("slicer-presets-h2c.json")["cloud"]["process"][0]

    names = await catalogue.read_preset_names(bambuddy)

    assert names[("cloud", cloud["id"])] == cloud["name"]


@respx.mock
async def test_a_refresh_without_the_standard_tier_keeps_the_old_catalogue(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#4717: the dialog's kept copy is not replaced by a read the sidecar did not answer."""
    presets, _ = _preset_routes()
    first = await cached_catalogue(bambuddy, tmp_path)
    (kept,) = (tmp_path / catalogue.CATALOGUE_DIRNAME).iterdir()
    _expire(tmp_path)
    presets.mock(return_value=_sidecar_unanswered())

    assert await cached_catalogue(bambuddy, tmp_path) == first
    await _refreshes_done()

    assert presets.call_count == 2
    assert kept.stat().st_mtime == 0  # not rewritten
    catalogue.forget_catalogues()
    assert await cached_catalogue(bambuddy, tmp_path) == first


@respx.mock
async def test_a_failed_local_presets_read_beats_a_read_without_the_standard_tier(
    bambuddy: BambuddyClient,
) -> None:
    """#4717 review: the reads' failures surface in their order (``settled``), so a
    failed /local-presets/ read is still what a read without the standard tier says."""
    respx.get(f"{API}/slicer/presets").mock(return_value=_sidecar_unanswered())
    respx.get(f"{API}/local-presets/").mock(return_value=httpx.Response(500))

    with pytest.raises(ApiError) as raised:
        await catalogue_for(bambuddy, native=None)

    assert "local presets" in raised.value.detail


@respx.mock
async def test_an_all_empty_catalogue_still_reads(bambuddy: BambuddyClient, tmp_path: Path) -> None:
    """#4717: with no cloud presets either, an empty standard tier is no sign of a
    sidecar that did not answer, and the catalogue reads as it did."""
    respx.get(f"{API}/slicer/presets").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))

    read = await cached_catalogue(bambuddy, tmp_path)

    assert not [row for row in read.process if row.ref.source in ("cloud", "standard")]
