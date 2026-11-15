"""Bambuddy's spools pulled into ScadBuddy's own (#6261, spec §8.1, §8.2). Read only:
nothing here writes to Bambuddy (SB to BB is #6263).

`pull` takes each BB spool in its own transaction, under an advisory lock on its id, in
this order:

1. A `filament_bb_links` row names it: the SB spool takes what BB changed since the last
   pull. "Changed" compares BB's values with the ones the last pull saw, kept in the
   ``data.bb`` of the spool's newest ``synced_from_bb`` event; a BB row whose
   ``updated_at`` is not past the link's ``bb_updated_at`` is not looked at. Per field
   the newer change wins (§8.2): a field SB edited (an ``edited`` event) after BB's
   ``updated_at`` keeps SB's value. A value BB no longer has never clears SB's, and the
   fields BB does not have (note, location, bed temperatures ...) are never touched. A
   change of what is left (``label_weight - weight_used``) is a ``bb`` absolute reading
   on the ``synced_from_bb`` event, unless the SB spool's weight is locked: then it is
   recorded in the event's data and never moves ``grams_left``, as an ``ams_remain``
   reading is not under the lock (`filament/ledger.py`).
2. Else an SB spool already is that spool: it holds the BB spool's ``tag_uid`` or
   ``tray_uuid`` through an open binding, or the BB record's `spool_record_links` row
   (provider ``bambuddy``) names it (a native spool migrated onto its physical spool's
   id, `20261114T1302Z_filament_from_native.sql`). It is linked; a spool that needs a
   type takes the one BB's values match, and one with no weight takes BB's.
3. Else a new SB spool: its type by ``material_number`` (`SpoolTypeStore.match`), else
   by identity (brand, material, subtype, colour, label weight), else none (it needs a
   type) and BB's values as its own. A spool with a type overrides only what BB says
   differently. It takes BB's tags unless another linked spool holds them, a
   ``created`` event with a ``bb`` reading of what is left, and its link.

Catalog names from BB (material, subtype, brand, category, effect) resolve by their upper
case name, and are added when missing. BB's ``core_weight_catalog_id`` names a BB spool
catalog entry that has no SB insert type yet, so the insert type is not pulled.

A pulled weight is an absolute reading taken at the pull, so a print both SB and BB
booked is counted once: BB's value already holds it, and SB's ``print_actual`` before the
reading is under the new anchor (§8.2, "Consumption is counted once").
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.filament import ledger
from scadbuddy.filament import models as m
from scadbuddy.filament.colour import normalise_rgba
from scadbuddy.filament.component import Filament
from scadbuddy.filament.spools import FilamentSpoolStore, TagKeysClashError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.spools.models import clean_tag_uid, clean_tray_uuid

logger = logging.getLogger(__name__)

Conn = Connection[DictRow]

#: Who the pull's events name.
ACTOR = "bambuddy"
#: The ``ref_kind`` of the pull's events; the ``ref_id`` is the BB spool id, with BB's
#: ``updated_at`` on a ``synced_from_bb`` event, so a retried pull books nothing twice.
REF_KIND = "bb_spool"
#: The advisory lock's first key: with the BB spool id, two pulls (the Schedule's and a
#: person's import) take one BB spool in turn.
_LOCK = 6261
#: Catalog rows the pull adds sort after the seeded ones, as the native migration's do.
_ADDED_SORT = 1000

#: A BB field (as the snapshot names it) and the catalog table it names a row of.
_CATALOGS: dict[str, tuple[str, str]] = {
    "material": ("material_id", "filament_materials"),
    "subtype": ("subtype_id", "filament_subtypes"),
    "brand": ("brand_id", "filament_brands"),
    "category": ("category_id", "filament_categories"),
    "effect": ("effect_id", "filament_effects"),
}
#: The BB fields that are an SB column as they are.
_PLAIN = (
    "colour_name",
    "rgba",
    "extra_colours",
    "label_weight_g",
    "cost_per_kg",
    "nozzle_temp_min",
    "nozzle_temp_max",
    "weight_locked",
)
#: The snapshot fields that are no column: what is left, the tags and the product code.
_NOT_COLUMNS = frozenset({"remaining_g", "tag_uid", "tray_uuid", "material_number"})

Outcome = Literal["pulled", "skipped", "failed"]


class PullReport(BaseModel):
    """What one pull did: how many SB spools it made, linked and changed, how many of
    those it made or linked still need a type, with their ids; and the BB spools it could
    not pull (each logged)."""

    created: int = 0
    linked: int = 0
    updated: int = 0
    needs_type: int = 0
    created_ids: list[UUID] = Field(default_factory=list)
    linked_ids: list[UUID] = Field(default_factory=list)
    updated_ids: list[UUID] = Field(default_factory=list)
    needs_type_ids: list[UUID] = Field(default_factory=list)
    failed_bb_ids: list[int] = Field(default_factory=list)


def _text(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _rgba(value: str | None) -> str | None:
    try:
        return normalise_rgba(value) if value else None
    except ValueError:
        return None


def _colours(value: str | None) -> list[str] | None:
    parts = [p for p in (value or "").replace(";", ",").replace(" ", ",").split(",") if p]
    colours = [_rgba(p) for p in parts]
    if not colours or any(c is None for c in colours):
        return None
    return [c for c in colours if c is not None]


def snapshot(bb: BbSpool) -> dict[str, Any]:
    """BB's values in SB's terms, as JSON: what a pull compares with the last one's."""
    label = bb.label_weight if bb.label_weight > 0 else None
    cost = None if bb.cost_per_kg is None else str(round(Decimal(str(bb.cost_per_kg)), 4))
    return {
        "material": _text(bb.material) or "Unknown",
        "subtype": _text(bb.subtype),
        "brand": _text(bb.brand),
        "category": _text(bb.category),
        "effect": _text(bb.effect_type),
        "colour_name": _text(bb.color_name),
        "rgba": _rgba(bb.rgba),
        "extra_colours": _colours(bb.extra_colors),
        "label_weight_g": label,
        "cost_per_kg": cost,
        "nozzle_temp_min": bb.nozzle_temp_min,
        "nozzle_temp_max": bb.nozzle_temp_max,
        "weight_locked": bb.weight_locked,
        "remaining_g": None if label is None else bb.remaining_g,
        "tag_uid": clean_tag_uid(bb.tag_uid),
        "tray_uuid": clean_tray_uuid(bb.tray_uuid),
        "material_number": _text(bb.material_number),
    }


