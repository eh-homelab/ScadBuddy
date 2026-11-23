"""#7244: Bambuddy's presets enter ScadBuddy's own only through the Schedule's pull, while
``filament_sync.presets`` pulls, a one-time import, and once, at the API's start, for the
bindings that still name a Bambuddy local id. No copy of Bambuddy's presets is kept."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import client_for
from scadbuddy.core.settings import FilamentSync, SyncDirection
from scadbuddy.filament.bb_pull_models import PullOutcome
from scadbuddy.filament.sb_presets import (
    FilamentPresetStore,
    LivePresets,
    PresetImport,
    adopt_at_start,
    adopt_held,
    import_presets,
)
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.slicing.own_presets import sb_id
from scadbuddy.spools.activities import filament_pull_activity
from scadbuddy.store.local import LocalBlobStore
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
    assert done == PresetImport(adopted=1, updated=1)
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
    pg_pool: PgPool, tmp_path: Path
) -> None:
    sb = _type(pg_pool, "PLA-O")
    _bind(pg_pool, sb, "GFA00")
    with respx.mock() as router:
        await adopt_at_start(pg_pool, lambda: LocalBlobStore(tmp_path), lambda: SETTINGS, retry_s=0)
        assert router.calls.call_count == 0
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
