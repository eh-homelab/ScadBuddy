"""#7244: Bambuddy's presets enter ScadBuddy's own only through the Schedule's pull, while
``filament_sync.presets`` pulls, a one-time import, and once, at the API's start, for the
bindings that still name a Bambuddy local id. No copy of Bambuddy's presets is kept."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import client_for
from scadbuddy.core.settings import FilamentSync, SyncDirection
from scadbuddy.filament.bb_pull_models import PullOutcome, PullStart
from scadbuddy.filament.rfid_push import try_lock_on
from scadbuddy.filament.sb_presets import (
    ADOPT_LOCK,
    FilamentPresetStore,
    LivePresets,
    PresetImport,
    adopt_at_start,
    adopt_held,
    import_presets,
)
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.slicing.own_presets import sb_id
from scadbuddy.spools import activities
from scadbuddy.spools.activities import filament_pull_activity
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows.spool_refresh import FILAMENT_PULL_TIMEOUT
from tests.conftest import PgPool
from tests.filament.test_bambuddy_pull import _Execute, _filament
from tests.filament.test_sb_presets import MemoryPresets, _bind, _bindings, _held, _type

pytestmark = pytest.mark.requires_postgres

BB = "https://bambuddy.test"
SETTINGS = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")


async def _one_of_each(
    pool: PgPool, blobs: LocalBlobStore, held: MemoryPresets
) -> tuple[UUID, UUID]:
    """A ``bambuddy`` preset taken earlier (Bambuddy id 7), whose stamp in Bambuddy moved
    since, and a type that names Bambuddy's preset 9, not taken yet."""
    taken = _type(pool, "PLA-T")
    await held.keep(_held(7, "Taken", "2026-11-20T01:00:00"))
    _bind(pool, taken, "7")
    assert await adopt_held(pool, blobs, held) == 1
    await held.keep(_held(7, "Taken", "2026-11-23T01:00:00", low="200"))
    waiting = _type(pool, "PLA-W")
    await held.keep(_held(9, "Waiting", "2026-11-23T01:00:00"))
    _bind(pool, waiting, "9")
    return taken, waiting


