"""The Bambuddy mirror of ScadBuddy's own filament presets (#7176 Decision 11): a new
preset is created, a changed one is rewritten in place under its Bambuddy id, one deleted
in Bambuddy is created again, a refusal is kept on the preset, and the setting turns it
off. Bambuddy is respx only."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from psycopg.types.json import Jsonb

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.models import LocalPreset, LocalPresetCatalogue
from scadbuddy.filament.bb_sync_results import ResetRefusedError, reset_links, reset_preview
from scadbuddy.filament.preset_mirror import (
    LOCK,
    mirror_pending,
    mirror_with,
    wants_mirror,
)
from scadbuddy.filament.rfid_push import try_lock_on
from scadbuddy.filament.sb_presets import (
    FilamentPresetStore,
    adopt_held,
    bambuddy_refs_on,
    create_on,
    document_bytes,
    keep_document,
    version_on,
)
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.store.local import LocalBlobStore
from tests.conftest import PgPool
from tests.filament.test_bambuddy_pull import _filament
from tests.filament.test_sb_presets import MemoryPresets, _bind, _held, _type

pytestmark = pytest.mark.requires_postgres

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"
#: The configured Bambuddy's instance id in these tests.
HERE = "a1b2c3d4e5f6"


def _preset_json(preset_id: int, name: str) -> dict[str, Any]:
    return {"id": preset_id, "name": name, "preset_type": "filament", "source": "local"}


def listed(*presets: dict[str, Any]) -> respx.Route:
    """Bambuddy's local presets, as the mirror lists them before a create."""
    return respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(
            200, json={"filament": list(presets), "printer": [], "process": []}
        )
    )


def detail(preset_id: int, name: str, setting: dict[str, Any]) -> respx.Route:
    """Bambuddy's preset ``preset_id`` with its document, as a lost create's match reads it."""
    return respx.get(f"{API}/local-presets/{preset_id}").mock(
        return_value=httpx.Response(200, json={**_preset_json(preset_id, name), "setting": setting})
    )


@pytest.fixture
async def client() -> AsyncIterator[BambuddyClient]:
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="s3cret")) as bambuddy:
        yield bambuddy


async def _make(
    pool: PgPool,
    blobs: LocalBlobStore,
    doc: dict[str, Any],
    *,
    source: str = "3dfp",
    bb_id: int | None = None,
    instance: str | None = None,
) -> int:
    key, sha256 = await keep_document(blobs, document_bytes(doc))
    with pool.connection() as conn, conn.transaction():
        return create_on(
            conn,
            name=doc["name"],
            source=source,  # type: ignore[arg-type]
            key=key,
            sha256=sha256,
            doc=doc,
            made_by="t",
            bb_preset_id=bb_id,
            bb_instance=instance,
        )


