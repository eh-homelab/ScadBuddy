"""#7244: Bambuddy's presets enter ScadBuddy's own only through the Schedule's pull, while
``filament_sync.presets`` pulls, or a one-time import; the kept copy's refresh imports
nothing."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
import respx

from scadbuddy.bambuddy.client import client_for
from scadbuddy.core.settings import FilamentSync, SyncDirection
from scadbuddy.filament.bb_pull_models import PullOutcome
from scadbuddy.filament.sb_presets import (
    FilamentPresetStore,
    LivePresets,
    PresetImport,
    adopt_held,
    import_presets,
)
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.slicing.held_presets import HeldPresetStore, refresh_held
from scadbuddy.slicing.own_presets import sb_id
from scadbuddy.spools.activities import filament_pull_activity
from scadbuddy.store.local import LocalBlobStore
from tests.conftest import PgPool
from tests.filament.test_bambuddy_pull import _Execute, _filament
from tests.filament.test_sb_presets import _bind, _bindings, _held, _type

pytestmark = pytest.mark.requires_postgres

BB = "https://bambuddy.test"
SETTINGS = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")


async def _one_of_each(
    pool: PgPool, blobs: LocalBlobStore, held: HeldPresetStore
) -> tuple[UUID, UUID]:
    """A ``bambuddy`` preset taken earlier (Bambuddy id 7), whose stamp in the copy moved
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
    held = HeldPresetStore(pg_pool)
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


async def test_the_kept_copy_refresh_writes_no_scadbuddy_preset(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    """Before #7244 the copy's refresh adopted every bound Bambuddy id at once."""
    held = HeldPresetStore(pg_pool)
    waiting = _type(pg_pool, "PLA-H")
    _bind(pg_pool, waiting, "9")
    listing = {
        "filament": [
            {
                "id": 9,
                "name": "Nine",
                "preset_type": "filament",
                "source": "orcaslicer",
                "updated_at": "2026-11-23T01:00:00",
            }
        ]
    }
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/local-presets/").respond(json=listing)
        router.get(f"{BB}/api/v1/local-presets/9").respond(
            json=_held(9, "Nine", "2026-11-23T01:00:00").model_dump(mode="json")
        )
        assert await refresh_held(held, lambda: SETTINGS) == "refreshed"
    assert set(await held.stamps()) == {9}
    assert FilamentPresetStore(pg_pool, lambda: LocalBlobStore(tmp_path)).all() == []
    assert _bindings(pg_pool, waiting) == [("9", None)]


async def test_a_one_time_import_reads_bambuddy_itself(pg_pool: PgPool, tmp_path: Path) -> None:
    """`LivePresets`: one list, then a read of each preset the import needs; the kept copy
    is neither read nor written."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    held = HeldPresetStore(pg_pool)
    _, waiting = await _one_of_each(pg_pool, blobs, held)
    await held.clear()
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
    assert await held.stamps() == {}
