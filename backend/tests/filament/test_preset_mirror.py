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
from scadbuddy.filament.preset_mirror import mirror_pending, mirror_with, wants_mirror
from scadbuddy.filament.sb_presets import (
    FilamentPresetStore,
    create_on,
    document_bytes,
    keep_document,
    version_on,
)
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.store.local import LocalBlobStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"


def _preset_json(preset_id: int, name: str) -> dict[str, Any]:
    return {"id": preset_id, "name": name, "preset_type": "filament", "source": "local"}


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
        )


def _row(pool: PgPool, preset_id: int) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT bb_preset_id, sha256, mirrored_sha256, mirror_error FROM filament_presets"
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
    create = respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(141, "Silk @H2C"))
    )
    assert await mirror_with(client, pg_pool, store) == 1
    assert json.loads(create.calls[0].request.content) == {
        "name": "Silk @H2C",
        "preset_type": "filament",
        "setting": doc,
    }
    row = _row(pg_pool, made)
    assert (row["bb_preset_id"], row["mirrored_sha256"]) == (141, row["sha256"])
    # In step: nothing is sent again.
    assert await mirror_with(client, pg_pool, store) == 0

    second = {**doc, "nozzle_temperature_range_low": ["200"]}
    key, sha256 = await keep_document(blobs, document_bytes(second))
    with pg_pool.connection() as conn, conn.transaction():
        version_on(conn, made, name="Silk @H2C", key=key, sha256=sha256, doc=second, made_by="t")
    update = respx.put(f"{API}/local-presets/141").mock(
        return_value=httpx.Response(200, json=_preset_json(141, "Silk @H2C"))
    )
    assert await mirror_with(client, pg_pool, store) == 1
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
    respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=_preset_json(77, "Gone"))
    )
    assert await mirror_with(client, pg_pool, FilamentPresetStore(pg_pool, lambda: blobs)) == 1
    assert _row(pg_pool, made)["bb_preset_id"] == 77


@respx.mock
async def test_a_refusal_is_kept_on_the_preset_and_bambuddys_own_are_never_sent(
    pg_pool: PgPool, tmp_path: Path, client: BambuddyClient
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    refused = await _make(pg_pool, blobs, {"name": "Refused"})
    theirs = await _make(pg_pool, blobs, {"name": "Theirs"}, source="bambuddy", bb_id=9)
    route = respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(403, json={"detail": "Forbidden"})
    )
    assert await mirror_with(client, pg_pool, store) == 0
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