def _row(pool: PgPool, preset_id: int) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT bb_preset_id, bb_instance, sha256, mirrored_sha256, mirror_error,"
            " mirror_create_floor FROM filament_presets"
            " WHERE id = %s",
            (preset_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


@respx.mock
async def test_a_new_preset_is_created_and_a_changed_one_rewritten_in_place(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    doc = {"name": "Silk @H2C", "filament_type": ["PLA"]}
    made = await _make(pg_pool, blobs, doc)
    listed(_preset_json(9, "Other"))
    create = respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(141, "Silk @H2C"))
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 1
    assert json.loads(create.calls[0].request.content) == {
        "name": "Silk @H2C",
        "preset_type": "filament",
        "setting": doc,
    }
    row = _row(pg_pool, made)
    assert (row["bb_preset_id"], row["mirrored_sha256"]) == (141, row["sha256"])
    assert (row["bb_instance"], row["mirror_create_floor"]) == (HERE, None)
    # In step: nothing is sent again.
    assert await mirror_with(client, pg_pool, store, HERE) == 0

    second = {**doc, "nozzle_temperature_range_low": ["200"]}
    key, sha256 = await keep_document(blobs, document_bytes(second))
    with pg_pool.connection() as conn, conn.transaction():
        version_on(conn, made, name="Silk @H2C", key=key, sha256=sha256, doc=second, made_by="t")
    update = respx.put(f"{API}/local-presets/141").mock(
        return_value=httpx.Response(200, json=_preset_json(141, "Silk @H2C"))
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 1
    assert json.loads(update.calls[0].request.content) == {"name": "Silk @H2C", "setting": second}
    assert create.call_count == 1
    assert _row(pg_pool, made)["mirrored_sha256"] == sha256


@respx.mock
async def test_a_preset_deleted_in_bambuddy_is_created_again(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    made = await _make(pg_pool, blobs, {"name": "Gone"}, source="manual", bb_id=50)
    respx.put(f"{API}/local-presets/50").mock(
        return_value=httpx.Response(404, json={"detail": "Not found"})
    )
    listed()
    respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(77, "Gone"))
    )
    assert (
        await mirror_with(client, pg_pool, FilamentPresetStore(pg_pool, lambda: blobs), HERE) == 1
    )
    assert _row(pg_pool, made)["bb_preset_id"] == 77


@respx.mock
async def test_a_refusal_is_kept_on_the_preset_and_bambuddys_own_are_never_sent(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    refused = await _make(pg_pool, blobs, {"name": "Refused"})
    theirs = await _make(pg_pool, blobs, {"name": "Theirs"}, source="bambuddy", bb_id=9)
    listed()
    route = respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(403, json={"detail": "Forbidden"})
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 0
    assert route.call_count == 1
    row = _row(pg_pool, refused)
    assert row["mirror_error"] is not None and row["mirrored_sha256"] is None
    assert _row(pg_pool, theirs)["mirror_error"] is None


async def test_the_setting_or_no_bambuddy_turns_it_off(pg_pool: PgPool, tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    await _make(pg_pool, blobs, {"name": "Kept here"})
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    off = StoredSettings(bambuddy_url=BASE, filament_preset_mirror=False)
    assert not wants_mirror(off)
    assert not wants_mirror(StoredSettings())
    assert wants_mirror(StoredSettings(bambuddy_url=BASE))
    assert await mirror_pending(pg_pool, store, off) == 0


@respx.mock
async def test_an_id_of_another_bambuddy_is_never_rewritten(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    """A preset whose id is another Bambuddy's than the links' (a mirror that raced a reset
    stored it) is created on the configured one; its id 17 there may be another preset."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    made = await _make(pg_pool, blobs, {"name": "Moved"}, bb_id=17, instance="0ld0ld0ld0ld")
    _links_name(pg_pool, HERE)
    put = respx.put(f"{API}/local-presets/17").mock(
        return_value=httpx.Response(200, json=_preset_json(17, "Someone else's"))
    )
    listed(_preset_json(17, "Someone else's"))
    respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(30, "Moved"))
    )
    assert (
        await mirror_with(client, pg_pool, FilamentPresetStore(pg_pool, lambda: blobs), HERE) == 1
    )
    assert put.call_count == 0
    row = _row(pg_pool, made)
    assert (row["bb_preset_id"], row["bb_instance"]) == (30, HERE)


@respx.mock
async def test_a_create_whose_answer_was_lost_is_not_made_twice(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    """The first create's answer never comes back, though Bambuddy made the preset; the
    next send finds it above the kept floor and rewrites it in place."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    made = await _make(pg_pool, blobs, {"name": "Lost"})
    first = listed(_preset_json(40, "Lost"))
    create = respx.post(f"{API}/local-presets/").mock(side_effect=httpx.ReadTimeout("lost"))
    assert await mirror_with(client, pg_pool, store, HERE) == 0
    row = _row(pg_pool, made)
    assert (row["bb_preset_id"], row["mirror_create_floor"]) == (None, 40)
    assert row["mirror_error"] is not None

    # Bambuddy made it as 42; 41 is another type's preset of the same name.
    first.mock(
        return_value=httpx.Response(
            200,
            json={
                "filament": [
                    _preset_json(40, "Lost"),
                    _preset_json(41, "Lost"),
                    _preset_json(42, "Lost"),
                ],
                "printer": [],
                "process": [],
            },
        )
    )
    detail(41, "Lost", {"name": "Lost", "filament_type": ["PETG"]})
    detail(42, "Lost", {"name": "Lost"})
    put = respx.put(f"{API}/local-presets/42").mock(
        return_value=httpx.Response(200, json=_preset_json(42, "Lost"))
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 1
    assert (create.call_count, put.call_count) == (1, 1)
    row = _row(pg_pool, made)
    assert (row["bb_preset_id"], row["mirror_create_floor"]) == (42, None)


@respx.mock
async def test_a_lost_create_never_takes_another_presets_copy_of_the_same_name(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    """The only preset of that name above the floor holds another document (another
    type's fetch of the same 3DFP name): it is never rewritten, and the preset is created
    again."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    made = await _make(pg_pool, blobs, {"name": "Twin", "filament_type": ["PLA"]})
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_presets SET mirror_create_floor = 40 WHERE id = %s", (made,))
    listed(_preset_json(41, "Twin"))
    detail(41, "Twin", {"name": "Twin", "filament_type": ["PETG"]})
    other = respx.put(f"{API}/local-presets/41")
    create = respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(43, "Twin"))
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 1
    assert (other.call_count, create.call_count) == (0, 1)
    assert _row(pg_pool, made)["bb_preset_id"] == 43


@respx.mock
async def test_an_id_another_preset_holds_is_that_presets_error_and_the_rest_are_sent(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    """Storing a Bambuddy id another ScadBuddy preset holds on this Bambuddy breaks the
    unique index: it is that preset's `mirror_error`, never the end of the run."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    held = await _make(pg_pool, blobs, {"name": "Held"}, bb_id=9, instance=HERE)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_presets SET mirrored_sha256 = sha256 WHERE id = %s", (held,))
    clash = await _make(pg_pool, blobs, {"name": "Clash"})
    after = await _make(pg_pool, blobs, {"name": "After"})
    listed()
    respx.post(f"{API}/local-presets/").mock(
        side_effect=[
            httpx.Response(200, json=_preset_json(9, "Clash")),
            httpx.Response(200, json=_preset_json(10, "After")),
        ]
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 1
    row = _row(pg_pool, clash)
    assert row["bb_preset_id"] is None
    assert row["mirror_error"] == "another preset holds Bambuddy preset 9"
    assert _row(pg_pool, after)["bb_preset_id"] == 10
    assert _row(pg_pool, held)["bb_preset_id"] == 9


@respx.mock
async def test_one_mirror_runs_at_a_time(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    """While another mirror holds the lock, a second sends nothing; the next one sends."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    await _make(pg_pool, blobs, {"name": "Once"})
    listed()
    create = respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(5, "Once"))
    )
    conninfo = pg_pool.conninfo
    held = try_lock_on(conninfo() if callable(conninfo) else str(conninfo), LOCK)
    assert held is not None
    try:
        assert await mirror_with(client, pg_pool, store, HERE) == 0
    finally:
        held.close()
    assert create.call_count == 0
    assert await mirror_with(client, pg_pool, store, HERE) == 1


@respx.mock
async def test_an_error_that_is_no_answer_is_kept_and_the_rest_are_sent(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    broken = await _make(pg_pool, blobs, {"name": "Broken"})
    fine = await _make(pg_pool, blobs, {"name": "Fine"})
    listed()
    real = client.create_local_preset

    async def create(name: str, preset_type: str, setting: Any) -> Any:
        if name == "Broken":
            raise RuntimeError("not an answer")
        return await real(name, preset_type, setting)

    monkeypatch.setattr(client, "create_local_preset", create)
    respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(6, "Fine"))
    )
    assert (
        await mirror_with(client, pg_pool, FilamentPresetStore(pg_pool, lambda: blobs), HERE) == 1
    )
    assert _row(pg_pool, broken)["mirror_error"] == "RuntimeError"
    assert _row(pg_pool, fine)["bb_preset_id"] == 6


@respx.mock
async def test_a_version_written_during_a_create_keeps_the_new_id(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-fetch writes version 2 while the create of version 1 is on its way: the id
    Bambuddy gave is kept, version 2 stays pending, and the next send rewrites that id."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    made = await _make(pg_pool, blobs, {"name": "Racing"})
    second = {"name": "Racing", "nozzle_temperature_range_low": ["200"]}
    key, sha256 = await keep_document(blobs, document_bytes(second))
    listed()
    real = client.create_local_preset

    async def create(name: str, preset_type: str, setting: Any) -> Any:
        answer = await real(name, preset_type, setting)
        with pg_pool.connection() as conn, conn.transaction():
            version_on(conn, made, name=name, key=key, sha256=sha256, doc=second, made_by="t")
        return answer

    monkeypatch.setattr(client, "create_local_preset", create)
    respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(88, "Racing"))
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 1
    row = _row(pg_pool, made)
    assert row["bb_preset_id"] == 88
    assert row["mirrored_sha256"] != row["sha256"] == sha256

    put = respx.put(f"{API}/local-presets/88").mock(
        return_value=httpx.Response(200, json=_preset_json(88, "Racing"))
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 1
    assert put.call_count == 1
    assert _row(pg_pool, made)["mirrored_sha256"] == sha256


#: The Bambuddy the filament sync's links name before the URL changes.
OLD = "0ld0ld0ld0ld"


def _links_name(pool: PgPool, instance: str | None) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE filament_bb_sync_state SET instance = %s", (instance,))


def _refs(pool: PgPool, preset_id: int) -> str | None:
    ref = f"sb-{preset_id}"
    with pool.connection() as conn:
        return bambuddy_refs_on(conn, [ref])[ref]


@respx.mock
async def test_after_a_url_change_the_mirror_waits_for_the_reset_then_creates(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    """The URL changed to another Bambuddy: until the links are reset, the mirror sends
    nothing and no path hands Bambuddy the old id; a reset that deletes the links drops
    the ids, and the next mirror creates each preset on the new Bambuddy, one made in
    the old Bambuddy too."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    ours = await _make(pg_pool, blobs, {"name": "Ours"}, bb_id=7, instance=OLD)
    theirs = await _make(
        pg_pool, blobs, {"name": "Theirs"}, source="bambuddy", bb_id=8, instance=OLD
    )
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_presets SET mirrored_sha256 = sha256")
    _links_name(pg_pool, OLD)
    assert _refs(pg_pool, ours) == "7"
    listed()
    create = respx.post(f"{API}/local-presets/").mock(
        side_effect=[
            httpx.Response(200, json=_preset_json(31, "Ours")),
            httpx.Response(200, json=_preset_json(32, "Theirs")),
        ]
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 0
    assert create.call_count == 0

    reset_links(_filament(pg_pool), OLD, HERE, None, expect_kept=False)
    assert _refs(pg_pool, ours) is None
    assert await mirror_with(client, pg_pool, store, HERE) == 2
    assert [_row(pg_pool, i)["bb_preset_id"] for i in (ours, theirs)] == [31, 32]
    assert _refs(pg_pool, ours) == "31"


def _instance(pool: PgPool) -> str | None:
    with pool.connection() as conn:
        row = conn.execute("SELECT instance FROM filament_bb_sync_state").fetchone()
    assert row is not None
    return row["instance"]  # type: ignore[no-any-return]


@respx.mock
async def test_with_only_preset_links_a_url_change_waits_for_the_reset(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    """No spool, supplier or other row links Bambuddy; the mirror made the only links.
    The first create records their Bambuddy, so after the URL changes the mirror sends
    nothing until a person resets the links; a confirmed delete drops the ids, and each
    preset is created on the new one."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    made = await _make(pg_pool, blobs, {"name": "Only"})
    assert _instance(pg_pool) is None
    old_base, new_base = "https://old.test", BASE
    respx.get(f"{old_base}/api/v1/local-presets/").mock(
        return_value=httpx.Response(200, json={"filament": [], "printer": [], "process": []})
    )
    respx.post(f"{old_base}/api/v1/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(7, "Only"))
    )
    async with BambuddyClient(BambuddyConfig(base_url=old_base, api_key="k")) as old:
        assert await mirror_with(old, pg_pool, store, OLD) == 1
    assert _instance(pg_pool) == OLD
    assert (_row(pg_pool, made)["bb_preset_id"], _row(pg_pool, made)["bb_instance"]) == (7, OLD)

    # The URL changes; a new version is pending too.
    second = {"name": "Only", "nozzle_temperature_range_low": ["200"]}
    key, sha256 = await keep_document(blobs, document_bytes(second))
    with pg_pool.connection() as conn, conn.transaction():
        version_on(conn, made, name="Only", key=key, sha256=sha256, doc=second, made_by="t")
    listed()
    create = respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(31, "Only"))
    )
    put = respx.put(f"{API}/local-presets/7")
    async with BambuddyClient(BambuddyConfig(base_url=new_base, api_key="k")) as new:
        assert await mirror_with(new, pg_pool, store, HERE) == 0
        assert (create.call_count, put.call_count) == (0, 0)

        preview = reset_preview(_filament(pg_pool), HERE, None)
        assert (preview.instance, preview.counts.presets, preview.counts.links()) == (OLD, 1, 1)
        done = reset_links(_filament(pg_pool), OLD, HERE, None, expect_kept=False)
        assert (done.instance, done.kept, done.deleted.presets) == (OLD, False, 1)
        row = _row(pg_pool, made)
        assert (row["bb_preset_id"], row["bb_instance"]) == (None, None)

        assert await mirror_with(new, pg_pool, store, HERE) == 1
    assert (create.call_count, put.call_count) == (1, 0)
    row = _row(pg_pool, made)
    assert (row["bb_preset_id"], row["bb_instance"], row["mirrored_sha256"]) == (31, HERE, sha256)
    assert _instance(pg_pool) == HERE


#: When the mirror's write last changed a preset in Bambuddy, naive UTC as Bambuddy sends it.
STAMP = "2026-11-23T08:00:00"


def _stamped(preset_id: int, name: str, updated_at: str = STAMP) -> dict[str, Any]:
    return {**_preset_json(preset_id, name), "updated_at": updated_at}


def _stamp_of(pool: PgPool, preset_id: int) -> str | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT bb_updated_at FROM filament_presets WHERE id = %s", (preset_id,)
        ).fetchone()
    assert row is not None
    return row["bb_updated_at"]  # type: ignore[no-any-return]


@respx.mock
async def test_the_mirror_keeps_bambuddys_time_of_each_write(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    """#7343: each create and rewrite keeps the `updated_at` Bambuddy answers, which a
    reset after a URL change compares to prove the same Bambuddy from the presets."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    made = await _make(pg_pool, blobs, {"name": "Stamped"})
    listed()
    respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_stamped(60, "Stamped"))
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 1
    assert _stamp_of(pg_pool, made) == STAMP

    second = {"name": "Stamped", "nozzle_temperature_range_low": ["200"]}
    key, sha256 = await keep_document(blobs, document_bytes(second))
    with pg_pool.connection() as conn, conn.transaction():
        version_on(conn, made, name="Stamped", key=key, sha256=sha256, doc=second, made_by="t")
    respx.put(f"{API}/local-presets/60").mock(
        return_value=httpx.Response(200, json=_stamped(60, "Stamped", "2026-11-23T09:00:00"))
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 1
    assert _stamp_of(pg_pool, made) == "2026-11-23T09:00:00"


def _catalogue(*presets: dict[str, Any]) -> list[LocalPreset]:
    return LocalPresetCatalogue.model_validate(
        {"filament": list(presets), "printer": [], "process": []}
    ).filament


async def _preset_links_only(pool: PgPool, blobs: LocalBlobStore) -> tuple[int, int]:
    """Two presets the mirror sent to Bambuddy ``OLD`` (ids 7 and 8, both stamped), the
    only links; the URL then changed."""
    first = await _make(pool, blobs, {"name": "One"}, bb_id=7, instance=OLD)
    second = await _make(pool, blobs, {"name": "Two"}, bb_id=8, instance=OLD)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_presets SET mirrored_sha256 = sha256, bb_updated_at = %s", (STAMP,)
        )
    _links_name(pool, OLD)
    return first, second


async def test_with_only_preset_links_the_presets_prove_the_same_bambuddy(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    """#7343 item 1: no spool links; the configured Bambuddy lists every linked preset id,
    none older than ScadBuddy's stamp and one exactly as stamped, so the reset keeps the
    links: the ids move to the configured Bambuddy and nothing is created again."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    first, second = await _preset_links_only(pg_pool, blobs)
    presets = _catalogue(_stamped(7, "One"), _stamped(8, "Two", "2026-11-23T10:00:00"))
    files = {"H2C": {"0.4": {"key": "k", "name": "n", "sha256": "s", "preset_id": "7"}}}
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_profile_fetches (type_id, tdfp_id, files, status)"
            " VALUES (%s, 30173, %s, 'imported')",
            (_type(pg_pool, "PLA-K"), Jsonb(files)),
        )

    preview = reset_preview(_filament(pg_pool), HERE, ([], []), presets=presets)
    assert (preview.same_bambuddy, preview.changed_since_pull) == (True, False)

    done = reset_links(_filament(pg_pool), OLD, HERE, ([], []), presets=presets, expect_kept=True)
    assert (done.kept, done.configured_instance) == (True, HERE)
    # #7223: the same Bambuddy, so a fetch run's preset ids stay.
    with pg_pool.connection() as conn:
        row = conn.execute("SELECT files FROM filament_profile_fetches").fetchone()
    assert row is not None
    assert row["files"] == files
    rows = [_row(pg_pool, i) for i in (first, second)]
    assert [(r["bb_preset_id"], r["bb_instance"]) for r in rows] == [(7, HERE), (8, HERE)]
    assert [r["mirrored_sha256"] == r["sha256"] for r in rows] == [True, True]
    assert _instance(pg_pool) == HERE


@pytest.mark.parametrize(
    ("listed_presets", "changed"),
    [
        # Another Bambuddy: preset 8 is not there.
        ([_stamped(7, "One")], False),
        # A row older than ScadBuddy's stamp: an older copy, or another Bambuddy.
        ([_stamped(7, "One"), _stamped(8, "Two", "2026-11-23T07:00:00")], False),
        # Both edited in Bambuddy since: nothing matches exactly.
        (
            [
                _stamped(7, "One", "2026-11-23T09:00:00"),
                _stamped(8, "Two", "2026-11-23T09:00:00"),
            ],
            True,
        ),
    ],
)
async def test_presets_that_do_not_prove_the_same_bambuddy_offer_the_delete_only(
    pg_pool: PgPool, tmp_path: Path, listed_presets: list[dict[str, Any]], changed: bool
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    await _preset_links_only(pg_pool, blobs)
    presets = _catalogue(*listed_presets)

    preview = reset_preview(_filament(pg_pool), HERE, ([], []), presets=presets)
    assert (preview.same_bambuddy, preview.changed_since_pull) == (False, changed)
    with pytest.raises(ResetRefusedError) as refused:
        reset_links(_filament(pg_pool), OLD, HERE, ([], []), presets=presets, expect_kept=True)
    assert refused.value.code == "proof_changed"
    assert _instance(pg_pool) == OLD


@pytest.mark.parametrize(
    ("listed_at", "proven"),
    [(STAMP, (True, False)), ("2026-11-23T09:00:00", (False, True))],
)
async def test_a_preset_imported_from_bambuddy_proves_it_by_its_import_time(
    pg_pool: PgPool, tmp_path: Path, listed_at: str, proven: tuple[bool, bool]
) -> None:
    """#7347 review: a ``bambuddy`` preset is never written by the mirror; its time is
    Bambuddy's ``updated_at`` as its import took it. Listed at that time it proves the
    same Bambuddy; edited in Bambuddy since, it proves nothing, as for a mirrored one."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    held = MemoryPresets()
    await held.keep(_held(7, "Imported", STAMP))
    _bind(pg_pool, _type(pg_pool, "PLA-I"), "7")
    assert await adopt_held(pg_pool, blobs, held, OLD) == 1
    with pg_pool.connection() as conn:
        row = conn.execute("SELECT id, source, bb_updated_at FROM filament_presets").fetchone()
    assert row is not None
    assert (row["source"], row["bb_updated_at"]) == ("bambuddy", STAMP)
    assert _instance(pg_pool) == OLD
    presets = _catalogue(_stamped(7, "Imported", listed_at))

    preview = reset_preview(_filament(pg_pool), HERE, ([], []), presets=presets)
    assert (preview.same_bambuddy, preview.changed_since_pull) == proven
    if proven[0]:
        done = reset_links(
            _filament(pg_pool), OLD, HERE, ([], []), presets=presets, expect_kept=True
        )
        assert done.kept is True
        assert (_row(pg_pool, row["id"])["bb_instance"], _instance(pg_pool)) == (HERE, HERE)
    else:
        with pytest.raises(ResetRefusedError) as refused:
            reset_links(_filament(pg_pool), OLD, HERE, ([], []), presets=presets, expect_kept=True)
        assert refused.value.code == "proof_changed"
        assert _instance(pg_pool) == OLD


async def test_untimed_preset_links_prove_nothing(pg_pool: PgPool, tmp_path: Path) -> None:
    """Presets the mirror sent before it kept Bambuddy's time have no stamp: at least
    half the links must be timed on both sides, as for spools, or nothing is proven."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    await _preset_links_only(pg_pool, blobs)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_presets SET bb_updated_at = NULL")
    presets = _catalogue(_stamped(7, "One"), _stamped(8, "Two"))
    preview = reset_preview(_filament(pg_pool), HERE, ([], []), presets=presets)
    assert preview.same_bambuddy is False


@respx.mock
async def test_a_lost_create_left_behind_a_newer_version_is_named_on_the_preset(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    """#7343 item 2: a create's answer was lost, then a new version was written, so no
    preset above the floor holds the document now sent and it is created again. The
    copy the lost create made stays in Bambuddy untracked: its id is the preset's
    `mirror_error` note. An id of that name another ScadBuddy preset holds is not named."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    made = await _make(pg_pool, blobs, {"name": "Twice", "nozzle_temperature_range_low": ["200"]})
    await _make(
        pg_pool, blobs, {"name": "Twice", "filament_type": ["PETG"]}, bb_id=41, instance=HERE
    )
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_presets SET mirrored_sha256 = sha256 WHERE bb_preset_id = 41")
        conn.execute("UPDATE filament_presets SET mirror_create_floor = 40 WHERE id = %s", (made,))
    listed(_preset_json(41, "Twice"), _preset_json(42, "Twice"))
    detail(41, "Twice", {"name": "Twice", "filament_type": ["PETG"]})
    detail(42, "Twice", {"name": "Twice"})
    create = respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(43, "Twice"))
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 1
    assert create.call_count == 1
    row = _row(pg_pool, made)
    assert (row["bb_preset_id"], row["mirrored_sha256"]) == (43, row["sha256"])
    assert row["mirror_error"] is not None
    assert "42" in row["mirror_error"]
    assert "41" not in row["mirror_error"]


@respx.mock
async def test_a_same_name_preset_made_by_hand_after_a_lost_create_is_named_too(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    """#7545: Bambuddy keeps no mark of who made a preset, so a preset of the same name a
    person made in Bambuddy after the lost create (43, another document) is named with
    the lost create's copy (42). The note says to keep a preset made by hand."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    made = await _make(pg_pool, blobs, {"name": "Hand", "nozzle_temperature_range_low": ["200"]})
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_presets SET mirror_create_floor = 40 WHERE id = %s", (made,))
    listed(_preset_json(42, "Hand"), _preset_json(43, "Hand"))
    detail(42, "Hand", {"name": "Hand"})
    detail(43, "Hand", {"name": "Hand", "filament_type": ["PLA"], "made": ["by hand"]})
    create = respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(44, "Hand"))
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 1
    assert create.call_count == 1
    row = _row(pg_pool, made)
    assert row["bb_preset_id"] == 44
    note = row["mirror_error"]
    assert note is not None
    assert "preset 42, 43 named 'Hand'" in note
    assert "you did not make it there yourself" in note


@respx.mock
async def test_copies_of_the_document_sent_and_other_bambuddys_ids_are_named_right(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    """#7343 review: two copies above the floor that both hold the document sent are not
    named as an older version's (none is chosen, the preset is created again, no note); an
    id another Bambuddy's preset holds does not hide an untracked copy on this one."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    doc = {"name": "Dup"}
    made = await _make(pg_pool, blobs, doc)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_presets SET mirror_create_floor = 40 WHERE id = %s", (made,))
    listed(_preset_json(41, "Dup"), _preset_json(42, "Dup"))
    detail(41, "Dup", doc)
    detail(42, "Dup", doc)
    create = respx.post(f"{API}/local-presets/").mock(
        side_effect=[
            httpx.Response(200, json=_preset_json(43, "Dup")),
            httpx.Response(200, json=_preset_json(53, "Other")),
        ]
    )
    assert await mirror_with(client, pg_pool, store, HERE) == 1
    row = _row(pg_pool, made)
    assert (row["bb_preset_id"], row["mirror_error"]) == (43, None)

    # Preset 51 on this Bambuddy is untracked; another Bambuddy's preset holds id 51 there.
    other = await _make(pg_pool, blobs, {"name": "Other", "v": 2})
    await _make(pg_pool, blobs, {"name": "Elsewhere"}, source="bambuddy", bb_id=51, instance=OLD)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_presets SET mirror_create_floor = 50 WHERE id = %s", (other,))
    listed(_preset_json(51, "Other"))
    detail(51, "Other", {"name": "Other", "v": 1})
    assert await mirror_with(client, pg_pool, store, HERE) == 1
    assert create.call_count == 2
    note = _row(pg_pool, other)["mirror_error"]
    assert note is not None and "51" in note
