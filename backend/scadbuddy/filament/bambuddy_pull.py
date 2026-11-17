"""Bambuddy's spools pulled into ScadBuddy's own (#6261, spec §8.1, §8.2). Read only:
nothing here writes to Bambuddy (SB to BB is #6263).

`pull` takes each BB spool in its own transaction, under an advisory lock on its id, in
this order:

1. A `filament_bb_links` row names it: the SB spool takes what BB changed since the last
   pull. "Changed" compares BB's values with the ones the last pull saw, kept in the
   ``data.bb`` of the spool's newest ``synced_from_bb`` event; a BB row whose
   ``updated_at`` is not past the link's ``bb_updated_at`` is not looked at, except for
   the insert type (below): a row whose last reading has no ``core_weight_catalog_id``
   is read in full on each pull until the key is recorded (the event is booked with no
   ref), which happens on the first pull where BB's id is mapped or None, or where the
   row changed; while the id stays unmapped the key stays absent and nothing is booked.
   A row whose catalog entry was mapped after it was read is checked for the insert
   type. Per field
   the newer change wins (§8.2): a field SB edited (an ``edited`` event) after BB's
   ``updated_at`` keeps SB's value. A value BB no longer has never clears SB's, and the
   fields BB does not have (note, location, bed temperatures ...) are never touched. A
   change of what is left (``label_weight - weight_used``) is a ``bb`` absolute reading
   on the ``synced_from_bb`` event, unless the SB spool's weight is locked: that value
   is BB's own estimate, and the lock guards against estimates (§5.5), so it is recorded
   in the event's data and never moves ``grams_left``. A BB weighing is a physical
   reading and counts under the lock (`_book`). A BB unlock clears the lock only when a
   pull set it (`_lock_from_bb`).
2. Else an SB spool already is that spool: it holds the BB spool's ``tag_uid`` or
   ``tray_uuid`` through an open binding. It is linked; a spool that needs a
   type takes the one BB's values match, and one with no weight takes BB's.
3. Else a new SB spool: its type by ``material_number`` (`SpoolTypeStore.match`), else
   by identity (brand, material, subtype, colour, label weight), else none (it needs a
   type) and BB's values as its own. A spool with a type overrides only what BB says
   differently. It takes BB's tags unless another linked spool holds them, a
   ``created`` event with a ``bb`` reading of what is left, and its link.

Catalog names from BB (material, subtype, brand, category, effect) resolve by their upper
case name, and are added when missing. BB's spool catalog (``GET /inventory/catalog``,
`BambuddyClient.spool_catalog`) is mapped once per pull (`_insert_types`): an entry is the
insert type that has its id as ``bb_catalog_id``, else the unlinked one of the same upper
case name (now linked), else a new one (its weight, no insert material). A type that
exists is never rewritten. A BB spool's ``core_weight_catalog_id`` (an unarchived mapped
entry) sets the SB spool's insert type: when the spool is made, its own insert type when
it has no spool type, else an override only when it differs from the spool type's; when
it is linked, or read for the first time (the last reading has no such key), where the
spool has none of its own, unless a person cleared it (an ``edited`` event naming
``insert_type_id`` since the link); and when BB's value changed since the last reading (a
recorded ``None`` is a value), as any moved field: BB's wins unless an ``edited`` event
since BB's change names ``insert_type_id``, and moving to the spool type's own insert type
clears the override. While the entry is not mapped the reading keeps the last id, or, with
no last id, leaves the key absent, so the pull that maps it applies it. A
catalog read that fails leaves the insert types alone and the spools are pulled
(`PullReport.catalog_failed`).

A pulled weight is an absolute reading at the BB row's ``updated_at``, not at the pull:
BB's state is as of its last write (the ``created`` and every ``synced_from_bb`` event
take that time; with none sent, the pull's). SB books a native print's ``print_started``
and ``print_ended`` at its plate's start (`filament/print_uses.py`), so a plate that started before
BB's write is under BB's anchor and is not taken off again, and one that started after is
(§8.2, "Consumption is counted once"; the rule is `ledger.py`'s). That holds when BB
deducts a plate as it ends, which it does per plate. It does not hold when BB's row moved
during a plate for another reason (an edit in BB): that plate is then not taken off, until
BB's next write, at its end, anchors again. BB's clock and SB's are compared as they are.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from pydantic import BaseModel, Field, ValidationError

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.bambuddy.models import SpoolAssignment, SpoolCatalogEntry
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import ledger
from scadbuddy.filament import models as m
from scadbuddy.filament.colour import normalise_rgba
from scadbuddy.filament.component import Filament
from scadbuddy.filament.presets import drop_migrated, own_presets
from scadbuddy.filament.spools import FilamentSpoolStore, TagKeysClashError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.operations.kinds import waiting_on_bambuddy
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
_NOT_COLUMNS = frozenset(
    {"remaining_g", "tag_uid", "tray_uuid", "material_number", "core_weight_catalog_id"}
)
#: The catalog entries already warned about as skipped, so a clash is told once a process.
_WARNED_SKIPS: set[tuple[int, str]] = set()
_WARNED_SKIPS_MAX = 1000

Outcome = Literal["pulled", "skipped", "failed"]


class PullReport(BaseModel):
    """What one pull did: how many SB spools it made, linked and changed, how many of
    those it made or linked still need a type, and how many it left alone because they
    are archived, with their ids; the BB spools it could not pull (each logged and rolled
    back); and the BB rows it could not read (each logged), with their ids when the row
    had one; whether BB's spool catalog failed (all or nothing: then no entry was mapped),
    and how many of its entries were skipped (no name or weight, or a name another type
    has), so a spool naming one gets no insert type."""

    created: int = 0
    linked: int = 0
    updated: int = 0
    needs_type: int = 0
    archived: int = 0
    invalid: int = 0
    #: BB's spool catalog could not be read or mapped; the insert types were left alone.
    catalog_failed: bool = False
    catalog_skipped: int = 0
    created_ids: list[UUID] = Field(default_factory=list)
    linked_ids: list[UUID] = Field(default_factory=list)
    updated_ids: list[UUID] = Field(default_factory=list)
    needs_type_ids: list[UUID] = Field(default_factory=list)
    archived_ids: list[UUID] = Field(default_factory=list)
    failed_bb_ids: list[int] = Field(default_factory=list)
    invalid_bb_ids: list[int] = Field(default_factory=list)


def _text(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _rgba(value: str | None) -> str | None:
    try:
        return normalise_rgba(value) if value else None
    except ValueError:
        return None


def _colours(value: str | None) -> list[str] | None:
    """BB's ``extra_colors``: comma-separated hex, 6 or 8 digits each, lower case with no
    ``#`` as BB stores it (``normalize_extra_colors``, ``backend/app/schemas/spool.py``
    lines 42-66 at https://github.com/maziggy/bambuddy/tree/1aa7bce166682fde929fec0d535bdaaa22685aad).
    Spaces and ``;`` are taken as commas too. One bad token drops the field."""
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
        "core_weight_catalog_id": bb.core_weight_catalog_id,
    }


def _column_value(field: str, value: Any) -> Any:
    return Decimal(value) if field == "cost_per_kg" and value is not None else value


def pull(
    store: Filament,
    spools: Sequence[BbSpool],
    *,
    now: datetime,
    actor: str = ACTOR,
    catalog: Sequence[SpoolCatalogEntry] = (),
) -> PullReport:
    """Pull ``spools`` (BB's ``GET /inventory/spools``) into ``store``, as the module
    says, after ``catalog`` (BB's spool catalog) is mapped to insert types. ``now`` is the
    link's time, and its ``bb_updated_at`` when BB sends none; ``actor`` is who the events
    name (a person's import, else ``bambuddy``). One BB spool that fails is rolled back,
    logged and listed; the rest go on, as when the catalog fails."""
    report = PullReport()
    try:
        report.catalog_skipped = _insert_types(store, catalog)
    except Exception:
        logger.warning("could not map Bambuddy's spool catalog", exc_info=True)
        report.catalog_failed = True
    for bb in spools:
        try:
            with store.spools.transaction() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK, bb.id))
                outcome = _one(conn, store, bb, now, actor)
        except Exception as error:
            logger.warning(
                "could not pull a Bambuddy spool",
                extra={"bb_spool_id": bb.id, "error": type(error).__name__},
                exc_info=True,
            )
            report.failed_bb_ids.append(bb.id)
            continue
        if outcome is None:
            continue
        what, spool_id, typeless = outcome
        getattr(report, f"{what}_ids").append(spool_id)
        if typeless:
            report.needs_type_ids.append(spool_id)
    for what in ("created", "linked", "updated", "needs_type", "archived"):
        setattr(report, what, len(getattr(report, f"{what}_ids")))
    return report


def _insert_types(store: Filament, catalog: Sequence[SpoolCatalogEntry]) -> int:
    """Make each entry an insert type, in one transaction under its own advisory lock
    (``0`` is no BB spool id): the type with its ``bb_catalog_id``, else the unlinked one
    of the same upper case name, which takes the id, else a new one. A type that exists
    keeps its name and weight; a fractional weight is rounded to whole grams. An entry with
    no name or no weight, or a name another type has, is skipped: how many is the answer
    (warned once a process, then at debug)."""
    if not catalog:
        return 0
    skipped = 0

    def skip(entry: SpoolCatalogEntry, why: str) -> None:
        nonlocal skipped
        skipped += 1
        key = (entry.id, why)
        if len(_WARNED_SKIPS) > _WARNED_SKIPS_MAX:
            _WARNED_SKIPS.clear()
        log = logger.debug if key in _WARNED_SKIPS else logger.warning
        _WARNED_SKIPS.add(key)
        log(
            "a Bambuddy spool catalog entry is skipped (%s); spools that name it get no "
            "insert type",
            why,
            extra={"bb_catalog_id": entry.id},
        )

    with store.spools.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s, 0)", (_LOCK,))
        for entry in catalog:
            name = _text(entry.name)
            grams = math.floor(entry.weight + 0.5)
            if name is None or grams <= 0:
                skip(entry, "no name or weight")
                continue
            if conn.execute(
                "SELECT 1 FROM filament_insert_types WHERE bb_catalog_id = %s", (entry.id,)
            ).fetchone():
                continue
            linked = conn.execute(
                "UPDATE filament_insert_types SET bb_catalog_id = %s, updated_at = now()"
                " WHERE upper(name) = upper(%s) AND archived_at IS NULL"
                " AND bb_catalog_id IS NULL",
                (entry.id, name),
            )
            if linked.rowcount:
                continue
            if conn.execute(
                "SELECT 1 FROM filament_insert_types WHERE upper(name) = upper(%s)"
                " AND archived_at IS NULL",
                (name,),
            ).fetchone():
                skip(entry, "another type has its name")
                continue
            conn.execute(
                "INSERT INTO filament_insert_types"
                " (name, sort_order, empty_weight_g, bb_catalog_id)"
                " VALUES (%s, %s, %s, %s)",
                (name, _ADDED_SORT, grams, entry.id),
            )
    return skipped


def _core_insert_type(conn: Conn, bb: BbSpool) -> UUID | None:
    """The unarchived insert type BB's ``core_weight_catalog_id`` is, once the catalog is
    mapped."""
    if bb.core_weight_catalog_id is None:
        return None
    row = conn.execute(
        "SELECT id FROM filament_insert_types WHERE bb_catalog_id = %s AND archived_at IS NULL",
        (bb.core_weight_catalog_id,),
    ).fetchone()
    return None if row is None else row["id"]


def _one(
    conn: Conn, store: Filament, bb: BbSpool, now: datetime, actor: str
) -> tuple[str, UUID, bool] | None:
    """What came of one BB spool. An archived SB spool, linked or named by the BB
    record's link, is left as it is: never restored, never written to (``archived``)."""
    link = conn.execute(
        "SELECT l.spool_id, l.bb_updated_at, s.archived_at IS NOT NULL AS archived"
        " FROM filament_bb_links l JOIN filament_spools s ON s.id = l.spool_id"
        " WHERE l.bb_spool_id = %s",
        (bb.id,),
    ).fetchone()
    if link is not None:
        spool_id: UUID = link["spool_id"]
        if link["archived"]:
            return "archived", spool_id, False
        changed = _update(conn, store, bb, spool_id, link["bb_updated_at"], now, actor)
        # BB may not move ``updated_at`` for a weighing: its events are booked anyway,
        # once each.
        booked = _book(conn, spool_id, bb, actor)
        presets = _bb_presets(conn, spool_id, bb, actor)
        return ("updated", spool_id, False) if changed or booked or presets else None
    after = snapshot(bb)
    held = _holder(conn, after, bb.id)
    if held is not None:
        held_id, archived = held
        if archived:
            return "archived", held_id, False
        typeless = _link(conn, store, bb, held_id, after, now, actor)
        _book(conn, held_id, bb, actor)
        _bb_presets(conn, held_id, bb, actor)
        return "linked", held_id, typeless
    made, typeless = _create(conn, store, bb, after, now, actor)
    _book(conn, made, bb, actor)
    _bb_presets(conn, made, bb, actor)
    return "created", made, typeless


#: The H2C nozzle sizes a Bambuddy spool's one preset is written for (ruling R1).
BB_PRESET_NOZZLES = ("0.2", "0.4", "0.6", "0.8")


def _bb_presets(conn: Conn, spool_id: UUID, bb: BbSpool, actor: str) -> bool:
    """A linked BB spool's ``slicer_filament`` (and its name) as the SB spool's own H2C
    preset at every nozzle size, a ``migrated`` row (#6262 Task 7 ruling R1), so a plan
    on Bambuddy's path finds the preset Bambuddy's spool named. True when a row changed.

    The value Bambuddy sent last is kept on the link (``bb_slicer_filament`` and its
    name). Nothing is written unless Bambuddy's value changed since then, so a preset a
    person deleted stays deleted; a link with no last value (from before it was kept)
    fills its empty keys once. On a change, a key is written only where the spool has no
    row for it, or where its row still holds that last value; any other row (a
    person's, or a ``migrated`` one the native API or the native migration wrote) is
    never touched. A clear in Bambuddy deletes only the rows that still hold its last
    value. Rows that change record one ``synced_from_bb`` event with the spool's own
    rows before and after (``changes.presets``). A spool with a type takes none: its
    type gives its presets (F27, `drop_migrated`).

    The spool's row is locked first (after the pull's advisory lock and any tags, the
    pull's order), as `FilamentSpoolStore.set_presets` locks it, so a person's edit in
    flight is waited for and then seen (re-review 2 I-A)."""
    # Here, not at the top: `filament.presets` reads `bambuddy.resolver`.
    from scadbuddy.bambuddy.resolver import PRINTER_MODEL

    conn.execute("SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,))
    sent = (
        _text(bb.slicer_filament),
        _text(bb.slicer_filament_name) if bb.slicer_filament else None,
    )
    link = conn.execute(
        "SELECT l.bb_slicer_filament, l.bb_slicer_filament_name, s.type_id IS NOT NULL AS typed"
        " FROM filament_bb_links l JOIN filament_spools s ON s.id = l.spool_id"
        " WHERE l.spool_id = %s",
        (spool_id,),
    ).fetchone()
    if link is None:
        return False
    last = (link["bb_slicer_filament"], link["bb_slicer_filament_name"])
    conn.execute(
        "UPDATE filament_bb_links SET bb_slicer_filament = %s, bb_slicer_filament_name = %s"
        " WHERE spool_id = %s AND (bb_slicer_filament, bb_slicer_filament_name)"
        " IS DISTINCT FROM (%s, %s)",
        (*sent, spool_id, *sent),
    )
    if link["typed"] or sent == last:
        return False
    found = conn.execute(
        "SELECT nozzle_diameter, slicer_filament, slicer_filament_name, migrated"
        " FROM filament_spool_presets WHERE spool_id = %s AND printer_model = %s",
        (spool_id, PRINTER_MODEL),
    ).fetchall()
    rows = {
        row["nozzle_diameter"]: (row["slicer_filament"], row["slicer_filament_name"])
        for row in found
    }
    migrated = {row["nozzle_diameter"] for row in found if row["migrated"]}
    # Bambuddy's own rows: none, or migrated ones still holding what it sent last. A
    # person's row (not migrated) is never Bambuddy's, even when its value matches.
    ours = [
        size
        for size in BB_PRESET_NOZZLES
        if size not in rows or (last[0] is not None and size in migrated and rows[size] == last)
    ]
    wanted = [size for size in ours if rows.get(size) != (sent if sent[0] else None)]
    if not wanted:
        return False
    before = own_presets(conn, spool_id)
    for size in wanted:
        if sent[0] is None:
            conn.execute(
                "DELETE FROM filament_spool_presets WHERE spool_id = %s AND printer_model = %s"
                " AND nozzle_diameter = %s",
                (spool_id, PRINTER_MODEL, size),
            )
        else:
            conn.execute(
                "INSERT INTO filament_spool_presets (spool_id, printer_model, nozzle_diameter,"
                " slicer_filament, slicer_filament_name, migrated)"
                " VALUES (%s, %s, %s, %s, %s, true)"
                " ON CONFLICT (spool_id, printer_model, nozzle_diameter) DO UPDATE SET"
                " slicer_filament = EXCLUDED.slicer_filament,"
                " slicer_filament_name = EXCLUDED.slicer_filament_name",
                (spool_id, PRINTER_MODEL, size, *sent),
            )
    ledger.record(
        conn,
        spool_id,
        "synced_from_bb",
        actor=actor,
        data={
            "bb_spool_id": bb.id,
            "changes": {"presets": [before, own_presets(conn, spool_id)]},
        },
    )
    return True


# -- step 2: an SB spool that already is this BB spool ---------------------------------


def _tag_holders(conn: Conn, uid: str | None, tray: str | None) -> list[dict[str, Any]]:
    """The open bindings of the tags BB's keys name, oldest first, each with its spool,
    whether that spool is archived and whether it is linked to a BB spool. Two rows of
    two spools are the keys naming two tags."""
    if uid is None and tray is None:
        return []
    return conn.execute(
        "SELECT b.spool_id, s.archived_at IS NOT NULL AS archived,"
        " EXISTS (SELECT 1 FROM filament_bb_links l WHERE l.spool_id = b.spool_id) AS linked"
        " FROM filament_tag_bindings b JOIN filament_tags t ON t.id = b.tag_id"
        " JOIN filament_spools s ON s.id = b.spool_id"
        " WHERE b.unbound_at IS NULL AND (t.tag_uid = %s OR t.tray_uuid = %s)"
        " ORDER BY b.bound_at, b.id",
        (uid, tray),
    ).fetchall()


def _holder(conn: Conn, after: dict[str, Any], bb_id: int) -> tuple[UUID, bool] | None:
    """The SB spool that already is this BB spool, and whether it is archived: the
    unlinked, unarchived spool with the oldest binding of the BB spool's tag (an archived
    one's tag is free to re-use: a new spool takes it). Keys that name two spools are
    logged, and the oldest binding is kept."""
    live = [
        h for h in _tag_holders(conn, after["tag_uid"], after["tray_uuid"]) if not h["archived"]
    ]
    if len({h["spool_id"] for h in live}) > 1:
        logger.warning(
            "a Bambuddy spool's tag keys are bound to two spools; the oldest binding is kept",
            extra={"bb_spool_id": bb_id, "spools": [str(h["spool_id"]) for h in live]},
        )
    free = [h for h in live if not h["linked"]]
    if free:
        found: UUID = free[0]["spool_id"]
        return found, False
    return None


def _link(
    conn: Conn,
    store: Filament,
    bb: BbSpool,
    spool_id: UUID,
    after: dict[str, Any],
    now: datetime,
    actor: str,
) -> bool:
    """Link the SB spool and keep what it has; a spool with no type takes the one BB's
    values match (dropping its ``migrated`` presets, `presets.drop_migrated`), and one
    with no weight BB's. True when it still needs a type."""
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
            dropped = drop_migrated(conn, spool_id)
            if dropped is not None:
                changes["presets"] = dropped
    insert = _backfill_insert_type(conn, spool_id, bb)
    if insert is not None:
        changes["insert_type_id"] = insert
    reading = after["remaining_g"]
    if current["grams_left"] is not None or current["weight_locked"]:
        reading = None
    _insert_link(conn, spool_id, bb, now)
    _synced(conn, spool_id, bb, after, changes, reading, actor, linked=True)
    FilamentSpoolStore.read(conn, spool_id, check=True)
    return type_id is None


def _insert_state(conn: Conn, spool_id: UUID) -> tuple[UUID | None, UUID | None]:
    """The spool's own insert type and its spool type's."""
    row = conn.execute(
        "SELECT s.insert_type_id AS own, t.insert_type_id AS typed FROM filament_spools s"
        " LEFT JOIN filament_spool_types t ON t.id = s.type_id WHERE s.id = %s",
        (spool_id,),
    ).fetchone()
    assert row is not None
    return row["own"], row["typed"]


def _insert_edited_since_link(conn: Conn, spool_id: UUID) -> bool:
    """A person edited the spool's insert type (set or cleared it) after the link."""
    return (
        conn.execute(
            "SELECT 1 FROM filament_spool_events e JOIN filament_bb_links l"
            " ON l.spool_id = e.spool_id WHERE e.spool_id = %s AND e.kind = 'edited'"
            " AND e.at > l.linked_at AND jsonb_typeof(e.data -> 'changes') = 'object'"
            " AND e.data -> 'changes' ? 'insert_type_id' LIMIT 1",
            (spool_id,),
        ).fetchone()
        is not None
    )


def _backfill_insert_type(conn: Conn, spool_id: UUID, bb: BbSpool) -> list[Any] | None:
    """Set the insert type BB's ``core_weight_catalog_id`` names where the SB spool has
    none of its own, it differs from the spool type's and no person cleared it since the
    link: the change, or None. Never replaces a choice already made."""
    insert_id = _core_insert_type(conn, bb)
    if insert_id is None:
        return None
    # The row first, as a person's edit of it is: this read-then-write must not lose one.
    conn.execute("SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,))
    own, typed = _insert_state(conn, spool_id)
    if own is not None or typed == insert_id or _insert_edited_since_link(conn, spool_id):
        return None
    conn.execute(
        "UPDATE filament_spools SET insert_type_id = %s, updated_at = now() WHERE id = %s",
        (insert_id, spool_id),
    )
    return [None, insert_id]


# -- step 3: a new SB spool ------------------------------------------------------------


def _create(
    conn: Conn, store: Filament, bb: BbSpool, after: dict[str, Any], now: datetime, actor: str
) -> tuple[UUID, bool]:
    kind = _match(conn, store, after)
    values = _values(conn, after, after.keys() - _NOT_COLUMNS)
    if kind is not None:
        typed = kind.model_dump()
        values = {c: v for c, v in values.items() if c not in typed or typed[c] != v}
        values["type_id"] = kind.id
    insert_id = _core_insert_type(conn, bb)
    if insert_id is not None and (kind is None or kind.insert_type_id != insert_id):
        values["insert_type_id"] = insert_id
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
        actor=actor,
        at=bb.updated_at,
    )
    _bind(conn, spool_id, after, actor)
    _insert_link(conn, spool_id, bb, now)
    # A lock BB set is BB's to clear (`_lock_from_bb`).
    locked = {"weight_locked": [False, True]} if values.get("weight_locked") else {}
    _synced(conn, spool_id, bb, after, locked, None, actor)
    FilamentSpoolStore.read(conn, spool_id, check=True)
    return spool_id, kind is None


def _bind(conn: Conn, spool_id: UUID, after: dict[str, Any], actor: str) -> None:
    """Put BB's tag on the spool, unless another unarchived spool still holds it (one
    linked to another BB spool, which step 2 did not take): a pull never takes a tag off
    a live spool it did not pull it for. An archived spool's tag is taken (re-use)."""
    uid, tray = after["tag_uid"], after["tray_uuid"]
    if uid is None and tray is None:
        return
    held = [
        h for h in _tag_holders(conn, uid, tray) if h["spool_id"] != spool_id and not h["archived"]
    ]
    if held:
        logger.warning(
            "a Bambuddy spool's tag is on another spool; not moved",
            extra={"spool_id": str(spool_id), "holder": str(held[0]["spool_id"])},
        )
        return
    kind: m.TagKind = "bambu_mifare" if tray is not None else "other"
    try:
        with conn.transaction():
            FilamentSpoolStore.bind_on(conn, spool_id, uid, tray, kind, False, actor)
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
    actor: str,
) -> bool:
    """True when the SB spool took anything from BB."""
    unmoved = bb.updated_at is not None and pulled_at is not None and bb.updated_at <= pulled_at
    last = conn.execute(
        "SELECT data -> 'bb' AS bb FROM filament_spool_events"
        " WHERE spool_id = %s AND kind = 'synced_from_bb' AND data ? 'bb'"
        " ORDER BY at DESC, seq DESC LIMIT 1",
        (spool_id,),
    ).fetchone()
    before: dict[str, Any] = (last["bb"] if last is not None else None) or {}
    insert_id = _core_insert_type(conn, bb)
    # A row BB did not move is read in full only when its last reading has no catalog id
    # (a pull before it was kept) or BB's id is mapped now and differs from the last one.
    # Else it may still take the insert type of an entry mapped after it was read.
    if (
        unmoved
        and "core_weight_catalog_id" in before
        and (insert_id is None or bb.core_weight_catalog_id == before["core_weight_catalog_id"])
    ):
        insert = _backfill_insert_type(conn, spool_id, bb)
        if insert is None:
            return False
        ledger.record(
            conn,
            spool_id,
            "synced_from_bb",
            actor=actor,
            at=bb.updated_at,
            data={"bb_spool_id": bb.id, "bb": before, "changes": {"insert_type_id": insert}},
        )
        return True
    after = snapshot(bb)
    if insert_id is None and bb.core_weight_catalog_id is not None:
        # Its entry is not mapped (the read failed, or it was skipped or archived): the
        # reading keeps the last id, so the pull that maps it sees the change. With no
        # last id the key stays absent, so the pull that maps it reads it for the first time.
        if "core_weight_catalog_id" in before:
            after["core_weight_catalog_id"] = before["core_weight_catalog_id"]
        else:
            del after["core_weight_catalog_id"]
    keys = (after["tag_uid"], after["tray_uuid"])
    retag = keys != (before.get("tag_uid"), before.get("tray_uuid")) and keys != (None, None)
    if retag:
        # The tags before the spool, as every writer of bindings takes them (`bind_tag`).
        conn.execute(
            "SELECT id FROM filament_tags WHERE tag_uid = %s OR tray_uuid = %s"
            " ORDER BY id FOR UPDATE",
            keys,
        )
    conn.execute("SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,))
    spool = FilamentSpoolStore.read(conn, spool_id)
    assert spool is not None
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
    if changes.get("weight_locked") == [True, False] and not _lock_from_bb(conn, spool_id):
        # A BB unlock never clears a lock SB set.
        del changes["weight_locked"]
    if spool.type_id is None:
        kind = _match(conn, store, after)
        if kind is not None:
            changes["type_id"] = [None, kind.id]
    if insert_id is not None and after["core_weight_catalog_id"] != before.get(
        "core_weight_catalog_id"
    ):
        own, typed = _insert_state(conn, spool_id)
        # A key the last reading does not have is read for the first time: only where the
        # spool has none of its own, and a person has not cleared it since the link. A key
        # it has (even None) moved: BB's value wins unless an ``edited`` event since BB's
        # change names the field.
        if "core_weight_catalog_id" not in before:
            free = own is None and not _insert_edited_since_link(conn, spool_id)
        else:
            free = "insert_type_id" not in edited
        # BB's own spool type's insert type is no override.
        target = None if typed == insert_id else insert_id
        if free and target != own:
            changes["insert_type_id"] = [own, target]
    if changes:
        columns = list(changes)
        conn.execute(
            f"UPDATE filament_spools SET {', '.join(f'{c} = %s' for c in columns)},"
            " updated_at = now() WHERE id = %s",
            [*(changes[c][1] for c in columns), spool_id],
        )
    if retag:
        _bind(conn, spool_id, after, actor)
    reading = after["remaining_g"]
    locked = bool(changes.get("weight_locked", [None, spool.weight_locked])[1])
    weight: str | None = None
    if reading is None or reading == before.get("remaining_g"):
        reading = None
    elif locked:
        reading, weight = None, "locked"
    # A row BB did not move is read again: its event takes no ref, which the same stamp
    # would otherwise share with the one it replaces (`ledger.record` drops a repeat).
    _synced(conn, spool_id, bb, after, changes, reading, actor, weight=weight, ref=not unmoved)
    FilamentSpoolStore.read(conn, spool_id, check=True)
    return True


def _lock_from_bb(conn: Conn, spool_id: UUID) -> bool:
    """True when the spool's lock was last set by a pull: the newest event that changed
    ``weight_locked`` is a ``synced_from_bb`` one. A lock with no such event (set at a
    spool's creation by SB, or never recorded) is SB's."""
    row = conn.execute(
        "SELECT kind FROM filament_spool_events WHERE spool_id = %s"
        " AND kind IN ('edited', 'synced_from_bb')"
        " AND jsonb_typeof(data -> 'changes') = 'object' AND data -> 'changes' ? 'weight_locked'"
        " ORDER BY at DESC, seq DESC LIMIT 1",
        (spool_id,),
    ).fetchone()
    return row is not None and row["kind"] == "synced_from_bb"


#: Bambuddy's ``last_scale_weight`` is the gross weight on the scale, spool core included
#: ("Last gross weight from scale (g)", ``backend/app/models/spool.py`` line 43), and its
#: scale route takes the spool's ``core_weight`` off it for the filament left
#: (``backend/app/api/routes/spoolbuddy.py`` lines 1249-1251), both at
#: https://github.com/maziggy/bambuddy/tree/1aa7bce166682fde929fec0d535bdaaa22685aad.


def _core_g(conn: Conn, spool_id: UUID, bb: BbSpool) -> tuple[int | None, str | None]:
    """The empty spool's weight and where it came from: the SB spool's effective insert
    type, else BB's own ``core_weight``; ``(None, None)`` when neither has one."""
    row = conn.execute(
        "SELECT i.empty_weight_g FROM filament_spools s"
        " LEFT JOIN filament_spool_types t ON t.id = s.type_id"
        " LEFT JOIN filament_insert_types i ON i.id = coalesce(s.insert_type_id, t.insert_type_id)"
        " WHERE s.id = %s",
        (spool_id,),
    ).fetchone()
    if row is not None and row["empty_weight_g"] is not None:
        return int(row["empty_weight_g"]), "insert_type"
    if bb.core_weight > 0:
        return bb.core_weight, "bb"
    return None, None


def _book(conn: Conn, spool_id: UUID, bb: BbSpool, actor: str) -> bool:
    """BB's last weighing as a ``weighed`` event and its last drying as a ``dried`` one,
    each once: ref ``bb_scale``/``bb_dried`` and ``<bb id>/<time>``, at BB's time, source
    ``bb``. The weighing's reading is the scale's gross weight less the core
    (`_core_g`), the gross kept in ``data``; with no core weight known it books no
    reading. A weighing is a physical reading, so it counts under the weight lock, which
    guards against estimates (§5.5). The ledger's rule picks the anchor: the weighing wins
    when it is the newest reading. True when either is new."""
    booked = False
    if bb.last_weighed_at is not None and bb.last_scale_weight is not None:
        gross = float(bb.last_scale_weight)
        core, came_from = _core_g(conn, spool_id, bb)
        data: dict[str, Any] = {"bb_gross_g": gross}
        net: float | None = None
        if core is None:
            data["skipped"] = "no_core_weight"
        else:
            net = max(gross - core, 0.0)
            data |= {"core_g": core, "core_from": came_from}
        booked |= (
            ledger.record(
                conn,
                spool_id,
                "weighed",
                source="bb",
                absolute_g=net,
                at=bb.last_weighed_at,
                ref_kind="bb_scale",
                ref_id=f"{bb.id}/{bb.last_weighed_at.isoformat()}",
                actor=actor,
                data=data,
            )
            is not None
        )
    if bb.last_dried_at is not None:
        booked |= (
            ledger.record(
                conn,
                spool_id,
                "dried",
                source="bb",
                at=bb.last_dried_at,
                ref_kind="bb_dried",
                ref_id=f"{bb.id}/{bb.last_dried_at.isoformat()}",
                actor=actor,
            )
            is not None
        )
    return booked


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
    actor: str,
    *,
    linked: bool = False,
    weight: str | None = None,
    ref: bool = True,
) -> None:
    """The ``synced_from_bb`` event, at BB's ``updated_at``: what BB said (the next
    pull's ``before``), what changed, and a ``bb`` reading when ``reading`` is set."""
    data: dict[str, Any] = {"bb_spool_id": bb.id, "bb": after, "changes": changes}
    if linked:
        data["linked"] = True
    if weight is not None:
        data["weight"] = {"skipped": weight, "bb_remaining_g": after["remaining_g"]}
    stamp = bb.updated_at.isoformat() if bb.updated_at is not None and ref else None
    ledger.record(
        conn,
        spool_id,
        "synced_from_bb",
        source="bb" if reading is not None else None,
        absolute_g=reading,
        ref_kind=REF_KIND if stamp else None,
        ref_id=f"{bb.id}@{stamp}" if stamp else None,
        actor=actor,
        # BB's state as of its last write, so the reading sits where it happened.
        at=bb.updated_at,
        data=data,
    )


# -- reading Bambuddy ------------------------------------------------------------------


def parse_rows(rows: Sequence[Any]) -> tuple[list[BbSpool], list[int | None]]:
    """BB's spool rows, each validated on its own: the good ones, and the id (``None``
    when it has no readable one) of each row that is not a spool, logged."""
    good: list[BbSpool] = []
    bad: list[int | None] = []
    for row in rows:
        try:
            good.append(BbSpool.model_validate(row))
        except ValidationError:
            row_id = row.get("id") if isinstance(row, dict) else None
            bad.append(row_id if isinstance(row_id, int) else None)
            logger.warning(
                "a Bambuddy spool row is not a spool; skipped",
                extra={"bb_spool_id": bad[-1]},
                exc_info=True,
            )
    return good, bad


async def read_and_pull(
    store: Filament, settings: StoredSettings, *, actor: str = ACTOR
) -> PullReport:
    """Read BB's unarchived spools and pull them; a malformed row is skipped and counted
    as ``invalid``. BB not configured is `client_for`'s 409; BB's own errors are its
    client's."""
    async with client_for(settings) as client:
        rows = await client.spool_rows()
    spools, bad = parse_rows(rows)
    catalog = await read_catalog(settings)
    report = await asyncio.to_thread(
        lambda: pull(store, spools, now=datetime.now(UTC), actor=actor, catalog=catalog or ())
    )
    report.catalog_failed = report.catalog_failed or catalog is None
    report.invalid = len(bad)
    report.invalid_bb_ids = [i for i in bad if i is not None]
    return report


#: How long :func:`read_assignments` waits for Bambuddy. It runs inside an item print's
#: check, whose whole budget is 6 s (`workflows/operation_activities.py`
#: `CHECK_BUDGET_SECONDS`), so it may take at most part of it; the client's own 30 s
#: would take all of it.
ASSIGNMENTS_TIMEOUT = 2.5


#: How long :func:`read_catalog` waits for Bambuddy: the catalog is the pull's second
#: read, and a slow one must not hold the spools back (the client's own 30 s would).
CATALOG_TIMEOUT = 5.0


async def read_catalog(settings: StoredSettings) -> list[SpoolCatalogEntry] | None:
    """Bambuddy's spool catalog (``GET /inventory/catalog``), or ``None``, logged, when
    Bambuddy refuses, answers in another shape or no JSON (it has no such route before
    1.2.6b1), or does not answer within :data:`CATALOG_TIMEOUT`: the pull then leaves the
    insert types alone. Bounded and a wait on Bambuddy as `read_assignments` is."""
    async with waiting_on_bambuddy():
        try:
            async with asyncio.timeout(CATALOG_TIMEOUT), client_for(settings) as client:
                return await client.spool_catalog()
        except TimeoutError:
            logger.warning("no spool catalog from Bambuddy: no answer within %s s", CATALOG_TIMEOUT)
        except ApiError as error:
            logger.warning("no spool catalog from Bambuddy: %s", error.detail)
        except ValueError as error:
            # A pydantic ValidationError (another shape) or a JSONDecodeError (no JSON).
            logger.warning("no spool catalog from Bambuddy: an answer of another shape: %s", error)
    return None


async def read_assignments(settings: StoredSettings) -> list[SpoolAssignment]:
    """:func:`try_assignments`, with ``[]`` for a failed read."""
    return await try_assignments(settings) or []


async def try_assignments(settings: StoredSettings) -> list[SpoolAssignment] | None:
    """Bambuddy's tray assignments (``GET /inventory/assignments``), which place a linked
    SB spool where no registry printer reports it (plan Decision 3, until #6263): none
    with no BB URL, and ``None``, logged, when Bambuddy refuses, answers in another shape or
    no JSON, or does not answer within :data:`ASSIGNMENTS_TIMEOUT`, so the spools are
    placed by the reports alone. The read is a wait on Bambuddy (`waiting_on_bambuddy`),
    so a check cut short inside it names Bambuddy."""
    if not settings.bambuddy_url:
        return []
    async with waiting_on_bambuddy():
        try:
            async with asyncio.timeout(ASSIGNMENTS_TIMEOUT), client_for(settings) as client:
                return await client.spool_assignments()
        except TimeoutError:
            logger.warning(
                "no tray assignments from Bambuddy: no answer within %s s", ASSIGNMENTS_TIMEOUT
            )
        except ApiError as error:
            logger.warning("no tray assignments from Bambuddy: %s", error.detail)
        except ValueError as error:
            # A pydantic ValidationError (another shape) or a JSONDecodeError (no JSON).
            logger.warning(
                "no tray assignments from Bambuddy: an answer of another shape: %s", error
            )
    return None


async def scheduled_pull(
    store: Filament, load: Callable[[], StoredSettings], sync: Callable[[], FilamentSync]
) -> Outcome:
    """The Schedule's pull (`SpoolRefresh`): ``skipped`` with ``filament_sync.spools``
    ``off`` or no BB URL, ``failed`` when BB or the store fails (logged, never raised),
    else ``pulled``. ``sync`` reads the setting as it is now (#6262 Task 13)."""
    if (await asyncio.to_thread(sync)).spools == "off":
        logger.info("Bambuddy's spools are not pulled: the Filament sync is off")
        return "skipped"
    settings = await asyncio.to_thread(load)
    if not settings.bambuddy_url:
        logger.info("no Bambuddy configured; its spools are not pulled")
        return "skipped"
    try:
        report = await read_and_pull(store, settings)
    except Exception as error:
        logger.warning(
            "could not pull Bambuddy's spools",
            extra={"error": type(error).__name__},
            exc_info=True,
        )
        return "failed"
    if report.failed_bb_ids or report.invalid:
        logger.warning(
            "some Bambuddy spools were not pulled",
            extra={"bb_spool_ids": report.failed_bb_ids, "invalid": report.invalid},
        )
    return "pulled"