def _column_value(field: str, value: Any) -> Any:
    return Decimal(value) if field == "cost_per_kg" and value is not None else value


def pull(store: Filament, spools: Sequence[BbSpool], *, now: datetime) -> PullReport:
    """Pull ``spools`` (BB's ``GET /inventory/spools``) into ``store``, as the module
    says. ``now`` is the link's time, and its ``bb_updated_at`` when BB sends none. One BB
    spool that fails is rolled back, logged and listed; the rest go on."""
    report = PullReport()
    for bb in spools:
        try:
            with store.spools.transaction() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK, bb.id))
                outcome = _one(conn, store, bb, now)
        except Exception as error:
            logger.warning(
                "could not pull a Bambuddy spool",
                extra={"bb_spool_id": bb.id, "error": type(error).__name__},
            )
            report.failed_bb_ids.append(bb.id)
            continue
        if outcome is None:
            continue
        what, spool_id, typeless = outcome
        getattr(report, f"{what}_ids").append(spool_id)
        if typeless:
            report.needs_type_ids.append(spool_id)
    for what in ("created", "linked", "updated", "needs_type"):
        setattr(report, what, len(getattr(report, f"{what}_ids")))
    return report


def _one(conn: Conn, store: Filament, bb: BbSpool, now: datetime) -> tuple[str, UUID, bool] | None:
    link = conn.execute(
        "SELECT spool_id, bb_updated_at FROM filament_bb_links WHERE bb_spool_id = %s",
        (bb.id,),
    ).fetchone()
    if link is not None:
        spool_id: UUID = link["spool_id"]
        if _update(conn, store, bb, spool_id, link["bb_updated_at"], now):
            return "updated", spool_id, False
        return None
    after = snapshot(bb)
    held = _holder(conn, after, bb.id)
    if held is not None:
        return "linked", held, _link(conn, store, bb, held, after, now)
    made, typeless = _create(conn, store, bb, after, now)
    return "created", made, typeless


# -- step 2: an SB spool that already is this BB spool ---------------------------------


