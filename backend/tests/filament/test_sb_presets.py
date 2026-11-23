"""ScadBuddy's own filament presets (#7176): the store, the ``sb-`` ref's foreign key,
adoption of the bindings that name a Bambuddy local preset, and the refresh of a preset
Bambuddy owns."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from psycopg import errors
from psycopg.types.json import Jsonb

from scadbuddy.bambuddy.models import LocalPresetDetail
from scadbuddy.filament import sb_presets
from scadbuddy.filament.models import TypePreset
from scadbuddy.filament.sb_presets import (
    FilamentPresetStore,
    adopt_held,
    bambuddy_refs_on,
    create_on,
    document_bytes,
    keep_document,
    refresh_bambuddy,
    version_on,
)
from scadbuddy.filament.types import PresetUnknownError, SpoolTypeStore
from scadbuddy.slicing.held_presets import HeldPresetStore
from scadbuddy.slicing.own_presets import sb_id, sb_ref
from scadbuddy.store.local import LocalBlobStore
from tests.conftest import PgPool
from tests.filament.test_types import _body

pytestmark = pytest.mark.requires_postgres


def _doc(name: str, low: str = "210", **over: Any) -> dict[str, Any]:
    return {
        "name": name,
        "inherits": "",
        "filament_type": ["PLA"],
        "default_filament_colour": ["#FFFFFF"],
        "compatible_printers": ["Bambu Lab H2C 0.4 nozzle"],
        "nozzle_temperature_range_low": [low],
        **over,
    }


def _held(bb_id: int, name: str, stamp: str | None, **over: Any) -> LocalPresetDetail:
    return LocalPresetDetail(
        id=bb_id,
        name=name,
        preset_type="filament",
        source="orcaslicer",
        updated_at=stamp,
        setting=_doc(name, **over),
    )


def _bind(pool: PgPool, type_id: UUID, ref: str, nozzle: str = "0.4") -> None:
    SpoolTypeStore(pool).set_presets(
        type_id,
        [
            TypePreset(
                type_id=type_id,
                printer_model="H2C",
                nozzle_diameter=nozzle,
                slicer_filament=ref,
                slicer_filament_name="shown",
            )
        ],
        actor="ann",
    )


def _type(pool: PgPool, code: str) -> UUID:
    return SpoolTypeStore(pool).create(_body(pool, code=code, tdfp_id=30173)).id


def _bindings(pool: PgPool, type_id: UUID) -> list[tuple[str, int | None]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT slicer_filament, preset_id FROM filament_spool_type_presets"
            " WHERE type_id = %s ORDER BY nozzle_diameter",
            (type_id,),
        ).fetchall()
    return [(row["slicer_filament"], row["preset_id"]) for row in rows]


def test_a_type_or_spool_naming_no_such_preset_is_refused(pg_pool: PgPool) -> None:
    """A 422 on the routes (`api/filament.py` `_TYPE_REFUSED`, `_SPOOL_REFUSED`)."""
    type_id = _type(pg_pool, "PLA-X")
    with pytest.raises(PresetUnknownError):
        _bind(pg_pool, type_id, "sb-404")
    assert _bindings(pg_pool, type_id) == []


def test_refs_read_only_the_sb_form() -> None:
    assert sb_ref(12) == "sb-12"
    assert sb_id("sb-12") == 12
    for other in ("12", "sb-0", "sb-", "sb-01", "SB-1", "GFA00", None, "sb-1x"):
        assert sb_id(other) is None


async def test_a_binding_names_only_a_preset_that_exists(pg_pool: PgPool, tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    type_id = _type(pg_pool, "PLA-A")
    with pytest.raises(errors.ForeignKeyViolation), pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_spool_type_presets (type_id, printer_model,"
            " nozzle_diameter, slicer_filament) VALUES (%s, 'H2C', '0.4', 'sb-77')",
            (type_id,),
        )
    doc = _doc("Made")
    key, sha256 = await keep_document(blobs, document_bytes(doc))
    with pg_pool.connection() as conn, conn.transaction():
        made = create_on(
            conn, name="Made", source="manual", key=key, sha256=sha256, doc=doc, made_by="t"
        )
    _bind(pg_pool, type_id, sb_ref(made))
    assert _bindings(pg_pool, type_id) == [(sb_ref(made), made)]
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    rows = await store.rows()
    assert [(r.id, r.filament_type, r.filament_colour, r.compatible_printers) for r in rows] == [
        (made, "PLA", "#FFFFFF", ["Bambu Lab H2C 0.4 nozzle"])
    ]
    assert await store.documents([made, 999]) == {made: doc}


async def test_a_new_document_is_a_new_version_of_the_same_preset(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    first = _doc("P")
    key, sha256 = await keep_document(blobs, document_bytes(first))
    with pg_pool.connection() as conn, conn.transaction():
        made = create_on(
            conn, name="P", source="manual", key=key, sha256=sha256, doc=first, made_by="t"
        )
        assert (
            version_on(conn, made, name="P", key=key, sha256=sha256, doc=first, made_by="t") is None
        )
    second = _doc("P", low="205")
    key2, sha2 = await keep_document(blobs, document_bytes(second))
    with pg_pool.connection() as conn, conn.transaction():
        assert version_on(conn, made, name="P", key=key2, sha256=sha2, doc=second, made_by="t") == 2
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    got = store.get(made)
    assert got is not None and (got.version, got.sha256) == (2, sha2)
    assert await store.documents([made]) == {made: second}
    with pg_pool.connection() as conn:
        holds = conn.execute(
            "SELECT key, holder_id FROM blob_refs WHERE holder_kind = 'filament_preset'"
            " ORDER BY holder_id"
        ).fetchall()
    # The old version's blob stays held, so it stays readable.
    assert [(h["key"], h["holder_id"]) for h in holds] == [(key, f"{made}:1"), (key2, f"{made}:2")]


async def test_adoption_turns_bambuddy_ids_into_scadbuddy_presets(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    held = HeldPresetStore(pg_pool)
    fetched_type = _type(pg_pool, "PLA-F")
    person_type = _type(pg_pool, "PLA-P")
    unheld_type = _type(pg_pool, "PLA-U")
    await held.keep(_held(141, "SUNLU Silk @H2C", "2026-11-23T01:00:00"))
    await held.keep(_held(7, "Mine", "2026-11-20T01:00:00"))
    _bind(pg_pool, fetched_type, "141")
    _bind(pg_pool, person_type, "7")
    _bind(pg_pool, unheld_type, "300")
    with pg_pool.connection() as conn:
        run = conn.execute(
            "INSERT INTO filament_profile_fetches (type_id, tdfp_id, files, status)"
            " VALUES (%s, 30173, %s, 'imported') RETURNING id",
            (
                fetched_type,
                Jsonb(
                    {"H2C": {"0.4": {"key": "k", "name": "n", "sha256": "s", "preset_id": "141"}}}
                ),
            ),
        ).fetchone()
    assert run is not None

    assert await adopt_held(pg_pool, blobs, held) == 2
    assert await adopt_held(pg_pool, blobs, held) == 0

    store = FilamentPresetStore(pg_pool, lambda: blobs)
    by_bb = {p.bb_preset_id: p for p in store.all()}
    fetched, person = by_bb[141], by_bb[7]
    assert (fetched.source, fetched.type_id, fetched.printer_model, fetched.nozzle_diameter) == (
        "3dfp",
        fetched_type,
        "H2C",
        "0.4",
    )
    assert (fetched.fetch_run_id, fetched.tdfp_id) == (run["id"], 30173)
    assert (person.source, person.type_id, person.bb_updated_at) == (
        "bambuddy",
        None,
        "2026-11-20T01:00:00",
    )
    # Bambuddy holds the document it came from: the mirror is in step.
    assert fetched.mirrored_sha256 == fetched.sha256
    assert _bindings(pg_pool, fetched_type) == [(sb_ref(fetched.id), fetched.id)]
    assert _bindings(pg_pool, person_type) == [(sb_ref(person.id), person.id)]
    # Not in the kept copy: left for a later refresh, still Bambuddy's id.
    assert _bindings(pg_pool, unheld_type) == [("300", None)]
    assert (await store.documents([person.id]))[person.id] == _doc("Mine")
    # A binding that names it again later (the pull) is moved to the same preset.
    later = _type(pg_pool, "PLA-L")
    _bind(pg_pool, later, "7")
    assert await adopt_held(pg_pool, blobs, held) == 0
    assert _bindings(pg_pool, later) == [(sb_ref(person.id), person.id)]
    with pg_pool.connection() as conn:
        assert bambuddy_refs_on(conn, [sb_ref(person.id), "GFA00", "sb-999"]) == {
            sb_ref(person.id): "7",
            "sb-999": None,
        }


async def test_one_preset_that_cannot_be_adopted_never_stops_the_rest(
    pg_pool: PgPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Bambuddy local preset whose row ScadBuddy refuses keeps its bindings' Bambuddy
    id; the ids after it are still adopted."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    held = HeldPresetStore(pg_pool)
    refused, kept = _type(pg_pool, "PLA-R"), _type(pg_pool, "PLA-K")
    await held.keep(_held(5, "Refused", "2026-11-20T01:00:00"))
    await held.keep(_held(9, "Kept", "2026-11-20T01:00:00"))
    _bind(pg_pool, refused, "5")
    _bind(pg_pool, kept, "9")
    real = sb_presets._adopt_one

    def refusing(pool: Any, detail: LocalPresetDetail, *args: Any) -> bool:
        if detail.id == 5:
            raise errors.CheckViolation("name too long")
        return real(pool, detail, *args)

    monkeypatch.setattr(sb_presets, "_adopt_one", refusing)
    assert await adopt_held(pg_pool, blobs, held) == 1
    assert _bindings(pg_pool, refused) == [("5", None)]
    [(ref, _)] = _bindings(pg_pool, kept)
    assert sb_id(ref) is not None


async def test_a_bambuddy_edit_is_a_new_version_of_its_preset(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    held = HeldPresetStore(pg_pool)
    type_id = _type(pg_pool, "PLA-B")
    await held.keep(_held(7, "Mine", "2026-11-20T01:00:00"))
    _bind(pg_pool, type_id, "7")
    await adopt_held(pg_pool, blobs, held)
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    (preset,) = store.all()
    assert await refresh_bambuddy(pg_pool, blobs, held) == 0

    await held.keep(_held(7, "Mine", "2026-11-23T02:00:00", low="205"))
    assert await refresh_bambuddy(pg_pool, blobs, held) == 1
    now = store.get(preset.id)
    assert now is not None
    assert (now.version, now.bb_updated_at) == (2, "2026-11-23T02:00:00")
    assert now.mirrored_sha256 == now.sha256
    doc = (await store.documents([preset.id]))[preset.id]
    assert doc["nozzle_temperature_range_low"] == ["205"]
    # A moved stamp with the same document makes no version.
    await held.keep(_held(7, "Mine", "2026-11-23T03:00:00", low="205"))
    assert await refresh_bambuddy(pg_pool, blobs, held) == 0
    again = store.get(preset.id)
    assert again is not None and (again.version, again.bb_updated_at) == (2, "2026-11-23T03:00:00")


async def test_a_preset_with_no_stamp_writes_no_blob_while_unchanged(
    pg_pool: PgPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kept preset Bambuddy sent with no ``updated_at`` is read at every refresh; its
    unchanged document keeps no new blob, and a changed one is a new version."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    held = HeldPresetStore(pg_pool)
    await held.keep(_held(8, "Stampless", None))
    _bind(pg_pool, _type(pg_pool, "PLA-N"), "8")
    await adopt_held(pg_pool, blobs, held)
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    (preset,) = store.all()

    kept: list[bytes] = []
    real = sb_presets.keep_document

    async def counting(store_: Any, body: bytes) -> tuple[str, str]:
        kept.append(body)
        return await real(store_, body)

    monkeypatch.setattr(sb_presets, "keep_document", counting)
    assert await refresh_bambuddy(pg_pool, blobs, held) == 0
    assert kept == []

    await held.keep(_held(8, "Stampless", None, low="200"))
    assert await refresh_bambuddy(pg_pool, blobs, held) == 1
    now = store.get(preset.id)
    assert now is not None and now.version == 2
    assert len(kept) == 1


async def test_another_bambuddys_preset_is_never_refreshed_or_reused(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    """After the Bambuddy URL changes, the kept copy's id 7 is another preset: the old
    Bambuddy's preset 7 takes no version from it, and a binding naming 7 gets a preset of
    its own."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    held = HeldPresetStore(pg_pool)
    doc = _doc("Old seven")
    key, sha256 = await keep_document(blobs, document_bytes(doc))
    with pg_pool.connection() as conn, conn.transaction():
        old = create_on(
            conn,
            name="Old seven",
            source="bambuddy",
            key=key,
            sha256=sha256,
            doc=doc,
            made_by="t",
            bb_preset_id=7,
            bb_instance="0ld0ld0ld0ld",
            bb_updated_at="2026-11-20T01:00:00",
        )
    await held.keep(_held(7, "New seven", "2026-11-23T01:00:00", low="190"))
    assert await refresh_bambuddy(pg_pool, blobs, held, "n3wn3wn3wn3w") == 0
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    before = store.get(old)
    assert before is not None and before.version == 1

    type_id = _type(pg_pool, "PLA-I")
    _bind(pg_pool, type_id, "7")
    assert await adopt_held(pg_pool, blobs, held, "n3wn3wn3wn3w") == 1
    [(ref, _)] = _bindings(pg_pool, type_id)
    made = store.get(sb_id(ref) or 0)
    assert made is not None and made.id != old
    assert (made.name, made.bb_instance) == ("New seven", "n3wn3wn3wn3w")


