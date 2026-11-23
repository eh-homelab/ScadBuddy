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


def _held(bb_id: int, name: str, stamp: str, **over: Any) -> LocalPresetDetail:
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