def _holder(conn: Conn, after: dict[str, Any], bb_id: int) -> UUID | None:
    """The unlinked SB spool that holds the BB spool's tag, else the one its record link
    names."""
    uid, tray = after["tag_uid"], after["tray_uuid"]
    if uid is not None or tray is not None:
        row = conn.execute(
            "SELECT b.spool_id FROM filament_tag_bindings b"
            " JOIN filament_tags t ON t.id = b.tag_id"
            " WHERE b.unbound_at IS NULL AND (t.tag_uid = %s OR t.tray_uuid = %s)"
            " AND NOT EXISTS (SELECT 1 FROM filament_bb_links l WHERE l.spool_id = b.spool_id)"
            " ORDER BY b.bound_at LIMIT 1",
            (uid, tray),
        ).fetchone()
        if row is not None:
            found: UUID = row["spool_id"]
            return found
    row = conn.execute(
        "SELECT s.id FROM spool_record_links r JOIN filament_spools s"
        " ON s.id = r.physical_spool_id"
        " WHERE r.provider = 'bambuddy' AND r.record_id = %s"
        " AND NOT EXISTS (SELECT 1 FROM filament_bb_links l WHERE l.spool_id = s.id)",
        (str(bb_id),),
    ).fetchone()
    return None if row is None else row["id"]


def _link(
    conn: Conn,
    store: Filament,
    bb: BbSpool,
    spool_id: UUID,
    after: dict[str, Any],
    now: datetime,
) -> bool:
    """Link the SB spool and keep what it has; a spool with no type takes the one BB's
    values match, and one with no weight BB's. True when it still needs a type."""
    current = conn.execute(
        "SELECT type_id, grams_left, weight_locked FROM filament_spools WHERE id = %s FOR UPDATE",
        (spool_id,),
    ).fetchone()
    assert current is not None
    changes: dict[str, list[Any]] = {}
    type_id: UUID | None = current["type_id"]
    if type_id is None:
        kind = _match(conn, store, after)
        if kind is not None:
            type_id = kind.id
            conn.execute(
                "UPDATE filament_spools SET type_id = %s, updated_at = now() WHERE id = %s",
                (type_id, spool_id),
            )
            changes["type_id"] = [None, type_id]
    reading = after["remaining_g"]
    if current["grams_left"] is not None or current["weight_locked"]:
        reading = None
    _insert_link(conn, spool_id, bb, now)
    _synced(conn, spool_id, bb, after, changes, reading, linked=True)
    FilamentSpoolStore.read(conn, spool_id, check=True)
    return type_id is None


# -- step 3: a new SB spool ------------------------------------------------------------


def _create(
    conn: Conn, store: Filament, bb: BbSpool, after: dict[str, Any], now: datetime
) -> tuple[UUID, bool]:
    kind = _match(conn, store, after)
    values = _values(conn, after, after.keys() - _NOT_COLUMNS)
    if kind is not None:
        typed = kind.model_dump()
        values = {c: v for c, v in values.items() if c not in typed or typed[c] != v}
        values["type_id"] = kind.id
    if not values.get("weight_locked"):
        values.pop("weight_locked", None)
    names = list(values)
    row = conn.execute(
        f"INSERT INTO filament_spools ({', '.join(names)})"
        f" VALUES ({', '.join('%s' for _ in names)}) RETURNING id",
        [values[n] for n in names],
    ).fetchone()
    assert row is not None
    spool_id: UUID = row["id"]
    reading = after["remaining_g"]
    ledger.record(
        conn,
        spool_id,
        "created",
        source="bb" if reading is not None else None,
        absolute_g=reading,
        ref_kind=REF_KIND,
        ref_id=str(bb.id),
        actor=ACTOR,
    )
    _bind(conn, spool_id, after)
    _insert_link(conn, spool_id, bb, now)
    _synced(conn, spool_id, bb, after, {}, None)
    FilamentSpoolStore.read(conn, spool_id, check=True)
    return spool_id, kind is None


def _bind(conn: Conn, spool_id: UUID, after: dict[str, Any]) -> None:
    """Put BB's tag on the spool, unless another spool still holds it (one linked to
    another BB spool, which step 2 did not take): a pull never takes a tag off a spool it
    did not pull it for."""
    uid, tray = after["tag_uid"], after["tray_uuid"]
    if uid is None and tray is None:
        return
    held = conn.execute(
        "SELECT b.spool_id FROM filament_tag_bindings b JOIN filament_tags t ON t.id = b.tag_id"
        " WHERE b.unbound_at IS NULL AND b.spool_id <> %s"
        " AND (t.tag_uid = %s OR t.tray_uuid = %s)",
        (spool_id, uid, tray),
    ).fetchone()
    if held is not None:
        logger.warning(
            "a Bambuddy spool's tag is on another spool; not moved",
            extra={"spool_id": str(spool_id), "holder": str(held["spool_id"])},
        )
        return
    kind: m.TagKind = "bambu_mifare" if tray is not None else "other"
    try:
        with conn.transaction():
            FilamentSpoolStore.bind_on(conn, spool_id, uid, tray, kind, False, ACTOR)
    except TagKeysClashError:
        logger.warning(
            "a Bambuddy spool's tag keys name two tags; not bound",
            extra={"spool_id": str(spool_id)},
        )