@pytest.mark.parametrize(
    ("direction", "imports"),
    [("off", False), ("sb_to_bb", False), ("bb_to_sb", True), ("two_way", True)],
)
async def test_the_pull_imports_presets_only_while_presets_pulls(
    pg_pool: PgPool, tmp_path: Path, direction: SyncDirection, imports: bool
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    held = MemoryPresets()
    _, waiting = await _one_of_each(pg_pool, blobs, held)
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    (taken,) = store.all()
    ran: list[PresetImport] = []

    async def run() -> PresetImport:
        done = await import_presets(pg_pool, blobs, held)
        ran.append(done)
        return done

    activity = filament_pull_activity(
        _filament(pg_pool),
        lambda: SETTINGS,
        lambda: FilamentSync(presets=direction),
        _Execute(PullOutcome(outcome="pulled")),
        run,
    )
    assert await activity() == "pulled"

    now = store.get(taken.id)
    assert now is not None
    [(ref, _)] = _bindings(pg_pool, waiting)
    if imports:
        assert ran == [PresetImport(adopted=1, updated=1)]
        assert now.version == 2
        assert sb_id(ref) is not None
    else:
        assert ran == []
        assert now.version == 1
        assert ref == "9"


async def test_a_failed_pull_still_imports_and_a_failed_import_changes_no_answer(
    pg_pool: PgPool,
) -> None:
    calls: list[str] = []

    async def failing() -> PresetImport:
        calls.append("import")
        raise RuntimeError("the blob store is down")

    down = _Execute(RuntimeError("Temporal is out of reach"))
    sync = lambda: FilamentSync(presets="two_way")  # noqa: E731
    f = _filament(pg_pool)
    assert await filament_pull_activity(f, lambda: SETTINGS, sync, down, failing)() == "failed"
    ok = _Execute(PullOutcome(outcome="pulled"))
    assert await filament_pull_activity(f, lambda: SETTINGS, sync, ok, failing)() == "pulled"
    assert calls == ["import", "import"]
    # No Bambuddy: no pull, so no import either.
    assert await filament_pull_activity(f, StoredSettings, sync, ok, failing)() == "skipped"
    assert calls == ["import", "import"]


async def test_the_pull_mirrors_whatever_the_direction_and_a_failed_mirror_changes_nothing(
    pg_pool: PgPool,
) -> None:
    """The mirror (#7176) runs last on every pull, a skipped one included, so it sends with
    ``filament_sync`` all ``sb_to_bb`` too; its failure changes no answer (#7244)."""
    mirrored: list[str] = []

    async def mirror() -> int:
        mirrored.append("mirror")
        return 0

    async def failing() -> int:
        mirrored.append("failing")
        raise RuntimeError("Bambuddy is down")

    f = _filament(pg_pool)
    ok = _Execute(PullOutcome(outcome="pulled"))
    nothing = lambda: FilamentSync(  # noqa: E731
        spools="sb_to_bb", weights="sb_to_bb", tags="sb_to_bb", presets="sb_to_bb"
    )
    assert await filament_pull_activity(f, lambda: SETTINGS, nothing, ok, None, mirror)() == (
        "skipped"
    )
    pulls = lambda: FilamentSync(presets="sb_to_bb", spools="two_way")  # noqa: E731
    assert await filament_pull_activity(f, lambda: SETTINGS, pulls, ok, None, failing)() == (
        "pulled"
    )
    assert mirrored == ["mirror", "failing"]


async def test_a_one_time_import_reads_bambuddy_itself(pg_pool: PgPool, tmp_path: Path) -> None:
    """`LivePresets`: one list, then a read of each preset the import needs."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    held = MemoryPresets()
    _, waiting = await _one_of_each(pg_pool, blobs, held)
    gone = _type(pg_pool, "PLA-G")
    _bind(pg_pool, gone, "11")
    rows = [
        _held(7, "Taken", "2026-11-23T01:00:00", low="200"),
        _held(9, "Waiting", "2026-11-23T01:00:00"),
        _held(11, "Gone", "2026-11-23T01:00:00"),
    ]
    listing = {"filament": [r.model_dump(mode="json", exclude={"setting"}) for r in rows]}
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/local-presets/").respond(json=listing)
        reads = {
            r.id: router.get(f"{BB}/api/v1/local-presets/{r.id}").respond(
                json=r.model_dump(mode="json")
            )
            for r in rows[:2]
        }
        # Deleted between the list and its read: left out.
        router.get(f"{BB}/api/v1/local-presets/11").respond(404, json={"detail": "gone"})
        async with client_for(SETTINGS) as client:
            done = await import_presets(pg_pool, blobs, LivePresets(client))
        assert router.calls.call_count == 4
        assert all(route.call_count == 1 for route in reads.values())
    # Bambuddy deleted 11 between the list and its read: its binding is still to fix.
    assert done == PresetImport(adopted=1, updated=1, unresolved=1)
    [(ref, _)] = _bindings(pg_pool, waiting)
    assert sb_id(ref) is not None
    assert _bindings(pg_pool, gone) == [("11", None)]


async def test_the_start_adopts_bound_bambuddy_ids_whatever_the_direction(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    """With ``filament_sync`` at its default (presets off): the bindings made before #7244
    become ``sb-`` refs, from Bambuddy read once; an id Bambuddy lacks is left."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    bound, gone = _type(pg_pool, "PLA-S"), _type(pg_pool, "PLA-X")
    _bind(pg_pool, bound, "9")
    _bind(pg_pool, gone, "11")
    nine = _held(9, "Nine", "2026-11-23T01:00:00")
    listing = {"filament": [nine.model_dump(mode="json", exclude={"setting"})]}
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/local-presets/").respond(json=listing)
        router.get(f"{BB}/api/v1/local-presets/9").respond(json=nine.model_dump(mode="json"))
        await adopt_at_start(pg_pool, lambda: blobs, lambda: SETTINGS, retry_s=0)
    [(ref, _)] = _bindings(pg_pool, bound)
    assert sb_id(ref) is not None
    assert _bindings(pg_pool, gone) == [("11", None)]


async def test_the_start_asks_bambuddy_nothing_when_nothing_is_bound(
    pg_pool: PgPool, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sb = _type(pg_pool, "PLA-O")
    _bind(pg_pool, sb, "GFA00")
    caplog.set_level(logging.INFO, logger="scadbuddy.filament.sb_presets")
    with respx.mock() as router:
        await adopt_at_start(pg_pool, lambda: LocalBlobStore(tmp_path), lambda: SETTINGS, retry_s=0)
        assert router.calls.call_count == 0
    # Logged, so a rollout shows the step ran (#7590).
    assert "nothing to adopt" in caplog.text
    # No Bambuddy configured: a bound id is left and nothing is asked.
    _bind(pg_pool, sb, "9")
    await adopt_at_start(pg_pool, lambda: LocalBlobStore(tmp_path), StoredSettings, retry_s=0)
    assert _bindings(pg_pool, sb) == [("9", None)]


async def test_the_start_tries_again_after_a_failure(pg_pool: PgPool, tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    bound = _type(pg_pool, "PLA-A")
    _bind(pg_pool, bound, "9")
    nine = _held(9, "Nine", "2026-11-23T01:00:00")
    listing = {"filament": [nine.model_dump(mode="json", exclude={"setting"})]}
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/local-presets/").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json=listing)]
        )
        router.get(f"{BB}/api/v1/local-presets/9").respond(json=nine.model_dump(mode="json"))
        await adopt_at_start(pg_pool, lambda: blobs, lambda: SETTINGS, retry_s=0)
    [(ref, _)] = _bindings(pg_pool, bound)
    assert sb_id(ref) is not None


async def test_the_start_asks_no_other_bambuddy_than_the_links_one(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    """A Bambuddy local id means something only on the Bambuddy that issued it: with the
    links recorded for another one, the start reads nothing and the binding stays, an error
    the print dialog shows (#7244 review)."""
    bound = _type(pg_pool, "PLA-E")
    _bind(pg_pool, bound, "9")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_bb_sync_state SET instance = 'aaaaaaaaaaaa'")
    with respx.mock() as router:
        await adopt_at_start(pg_pool, lambda: LocalBlobStore(tmp_path), lambda: SETTINGS, retry_s=0)
        assert router.calls.call_count == 0
    assert _bindings(pg_pool, bound) == [("9", None)]


async def test_the_start_waits_while_another_replica_holds_its_lock(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    """One replica adopts: another that finds `ADOPT_LOCK` held reads nothing, and tries
    again, so it adopts once the holder ended without doing so (#7244 review)."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    bound = _type(pg_pool, "PLA-L")
    _bind(pg_pool, bound, "9")
    nine = _held(9, "Nine", "2026-11-23T01:00:00")
    listing = {"filament": [nine.model_dump(mode="json", exclude={"setting"})]}
    assert isinstance(pg_pool.conninfo, str)
    held = try_lock_on(pg_pool.conninfo, ADOPT_LOCK)
    assert held is not None
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/local-presets/").respond(json=listing)
        router.get(f"{BB}/api/v1/local-presets/9").respond(json=nine.model_dump(mode="json"))
        waiting = asyncio.create_task(
            adopt_at_start(pg_pool, lambda: blobs, lambda: SETTINGS, retry_s=0.05)
        )
        try:
            await asyncio.sleep(0.5)
            assert not waiting.done()
            assert router.calls.call_count == 0
        finally:
            held.close()
        await asyncio.wait_for(waiting, 30)
    [(ref, _)] = _bindings(pg_pool, bound)
    assert sb_id(ref) is not None


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("left_s", "logged"),
    [(-1.0, "no time was left to import"), (0.05, "ran out of time importing")],
)
async def test_a_pull_that_leaves_the_import_no_time_says_so(
    pg_pool: PgPool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    left_s: float,
    logged: str,
) -> None:
    """A slow pull leaves the import what is left of the activity's timeout: none, or too
    little, is a warning, and the pull's answer stands (#7244 review)."""
    clock = _Clock()
    monkeypatch.setattr(activities, "time", clock)
    deadline = FILAMENT_PULL_TIMEOUT.total_seconds() - activities.IMPORT_MARGIN_S

    class _Slow(_Execute):
        async def __call__(self, start: PullStart) -> PullOutcome:
            clock.now = deadline - left_s
            return await super().__call__(start)

    started: list[str] = []

    async def slow_import() -> PresetImport:
        started.append("import")
        await asyncio.sleep(5)
        return PresetImport(adopted=0, updated=0)

    caplog.set_level(logging.WARNING)
    run = filament_pull_activity(
        _filament(pg_pool),
        lambda: SETTINGS,
        lambda: FilamentSync(presets="two_way"),
        _Slow(PullOutcome(outcome="pulled")),
        slow_import,
    )
    assert await run() == "pulled"
    assert started == ([] if left_s < 0 else ["import"])
    assert [r.getMessage() for r in caplog.records if logged in r.getMessage()]
