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

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.filament.bb_sync_results import reset_links, reset_preview
from scadbuddy.filament.preset_mirror import (
    LOCK,
    mirror_pending,
    mirror_with,
    wants_mirror,
)
from scadbuddy.filament.rfid_push import try_lock_on
from scadbuddy.filament.sb_presets import (
    FilamentPresetStore,
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
    nothing until a person resets the links, which drops the ids (no spool link proves
    the same Bambuddy); then each preset is created on the new one."""
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