# -- step 1: a linked spool takes what BB changed --------------------------------------


def _update(
    conn: Conn,
    store: Filament,
    bb: BbSpool,
    spool_id: UUID,
    pulled_at: datetime | None,
    now: datetime,
) -> bool:
    """True when the SB spool took anything from BB."""
    if bb.updated_at is not None and pulled_at is not None and bb.updated_at <= pulled_at:
        return False
    conn.execute("SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,))
    spool = FilamentSpoolStore.read(conn, spool_id)
    assert spool is not None
    last = conn.execute(
        "SELECT data -> 'bb' AS bb FROM filament_spool_events"
        " WHERE spool_id = %s AND kind = 'synced_from_bb' AND data ? 'bb'"
        " ORDER BY at DESC, id DESC LIMIT 1",
        (spool_id,),
    ).fetchone()
    before: dict[str, Any] = (last["bb"] if last is not None else None) or {}
    after = snapshot(bb)
    conn.execute(
        "UPDATE filament_bb_links SET bb_updated_at = %s WHERE spool_id = %s",
        (bb.updated_at or now, spool_id),
    )
    if after == before:
        return False
    # The newer change wins per field: what SB edited after BB's change stays SB's.
    edited = {
        row["field"]
        for row in conn.execute(
            "SELECT DISTINCT jsonb_object_keys(data -> 'changes') AS field"
            " FROM filament_spool_events WHERE spool_id = %s AND kind = 'edited' AND at > %s"
            " AND jsonb_typeof(data -> 'changes') = 'object'",
            (spool_id, bb.updated_at or now),
        ).fetchall()
    }
    moved = {
        f for f in after.keys() - _NOT_COLUMNS if after[f] != before.get(f) and after[f] is not None
    }
    wanted = {f for f in moved if _CATALOGS.get(f, (f,))[0] not in edited}
    material = spool.effective.material_id
    if "material" in wanted:
        material = _catalog_id(conn, "filament_materials", after["material"], None)
    values = _values(conn, after, wanted, material=material)
    current = spool.effective.model_dump() | {"weight_locked": spool.weight_locked}
    changes = {c: [current.get(c), v] for c, v in values.items() if current.get(c) != v}
    if spool.type_id is None:
        kind = _match(conn, store, after)
        if kind is not None:
            changes["type_id"] = [None, kind.id]
    if changes:
        columns = list(changes)
        conn.execute(
            f"UPDATE filament_spools SET {', '.join(f'{c} = %s' for c in columns)},"
            " updated_at = now() WHERE id = %s",
            [*(changes[c][1] for c in columns), spool_id],
        )
    if (after["tag_uid"], after["tray_uuid"]) != (before.get("tag_uid"), before.get("tray_uuid")):
        _bind(conn, spool_id, after)
    reading = after["remaining_g"]
    locked = bool(changes.get("weight_locked", [None, spool.weight_locked])[1])
    weight: str | None = None
    if reading is None or reading == before.get("remaining_g"):
        reading = None
    elif locked:
        reading, weight = None, "locked"
    _synced(conn, spool_id, bb, after, changes, reading, weight=weight)
    FilamentSpoolStore.read(conn, spool_id, check=True)
    return True


# -- shared ----------------------------------------------------------------------------


def _match(conn: Conn, store: Filament, after: dict[str, Any]) -> m.SpoolType | None:
    """The type BB's values name: by product code, else by identity."""
    names = {"brand": after["brand"], "material": after["material"], "subtype": after["subtype"]}
    if after["material_number"] is not None:
        kind = store.types.match(
            code=after["material_number"], rgba="#000000", label_weight_g=0, conn=conn, **names
        )
        if kind is not None:
            return kind
    if after["rgba"] is None or after["label_weight_g"] is None:
        return None
    return store.types.match(
        rgba=after["rgba"], label_weight_g=after["label_weight_g"], conn=conn, **names
    )


def _values(
    conn: Conn, after: dict[str, Any], fields: Any, *, material: UUID | None = None
) -> dict[str, Any]:
    """The SB columns of the BB ``fields`` that have a value; catalog names resolved, and
    added when missing. A subtype belongs to ``material`` (the new one, else this)."""
    values: dict[str, Any] = {}
    if "material" in fields and after["material"] is not None:
        values["material_id"] = material = _catalog_id(
            conn, "filament_materials", after["material"], None
        )
    for field, (column, table) in _CATALOGS.items():
        if field == "material" or field not in fields or after[field] is None:
            continue
        values[column] = _catalog_id(conn, table, after[field], material)
    for field in _PLAIN:
        if field in fields and after[field] is not None:
            values[field] = _column_value(field, after[field])
    return values


def _catalog_id(conn: Conn, table: str, name: str, material: UUID | None) -> UUID:
    """The unarchived row of ``table`` named ``name`` in any case, else a new one. A
    subtype is ``material``'s own when it has one of the name, else one for every
    material, else a new one of ``material``."""
    subtype = table == "filament_subtypes"
    while True:
        if subtype:
            row = conn.execute(
                "SELECT id FROM filament_subtypes WHERE archived_at IS NULL"
                " AND upper(name) = upper(%s) AND (material_id = %s OR material_id IS NULL)"
                " ORDER BY material_id IS NULL LIMIT 1",
                (name, material),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT id FROM {table} WHERE archived_at IS NULL AND upper(name) = upper(%s)",
                (name,),
            ).fetchone()
        if row is not None:
            found: UUID = row["id"]
            return found
        if subtype:
            made = conn.execute(
                "INSERT INTO filament_subtypes (name, sort_order, material_id)"
                " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                (name, _ADDED_SORT, material),
            ).fetchone()
        else:
            made = conn.execute(
                f"INSERT INTO {table} (name, sort_order) VALUES (%s, %s)"
                " ON CONFLICT DO NOTHING RETURNING id",
                (name, _ADDED_SORT),
            ).fetchone()
        if made is not None:
            new: UUID = made["id"]
            return new


def _insert_link(conn: Conn, spool_id: UUID, bb: BbSpool, now: datetime) -> None:
    conn.execute(
        "INSERT INTO filament_bb_links (spool_id, bb_spool_id, bb_updated_at, linked_at)"
        " VALUES (%s, %s, %s, %s)",
        (spool_id, bb.id, bb.updated_at or now, now),
    )


def _synced(
    conn: Conn,
    spool_id: UUID,
    bb: BbSpool,
    after: dict[str, Any],
    changes: dict[str, list[Any]],
    reading: float | None,
    *,
    linked: bool = False,
    weight: str | None = None,
) -> None:
    """The ``synced_from_bb`` event: what BB said (the next pull's ``before``), what
    changed, and a ``bb`` reading when ``reading`` is set."""
    data: dict[str, Any] = {"bb_spool_id": bb.id, "bb": after, "changes": changes}
    if linked:
        data["linked"] = True
    if weight is not None:
        data["weight"] = {"skipped": weight, "bb_remaining_g": after["remaining_g"]}
    stamp = bb.updated_at.isoformat() if bb.updated_at is not None else None
    ledger.record(
        conn,
        spool_id,
        "synced_from_bb",
        source="bb" if reading is not None else None,
        absolute_g=reading,
        ref_kind=REF_KIND if stamp else None,
        ref_id=f"{bb.id}@{stamp}" if stamp else None,
        actor=ACTOR,
        data=data,
    )


# -- reading Bambuddy ------------------------------------------------------------------


async def read_and_pull(store: Filament, settings: StoredSettings) -> PullReport:
    """Read BB's unarchived spools and pull them. BB not configured is `client_for`'s
    409; BB's own errors are its client's."""
    async with client_for(settings) as client:
        spools = await client.spools()
    return await asyncio.to_thread(pull, store, spools, now=datetime.now(UTC))


async def scheduled_pull(store: Filament, load: Callable[[], StoredSettings]) -> Outcome:
    """The Schedule's pull (`SpoolRefresh`): ``skipped`` with no BB URL, ``failed`` when
    BB or the store fails (logged by its type, never raised), else ``pulled``."""
    settings = await asyncio.to_thread(load)
    if not settings.bambuddy_url:
        logger.info("no Bambuddy configured; its spools are not pulled")
        return "skipped"
    try:
        report = await read_and_pull(store, settings)
    except Exception as error:
        logger.warning("could not pull Bambuddy's spools", extra={"error": type(error).__name__})
        return "failed"
    if report.failed_bb_ids:
        logger.warning(
            "some Bambuddy spools were not pulled", extra={"bb_spool_ids": report.failed_bb_ids}
        )
    return "pulled"