async def test_an_adopted_id_records_the_links_instance(pg_pool: PgPool, tmp_path: Path) -> None:
    """A preset's Bambuddy id is a link (#7176): when presets are the only links, the
    adoption records their Bambuddy, so a URL change later waits for a reset."""
    blobs = LocalBlobStore(tmp_path / "blobs")
    held = HeldPresetStore(pg_pool)
    await held.keep(_held(7, "Mine", "2026-11-20T01:00:00"))
    _bind(pg_pool, _type(pg_pool, "PLA-R"), "7")
    assert await adopt_held(pg_pool, blobs, held, "a1b2c3d4e5f6") == 1
    with pg_pool.connection() as conn:
        state = conn.execute("SELECT instance FROM filament_bb_sync_state").fetchone()
    assert state is not None and state["instance"] == "a1b2c3d4e5f6"


async def test_a_scadbuddy_owned_preset_is_never_read_back(pg_pool: PgPool, tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    held = HeldPresetStore(pg_pool)
    doc = _doc("Ours")
    key, sha256 = await keep_document(blobs, document_bytes(doc))
    with pg_pool.connection() as conn, conn.transaction():
        made = create_on(
            conn,
            name="Ours",
            source="3dfp",
            key=key,
            sha256=sha256,
            doc=doc,
            made_by="t",
            bb_preset_id=50,
            bb_updated_at="2026-11-20T01:00:00",
        )
    await held.keep(_held(50, "Ours", "2026-11-23T01:00:00", low="190"))
    assert await refresh_bambuddy(pg_pool, blobs, held) == 0
    store = FilamentPresetStore(pg_pool, lambda: blobs)
    assert json.dumps((await store.documents([made]))[made]) == json.dumps(doc)
