"""The plan of the ScadBuddy-to-Bambuddy sync (#6265 Task 4, plan
`docs/superpowers/plans/2026-11-18-filament-bb-sync.md`): what SB wants Bambuddy to hold,
the field hashes, "newer change wins", and `plan`, which turns one `BbSnapshot` into the
`BbWrite` calls of one round.

The print worker reads Bambuddy and posts the snapshot to the plan route (Task 6), which
calls `plan`. `plan` reads SB, compares each item with what Bambuddy holds, and writes
nothing to SB but sync bookkeeping: the create intents (Decision 10) and their expiry
(R-T4-20), the error row of a rename onto a name Bambuddy already holds (R-T4-8), and the
seen copy of a link that has none (R-T4-22) with its marks of the fields that differ
(R-T4-27; R-T4-32 for an adoption of a row a person made, R-T4-35 for a field a person
changed on a row SB's lost create made), and the preset list hash of
a link that never pushed it (R-T4-31). The writes' answers reach SB through
``results`` (Task 5) only.

Every compare and every hash of a Bambuddy value goes through `seen_row`/`seen_value`
(Task 3, R-T3-11), so both sides are in one JSON form; the archive state compares as
``archived_at is None`` on both sides, never as times (R-T3-2).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.bambuddy.models import as_utc
from scadbuddy.core.cancel import CancelToken, check, watched
from scadbuddy.core.settings import FilamentSync, SyncDirection, pushes
from scadbuddy.filament import models as m
from scadbuddy.filament.bambuddy_pull import parse_rows, snapshot
from scadbuddy.filament.bb_sync_models import (
    BbSnapshot,
    BbWrite,
    Kind,
    Op,
    SyncPlan,
    json_form,
    pushes_anything,
    seen_row,
    seen_value,
)
from scadbuddy.filament.colour import canonical_colours, canonical_rgba
from scadbuddy.filament.component import Filament
from scadbuddy.filament.presets import effective_k_values_of, effective_presets_of
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.filament.types import OVERRIDABLE

Conn = Connection[DictRow]

#: A spool field's data type (Decision 14), by Bambuddy's names; a field not listed is a
#: ``spools`` field.
_FIELD_TYPE: dict[str, str] = {
    "weight_used": "weights",
    "weight_locked": "weights",
    "last_dried_at": "weights",
    "slicer_filament": "presets",
    "slicer_filament_name": "presets",
    "filament_presets": "presets",
    "k_profiles": "presets",
    "suppliers": "suppliers",
    "location_id": "locations",
}

#: The spool fields that are a whole-list ``PUT`` of their own, each its own `Op`.
_LISTS: dict[str, Op] = {
    "suppliers": "suppliers",
    "k_profiles": "k_profiles",
    "filament_presets": "presets",
}
#: ``SpoolCreate`` has no such field (`sync-routes.json`, v1.3.1): a new spool takes it on
#: the next pass's update.
_NOT_ON_CREATE = frozenset({"last_dried_at"})
#: A ``PATCH`` that sends ``weight_used`` locks the weight unless ``weight_locked`` is sent
#: too (`sync-routes.json` ``write_semantics.spool_update_partial``, Bambuddy v1.3.1
#: ``inventory.py`` lines 1701-1716), so the lock always goes with it.
_GOES_WITH = {"weight_used": "weight_locked"}

#: The error label of a linked row whose new SB name an unlinked Bambuddy row holds
#: (R-T4-8); Bambuddy would answer 409.
NAME_TAKEN = "name_taken"

#: A Bambuddy row made after an intent may carry a time a little before it: the two
#: clocks agree within about 0.25 s (spec §8.2, measured 2026-11-15).
_CLOCK_SKEW = timedelta(seconds=5)

#: The SB columns whose ``edited`` events change a field (Decision 8). ``presets`` and
#: ``k_values`` are the keys `set_presets`/`set_k_values` write.
_SB_COLUMNS: dict[str, tuple[str, ...]] = {
    "material": ("material_id",),
    "subtype": ("subtype_id",),
    "brand": ("brand_id",),
    "category": ("category_id",),
    "effect_type": ("effect_id",),
    "color_name": ("colour_name",),
    "rgba": ("rgba",),
    "extra_colors": ("extra_colours",),
    "label_weight": ("label_weight_g",),
    "core_weight": ("insert_type_id",),
    "core_weight_catalog_id": ("insert_type_id",),
    "nozzle_temp_min": ("nozzle_temp_min",),
    "nozzle_temp_max": ("nozzle_temp_max",),
    "note": ("note",),
    "cost_per_kg": ("cost_per_kg",),
    "low_stock_threshold_pct": ("low_stock_pct",),
    "material_number": ("type_id",),
    "weight_locked": ("weight_locked",),
    "slicer_filament": ("presets",),
    "slicer_filament_name": ("presets",),
    "filament_presets": ("presets",),
    "k_profiles": ("k_values",),
    "suppliers": ("type_id", "invoice_line_id"),
}
#: The fields whose change time is the newest event of these kinds (Decision 8).
_EVENT_KINDS: dict[str, tuple[str, ...]] = {
    "location_id": ("moved",),
    "archived_at": ("archived", "restored"),
    "last_dried_at": ("dried",),
}
#: The fields a spool takes from its type whatever it overrides: their change time also
#: counts the type's ``updated_at``.
_FROM_TYPE = frozenset(
    {"material_number", "slicer_filament", "slicer_filament_name", "filament_presets",
     "k_profiles", "suppliers"}
)  # fmt: skip


class _Unseen:
    """The type of `UNSEEN`."""

    def __repr__(self) -> str:
        return "UNSEEN"


#: What SB last saw in Bambuddy when it saw nothing (a link made before Task 3, with no
#: ``bb_seen``): it equals no value, so only the change times decide (R-T4-4).
UNSEEN: Any = _Unseen()

#: R-T4-27: the key of `bb_pushed` that holds, per field a seeded seen copy left out, the
#: hashes of SB's and Bambuddy's values at seeding (``{"sb": …, "bb": …}``). No pushed
#: field has this name (`test_the_reserved_key_is_no_field_name`).
UNSEEN_KEY = "unseen"


@dataclass(frozen=True)
class Pending:
    """A reference to an SB row Bambuddy has no id for yet: a spool that names it waits
    for it while its create is planned or in flight (`plan` counts it ``deferred``, or
    ``blocked`` when this pass did not read that row's listing), and else leaves the
    field out."""

    kind: Kind
    sb_id: UUID


@dataclass(frozen=True)
class BbRefs:
    """The Bambuddy ids of SB rows, for the fields of a spool that name one: insert types
    (BB spool catalog entries), locations, suppliers and printers."""

    insert_types: Mapping[UUID, int] = field(default_factory=dict)
    locations: Mapping[UUID, int] = field(default_factory=dict)
    suppliers: Mapping[UUID, int] = field(default_factory=dict)
    printers: Mapping[UUID, int] = field(default_factory=dict)

    @classmethod
    def read(cls, conn: Conn) -> BbRefs:
        def ids(sql: str) -> dict[UUID, int]:
            return {row["id"]: row["bb"] for row in conn.execute(sql).fetchall()}

        return cls(
            insert_types=ids(
                "SELECT id, bb_catalog_id AS bb FROM filament_insert_types"
                " WHERE bb_catalog_id IS NOT NULL"
            ),
            locations=ids(
                "SELECT id, bb_location_id AS bb FROM filament_locations"
                " WHERE bb_location_id IS NOT NULL"
            ),
            suppliers=ids(
                "SELECT id, bb_supplier_id AS bb FROM filament_suppliers"
                " WHERE bb_supplier_id IS NOT NULL"
            ),
            printers=ids(
                "SELECT id, bambuddy_printer_id AS bb FROM printers"
                " WHERE bambuddy_printer_id IS NOT NULL"
            ),
        )

    def without(self, rows: Iterable[tuple[str, UUID]]) -> BbRefs:
        """These refs less the ``(kind, sb_id)`` rows given, which a spool then names as
        a `Pending` (#6688: a linked row Bambuddy may have deleted)."""
        gone = set(rows)

        def keep(kind: str, ids: Mapping[UUID, int]) -> dict[UUID, int]:
            return {sb: bb for sb, bb in ids.items() if (kind, sb) not in gone}

        return replace(
            self,
            insert_types=keep("insert_type", self.insert_types),
            locations=keep("location", self.locations),
            suppliers=keep("supplier", self.suppliers),
        )


#: The advisory lock one plan or results call of a Bambuddy instance's sync holds.
SYNC_LOCK = "scadbuddy:filament:bb_sync:"


class SyncBusyError(Exception):
    """Another plan or results call of this Bambuddy instance's sync is running (#6902)."""


def hold_sync(conn: Conn, instance: str) -> None:
    """Take the instance's sync lock for ``conn``'s transaction, without waiting, else
    `SyncBusyError` (#6902). On prod a call the print worker had given up on ran on, and
    its retries ran beside it, queued on the state row's lock: so a second copy of a call
    never runs beside the first, and never waits on its row locks either. The CRUD route
    answers 503 ``sync_busy``, which the worker's client tries again after a backoff."""
    held = conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS held",
        (SYNC_LOCK + instance,),
    ).fetchone()
    if held is None or not held["held"]:
        raise SyncBusyError(f"a sync call of Bambuddy {instance} is running")


def field_hash(value: Any) -> str:
    """The hash of one field's value (Decision 7): the first 16 hex of SHA-256 of its
    canonical JSON (sorted keys, no spaces). Callers hash the compare form
    (`compare_form`), so ``rgba`` and ``extra_colors`` are canonical."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def is_printer_place(location: m.Location | Mapping[str, Any]) -> bool:
    """A printer's place: an AMS unit, a tray, the external spool place or a plain
    printer place, all of them ``printer_id IS NOT NULL`` (F18). It is never pushed as a
    Bambuddy location, and a spool in one leaves BB's ``location_id`` alone."""
    if isinstance(location, Mapping):
        return location.get("printer_id") is not None
    return location.printer_id is not None


# -- what SB wants Bambuddy to hold ---------------------------------------------------


def desired_spool(conn: Conn, spool_id: UUID, refs: BbRefs, sync: FilamentSync) -> dict[str, Any]:
    """The spool as Bambuddy should hold it: `desired_and_unmapped`'s values."""
    return desired_and_unmapped(conn, spool_id, refs, sync)[0]


@dataclass(frozen=True)
class SpoolFacts:
    """What `desired_and_unmapped` reads of SB for a set of spools, read in a fixed number
    of queries whatever their number (#6902: per spool, the plan made about 20 queries a
    spool and timed out on prod's 110 spools). Only the data types ``sync`` pushes are
    read."""

    spools: Mapping[UUID, m.Spool]
    codes: Mapping[UUID, str]
    core_weights: Mapping[UUID, Any]
    places: Mapping[UUID, dict[str, Any]]
    presets: Mapping[UUID, list[m.EffectivePreset]]
    k_values: Mapping[UUID, list[m.EffectiveKValue]]
    #: Each type's unarchived products of unarchived suppliers, by ``upper(sku)``, id.
    products: Mapping[UUID, list[dict[str, Any]]]
    #: Each product's newest price point.
    prices: Mapping[UUID, Any]
    #: The unarchived supplier of each spool's invoice.
    bought: Mapping[UUID, UUID]

    @classmethod
    def read(cls, conn: Conn, spool_ids: Iterable[UUID], sync: FilamentSync) -> SpoolFacts:
        ids = list(dict.fromkeys(spool_ids))
        spools = FilamentSpoolStore.read_many(conn, ids)
        found = list(spools)
        type_ids = list({s.type_id for s in spools.values() if s.type_id is not None})

        def by_id(sql: str, wanted: list[UUID], column: str) -> dict[UUID, Any]:
            if not wanted:
                return {}
            return {r["id"]: r[column] for r in conn.execute(sql, (wanted,)).fetchall()}

        codes: dict[UUID, str] = {}
        core_weights: dict[UUID, Any] = {}
        if pushes(sync.spools):
            codes = by_id(
                "SELECT id, code FROM filament_spool_types WHERE id = ANY(%s)", type_ids, "code"
            )
            inserts = list(
                {
                    s.effective.insert_type_id
                    for s in spools.values()
                    if s.effective.insert_type_id is not None
                }
            )
            core_weights = by_id(
                "SELECT id, empty_weight_g FROM filament_insert_types WHERE id = ANY(%s)",
                inserts,
                "empty_weight_g",
            )
        places: dict[UUID, dict[str, Any]] = {}
        located = list({s.location_id for s in spools.values() if s.location_id is not None})
        if located:
            places = {
                row["id"]: dict(row)
                for row in conn.execute(
                    "SELECT l.id, l.printer_id, upper(btrim(p.model)) AS model"
                    " FROM filament_locations l LEFT JOIN printers p ON p.id = l.printer_id"
                    " WHERE l.id = ANY(%s)",
                    (located,),
                ).fetchall()
            }
        presets: dict[UUID, list[m.EffectivePreset]] = {}
        k_values: dict[UUID, list[m.EffectiveKValue]] = {}
        if pushes(sync.presets):
            presets = effective_presets_of(conn, found)
            k_values = effective_k_values_of(conn, found)
        products: dict[UUID, list[dict[str, Any]]] = {}
        prices: dict[UUID, Any] = {}
        bought: dict[UUID, UUID] = {}
        if pushes(sync.suppliers):
            if type_ids:
                for row in conn.execute(
                    "SELECT p.id, p.supplier_id, p.sku, p.type_id FROM filament_supplier_products p"
                    " JOIN filament_suppliers s ON s.id = p.supplier_id"
                    " WHERE p.type_id = ANY(%s) AND p.archived_at IS NULL"
                    " AND s.archived_at IS NULL ORDER BY upper(p.sku), p.id",
                    (type_ids,),
                ).fetchall():
                    products.setdefault(row["type_id"], []).append(row)
            prices = SuppliersStore._newest(conn, [r["id"] for rs in products.values() for r in rs])
            if found:
                bought = {
                    row["id"]: row["supplier_id"]
                    for row in conn.execute(
                        "SELECT sp.id, i.supplier_id FROM filament_spools sp"
                        " JOIN filament_invoice_lines l ON l.id = sp.invoice_line_id"
                        " JOIN filament_invoices i ON i.id = l.invoice_id"
                        " JOIN filament_suppliers s ON s.id = i.supplier_id"
                        " WHERE sp.id = ANY(%s) AND s.archived_at IS NULL",
                        (found,),
                    ).fetchall()
                }
        return cls(spools, codes, core_weights, places, presets, k_values, products, prices, bought)


def desired_and_unmapped(
    conn: Conn,
    spool_id: UUID,
    refs: BbRefs,
    sync: FilamentSync,
    facts: SpoolFacts | None = None,
) -> tuple[dict[str, Any], int]:
    """The spool as Bambuddy should hold it, by BB's names, for the data types that push
    (Decision 14), in SB's own forms (`compare_form` and `body_value` turn them into the
    compare and the body form). A row Bambuddy has no id for is a `Pending`. A field SB
    has no value for that Bambuddy cannot take empty is left out: ``label_weight`` with no
    label weight, ``core_weight`` and ``core_weight_catalog_id`` with no insert type,
    ``low_stock_threshold_pct`` outside Bambuddy's 1-99, ``weight_used`` with no label
    weight or no grams left. ``archived_at`` is the archive state. Also the number of K
    values left out for a printer with no Bambuddy id (``unmapped_k``, Decision 14).
    ``facts`` is what a caller read once for many spools (`SpoolFacts`, read under the same
    ``sync``); without it the one spool's are read."""
    if facts is None:
        facts = SpoolFacts.read(conn, [spool_id], sync)
    spool = facts.spools.get(spool_id)
    if spool is None:
        raise LookupError(f"no spool {spool_id}")
    values = spool.effective
    out: dict[str, Any] = {}
    unmapped = 0
    if pushes(sync.spools):
        code = None if spool.type_id is None else facts.codes.get(spool.type_id)
        out |= {
            "material": spool.names.material,
            "subtype": spool.names.subtype,
            "brand": spool.names.brand,
            "category": spool.names.category,
            "effect_type": spool.names.effect,
            "color_name": values.colour_name,
            "rgba": values.rgba,
            "extra_colors": values.extra_colours,
            "nozzle_temp_min": values.nozzle_temp_min,
            "nozzle_temp_max": values.nozzle_temp_max,
            "note": spool.note,
            "cost_per_kg": values.cost_per_kg,
            "material_number": code,
            "archived_at": spool.archived_at,
        }
        if values.label_weight_g is not None:
            out["label_weight"] = values.label_weight_g
        pct = values.low_stock_pct
        if pct is not None and 1 <= pct <= 99:
            out["low_stock_threshold_pct"] = pct
        insert = values.insert_type_id
        if insert is not None and insert in facts.core_weights:
            out["core_weight"] = facts.core_weights[insert]
            out["core_weight_catalog_id"] = refs.insert_types.get(insert) or Pending(
                "insert_type", insert
            )
    if pushes(sync.weights):
        label, left = values.label_weight_g, spool.grams_left
        if label is not None and left is not None:
            out["weight_used"] = round(max(0.0, label - left), 1)
        out["weight_locked"] = spool.weight_locked
        out["last_dried_at"] = spool.last_dried_at
    place = None if spool.location_id is None else facts.places.get(spool.location_id)
    if pushes(sync.presets):
        presets = facts.presets.get(spool_id, [])
        chosen = _preset(presets, place)
        # Bambuddy knows a ScadBuddy preset (#7176) by its mirror's id only: one with no
        # mirror is sent as none, never as its `sb-` ref.
        sent = None if chosen is None else chosen.for_bambuddy()
        out["slicer_filament"] = sent
        out["slicer_filament_name"] = (
            None if chosen is None or sent is None else chosen.slicer_filament_name
        )
        out["filament_presets"] = [
            {
                "printer_model": p.printer_model,
                "nozzle_diameter": p.nozzle_diameter,
                "slicer_filament": p.for_bambuddy(),
                "slicer_filament_name": p.slicer_filament_name,
            }
            for p in presets
            if p.for_bambuddy() is not None
        ]
        k_values = facts.k_values.get(spool_id, [])
        unmapped = sum(1 for k in k_values if k.printer_id not in refs.printers)
        out["k_profiles"] = [
            {
                "printer_id": refs.printers[k.printer_id],
                "extruder": k.extruder,
                "nozzle_diameter": k.nozzle_diameter,
                "nozzle_type": k.nozzle_type,
                "k_value": k.k_value,
                "name": k.name,
                "cali_idx": k.cali_idx,
                "setting_id": k.setting_id,
            }
            for k in k_values
            if k.printer_id in refs.printers
        ]
    if pushes(sync.suppliers):
        out["suppliers"] = _supplier_links(spool, refs, facts)
    if pushes(sync.locations) and (place is None or not is_printer_place(place)):
        out["location_id"] = (
            None
            if place is None
            else refs.locations.get(place["id"]) or Pending("location", place["id"])
        )
    return out, unmapped


def _preset(
    presets: Sequence[m.EffectivePreset], place: Mapping[str, Any] | None
) -> m.EffectivePreset | None:
    """The spool's effective preset for the model of the printer it is loaded in (its
    smallest nozzle), else the first by model and nozzle (Decision 14)."""
    model = None if place is None else place.get("model")
    loaded = [p for p in presets if model and p.printer_model == model]
    ordered = sorted(loaded or presets, key=lambda p: (p.printer_model, p.nozzle_diameter))
    return ordered[0] if ordered else None


def _supplier_links(spool: m.Spool, refs: BbRefs, facts: SpoolFacts) -> list[dict[str, Any]]:
    """One link per supplier of a product of the spool's type (its newest priced product,
    else its first by SKU): the SKU and the newest per-kg price; the supplier of the
    spool's invoice is the purchase source, linked on its own when it has no such
    product (Decision 14). Archived suppliers and products are left out."""
    rows = [] if spool.type_id is None else facts.products.get(spool.type_id, [])
    source = facts.bought.get(spool.id)
    chosen: dict[UUID, dict[str, Any]] = {}
    for row in rows:
        price = facts.prices.get(row["id"])
        held = chosen.get(row["supplier_id"])
        if held is None or (held["price"] is None and price is not None):
            chosen[row["supplier_id"]] = {"sku": row["sku"], "price": price}
    if source is not None and source not in chosen:
        chosen[source] = {"sku": None, "price": None}
    return [
        {
            "supplier_id": refs.suppliers.get(supplier) or Pending("supplier", supplier),
            "supplier_article_number": held["sku"],
            "quoted_price_per_kg": None if held["price"] is None else held["price"].price_per_kg,
            "is_purchase_source": supplier == source,
        }
        for supplier, held in chosen.items()
    ]


# -- the forms of one value -----------------------------------------------------------


def k_form(rows: Sequence[Any] | None) -> list[dict[str, Any]]:
    """K-profiles as the sync compares them (Decision 14): ``printer_id``, ``extruder``,
    ``nozzle_diameter`` and ``k_value`` (4 decimals), sorted. Takes BB's rows, SB's
    desired rows or a stored ``bb_k_profiles``."""
    out = []
    for row in rows or ():
        data = row if isinstance(row, Mapping) else cast("Any", row).model_dump()
        k = data.get("k_value")
        out.append(
            {
                "printer_id": data.get("printer_id"),
                "extruder": data.get("extruder"),
                "nozzle_diameter": None
                if data.get("nozzle_diameter") is None
                else str(data["nozzle_diameter"]),
                "k_value": None if k is None else round(float(k), 4),
            }
        )
    return sorted(out, key=lambda r: json.dumps(r, sort_keys=True))


def compare_form(field: str, value: Any) -> Any:
    """SB's ``value`` of ``field`` in the one form compares and hashes take (R-T3-11):
    `seen_value`'s, the archive state as ``archived_at is None``, the K-profiles as
    `k_form`, the preset list as JSON sorted by key."""
    if field == "archived_at":
        return value is None
    if field == "k_profiles":
        return k_form(value)
    if field == "filament_presets":
        rows = json_form(value or [])
        return sorted(cast("list[Any]", rows), key=lambda r: json.dumps(r, sort_keys=True))
    return seen_value(field, value)


def body_value(field: str, value: Any) -> Any:
    """SB's ``value`` of ``field`` as Bambuddy takes it: JSON values, ``rgba`` as
    ``RRGGBBAA`` without ``#``, ``extra_colors`` comma-joined the same way (Decision 14)."""
    if field == "rgba":
        colour = canonical_rgba(value)
        return None if colour is None else colour[1:]
    if field == "extra_colors":
        text = ",".join(value) if isinstance(value, list | tuple) else value
        colours = canonical_colours(text if isinstance(text, str) else None)
        return None if colours is None else ",".join(c[1:] for c in colours)
    return json_form(value)


def bb_form(field: str, seen: Mapping[str, Any], k_profiles: Any) -> Any:
    """Bambuddy's value of ``field`` in the compare form, from a `seen_row` and its
    K-profiles; ``None`` for the preset list, which no BB row carries."""
    if field == "archived_at":
        return seen.get("archived_at") is None
    if field == "k_profiles":
        return UNSEEN if k_profiles is None else k_form(k_profiles)
    return seen.get(field)


# -- "newer change wins" --------------------------------------------------------------


def decide(
    *,
    sb: Any,
    bb_now: Any,
    bb_seen: Any,
    sb_at: datetime,
    bb_at: datetime | None,
    direction: SyncDirection,
    seen_at: datetime | None = None,
) -> bool:
    """Whether to push one field (Decision 8), all values in the compare form: never
    when SB's value is Bambuddy's; under ``sb_to_bb`` always otherwise; under ``two_way``
    when Bambuddy's value is still what SB last saw there, or SB's change is strictly
    after BB's ``updated_at`` (a tie goes to Bambuddy, as on the pull). When Bambuddy's
    value moved off what SB saw and its row has no ``updated_at``, its age is unknown,
    and SB's value is never pushed over it (decision 77). When SB never saw BB's value
    (`UNSEEN`), only a BB time SB's change is strictly after lets it win (R-T4-4). With
    no BB time, a field a seeded seen copy left out (``seen_at`` is that copy's
    ``bb_seen_at``) waits for an SB change strictly after ``seen_at`` (R-T4-22). This is
    the rule of spools, whose fields have their own SB change time; the other kinds take
    `_row_wins`."""
    if sb == bb_now or not pushes(direction):
        return False
    if direction == "sb_to_bb":
        return True
    if bb_seen is UNSEEN:
        if bb_at is not None:
            return sb_at > bb_at
        return seen_at is not None and sb_at > seen_at
    if bb_now == bb_seen:
        return True
    return bb_at is not None and sb_at > bb_at


def _sb_changed(sb: Any, bb_seen: Any, mark: Mapping[str, str] | None) -> bool:
    """Whether SB changed a field of a row kind since SB last saw Bambuddy's value: for a
    field in the seen copy, SB's value is not the seen one; for a field a seeded copy left
    out, SB's hash is not the one ``mark`` recorded at seeding (no mark: not known)."""
    if bb_seen is UNSEEN:
        return mark is not None and field_hash(sb) != mark["sb"]
    return bool(sb != bb_seen)


def _bb_changed(bb_now: Any, bb_seen: Any, mark: Mapping[str, str] | None) -> bool:
    """`_sb_changed` for Bambuddy's value (no mark: not known, so changed)."""
    if bb_seen is UNSEEN:
        return mark is None or field_hash(bb_now) != mark["bb"]
    return bool(bb_now != bb_seen)


def _row_wins(sb: Any, bb_now: Any, bb_seen: Any, mark: Mapping[str, str] | None) -> bool:
    """``two_way`` for one field of a supplier, location, insert type or filament entry,
    whose SB row has one change time for all its fields (R-T4-27). The row time never
    shows when SB changed this field, so SB's value is pushed only when SB changed it
    (`_sb_changed`) and Bambuddy kept its value (the seen one, or the one at seeding).
    When both changed it, nothing is pushed (R-T4-33): the row time cannot order SB's
    change against Bambuddy's, so `_conflict` counts it."""
    return _sb_changed(sb, bb_seen, mark) and not _bb_changed(bb_now, bb_seen, mark)


def _conflict(sb: Any, bb_now: Any, bb_seen: Any, mark: Mapping[str, str] | None) -> bool:
    """R-T4-33: both sides changed a field of a row kind since SB last saw it."""
    return _sb_changed(sb, bb_seen, mark) and _bb_changed(bb_now, bb_seen, mark)


def sb_changed_at(conn: Conn, spool_id: UUID, field: str) -> datetime:
    """When SB last changed the spool's ``field`` (BB's name), the one rule of Decision 8
    (F12; Task 9's pull uses it too): the newest ``edited`` event naming one of its
    columns, else the spool's ``created_at``; for a value the spool takes from its type
    (an override it does not set, or a field only the type has) the later of that, the
    type's ``updated_at`` and the newest ``edited`` event naming ``type_id``. For
    ``weight_used`` it is the newest event with a weight (a reading or a delta), for
    ``location_id`` the newest ``moved``, for the archive state the newest ``archived`` or
    ``restored``, for ``last_dried_at`` the newest ``dried``. For ``suppliers`` it also
    counts the type's products and price points."""
    row = conn.execute(
        "SELECT s.*, t.updated_at AS type_at FROM filament_spools s"
        " LEFT JOIN filament_spool_types t ON t.id = s.type_id WHERE s.id = %s",
        (spool_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no spool {spool_id}")
    times: list[datetime | None] = [row["created_at"]]

    def newest(where: str, params: Sequence[Any]) -> None:
        found = conn.execute(
            f"SELECT max(at) AS at FROM filament_spool_events WHERE spool_id = %s AND {where}",
            (spool_id, *params),
        ).fetchone()
        times.append(None if found is None else found["at"])

    def edited(columns: Sequence[str]) -> None:
        newest(
            "kind = 'edited' AND jsonb_typeof(data -> 'changes') = 'object'"
            " AND data -> 'changes' ?| %s",
            (list(columns),),
        )

    if field == "weight_used":
        newest("(grams_after IS NOT NULL OR delta_g IS NOT NULL)", ())
    elif field in _EVENT_KINDS:
        newest("kind = ANY(%s)", (list(_EVENT_KINDS[field]),))
    else:
        columns = _SB_COLUMNS.get(field, ())
        if columns:
            edited(columns)
        inherited = field in _FROM_TYPE or any(
            c in OVERRIDABLE and row.get(c) is None for c in columns
        )
        if row["type_id"] is not None and inherited:
            times.append(row["type_at"])
            edited(("type_id",))
    if field == "suppliers" and row["type_id"] is not None:
        found = conn.execute(
            "SELECT greatest(max(p.updated_at), max(pp.updated_at)) AS at"
            " FROM filament_supplier_products p"
            " LEFT JOIN filament_price_points pp ON pp.product_id = p.id WHERE p.type_id = %s",
            (row["type_id"],),
        ).fetchone()
        times.append(None if found is None else found["at"])
    return max(t for t in times if t is not None)


# -- the plan -------------------------------------------------------------------------

#: The plan's phases (Decision 10's order), and last the items Bambuddy refused in an
#: earlier pass (R-T4-2).
_Phase = Literal[0, 1, 2, 3, 4]
_RETRY: _Phase = 4


@dataclass
class _Candidate:
    write: BbWrite
    intent: bool


@dataclass
class _Item:
    """One SB row of a kind Bambuddy keeps (not a spool): its desired values (compare
    form), its link, and what was pushed and seen. Its row time is not read: it never
    shows when SB changed one field (R-T4-27, R-T4-34)."""

    sb_id: UUID
    bb_id: int | None
    archived: bool
    name: str
    desired: dict[str, Any]
    body: dict[str, Any]
    pushed: dict[str, str]
    #: `UNSEEN_KEY`'s marks: a seeded field's hashes at seeding (R-T4-27).
    unseen: dict[str, dict[str, str]]
    #: ``bb_pushed`` holds `UNSEEN_KEY`. On an unlinked row: an adoption of a row a
    #: person made is under way (R-T4-32), whatever intent it recorded.
    marked: bool
    seen: dict[str, Any] | None
    seen_at: datetime | None


@dataclass
class _Planner:
    conn: Conn
    snap: BbSnapshot
    sync: FilamentSync
    refs: BbRefs
    intents: dict[tuple[str, UUID], datetime]
    #: What each intent's create sent: its field hashes (R-T4-35; ``None``: not known).
    sent: dict[tuple[str, UUID], dict[str, str] | None]
    #: The items whose last write failed (`filament_bb_push_errors`), by when.
    errors: dict[tuple[str, UUID], datetime]
    phases: dict[_Phase, list[_Candidate]] = field(
        default_factory=lambda: {0: [], 1: [], 2: [], 3: [], 4: []}
    )
    #: R-T4-1: waits for a write planned earlier in this pass (sets ``more``).
    deferred: set[tuple[str, UUID]] = field(default_factory=set)
    #: Refused by Bambuddy, not tried again in this round.
    failed: set[tuple[str, UUID]] = field(default_factory=set)
    #: Held by a read this pass did not get.
    blocked: set[tuple[str, UUID]] = field(default_factory=set)
    unmapped_k: int = 0
    #: R-T4-33: the fields of row kinds both sides changed (`_conflict`).
    conflicts: int = 0
    #: The rows whose create (or adopt) is planned in this round.
    creating: set[tuple[str, UUID]] = field(default_factory=set)
    #: The archived rows of the kinds `rows` plans (R-T4-19).
    archived: set[tuple[str, UUID]] = field(default_factory=set)
    #: #6688 (R-6688-1, R-6688-5): linked rows missing from their complete listing whose
    #: probe this round plans, and those whose probe Bambuddy refused in this pass. A
    #: spool that names one waits.
    probing: set[tuple[str, UUID]] = field(default_factory=set)
    unsure: set[tuple[str, UUID]] = field(default_factory=set)
    #: R-6688-9: rows missing from their listing whose probe the round's cap left out.
    #: They count ``deferred``, and a spool that names one waits as for a probe.
    capped: set[tuple[str, UUID]] = field(default_factory=set)
    #: The round's write cap (`plan`'s ``limit``, bounded by `PlanRequest`), which
    #: `missing` checks a probe against.
    limit: int = field(kw_only=True)
    #: Every spool's SB data, read once by `spools` (#6902).
    facts: SpoolFacts | None = None
    #: Stops the plan between spools when its caller went away (#6902).
    cancel: CancelToken | None = field(default=None, kw_only=True)

    @property
    def full(self) -> bool:
        return self.snap.full

    def add(
        self,
        phase: _Phase,
        kind: Kind,
        sb_id: UUID,
        op: Op,
        bb_id: int | None,
        body: dict[str, Any] | list[Any],
        hashes: dict[str, str],
        *,
        intent: bool = False,
        retry_last: bool = True,
    ) -> None:
        if retry_last and (kind, sb_id) in self.errors:
            phase = _RETRY
        write = BbWrite(
            key=f"{kind}:{sb_id}:{op}",
            kind=kind,
            sb_id=sb_id,
            op=op,
            bb_id=bb_id,
            body=body,
            hashes=hashes,
        )
        self.phases[phase].append(_Candidate(write, intent))

    def intent_state(self, kind: Kind, sb_id: UUID) -> Literal["none", "old", "young"]:
        """Decision 10, F9: an intent started after the snapshot was read is a create
        whose answer this pass lost (``young``); an older one may have made a BB row."""
        started = self.intents.get((kind, sb_id))
        if started is None:
            return "none"
        return "young" if started > self.snap.read_at else "old"

    def refused(self, kind: Kind, sb_id: UUID, *, create: bool) -> bool:
        """R-T4-1: whether an item Bambuddy refused gets no write in this round (it is
        counted ``failed``): one refused in this pass (its error row is newer than the
        snapshot) waits for the next pass, and a create refused earlier is tried again
        on a full pass only. The others go last (`add`, R-T4-2)."""
        at = self.errors.get((kind, sb_id))
        if at is None:
            return False
        if at > self.snap.read_at or (create and not self.full):
            self.failed.add((kind, sb_id))
            return True
        return False

    def read(self, kind: Kind) -> bool:
        """Whether this pass read Bambuddy's listing of ``kind``."""
        listing = {
            "supplier": self.snap.suppliers,
            "location": self.snap.locations,
            "insert_type": self.snap.catalog,
            "filament": self.snap.filaments,
        }.get(kind)
        return listing is not None

    def in_flight(self, ref: Pending) -> bool:
        """True while a row's create is planned in this round, or has an open intent and
        no failed write: a row Bambuddy refused is no reason to wait, or a spool naming it
        would wait every pass. A linked row missing from its listing is in flight while
        its probe is planned or failed in this pass (R-6688-5)."""
        key = (ref.kind, ref.sb_id)
        if key in self.probing or key in self.unsure or key in self.capped:
            return True
        return key not in self.errors and (key in self.creating or key in self.intents)

    def expire(self, kind: Kind, sb_id: UUID) -> None:
        """R-T4-20: close an intent that can no longer be answered (its row is archived,
        or a full pass newer than it found no row it made). The item is planned again as
        a fresh create when it is eligible. The plan keeps the intent in memory for this
        round, so a spool naming an archived row counts ``blocked`` once (R-T4-19)."""
        self.conn.execute(
            "DELETE FROM filament_bb_creates WHERE kind = %s AND sb_id = %s", (kind, sb_id)
        )

    def seed(
        self, table: str, sb_id: UUID, seen: dict[str, Any], marks: dict[str, dict[str, str]]
    ) -> None:
        """R-T4-22: store the seen copy of a linked row that has none, at ``snap.read_at``,
        and (R-T4-27) the marks of the fields it leaves out in ``bb_pushed[UNSEEN_KEY]``."""
        self.conn.execute(
            f"UPDATE {table} SET bb_seen = %s, bb_seen_at = %s,"
            " bb_pushed = bb_pushed || jsonb_build_object(%s::text, %s::jsonb)"
            " WHERE id = %s AND bb_seen IS NULL",
            (Jsonb(seen), self.snap.read_at, UNSEEN_KEY, Jsonb(marks), sb_id),
        )

    def unseen_marks(self, kind: Kind, sb_id: UUID, marks: dict[str, dict[str, str]]) -> None:
        """R-T4-32: store the seeding marks of the fields an adoption of a row a person
        made does not send, in ``bb_pushed[UNSEEN_KEY]`` (sync bookkeeping, like `seed`),
        also when there are none: the key marks the row as a person's (`_Item.marked`).
        `results` keeps the key when it merges the write's hashes (Task 5, R-T4-27)."""
        self.conn.execute(
            f"UPDATE {_MARK_TABLES[kind]} SET"
            " bb_pushed = bb_pushed || jsonb_build_object(%s::text, %s::jsonb)"
            " WHERE id = %s",
            (UNSEEN_KEY, Jsonb(marks), sb_id),
        )

    # -- fields ----------------------------------------------------------------------

    def changed(
        self,
        desired: Mapping[str, Any],
        bb_now: Mapping[str, Any],
        bb_seen: Mapping[str, Any] | None,
        pushed: Mapping[str, str],
        direction: Any,
        sb_at: Any = None,
        bb_at: datetime | None = None,
        seen_at: datetime | None = None,
        marks: Mapping[str, Mapping[str, str]] | None = None,
    ) -> list[str]:
        """The fields to push of one linked item, ``desired`` in the compare form.
        ``direction`` and ``sb_at`` are a field's direction and SB change time, each a
        callable of the field (the change time only asked for when ``two_way`` needs
        it). ``seen_at`` is ``bb_seen_at`` when ``bb_seen`` is set. A field ``bb_now``
        does not carry is compared by its hash alone; an event-driven pass skips a field
        whose hash matches what was pushed (Decision 7). ``marks`` is given for a kind
        with one SB change time per row, which ``two_way`` decides by `_row_wins` and
        ``sb_to_bb`` by the difference alone: such a kind passes no times (R-T4-34).
        ``sb_at`` and ``bb_at`` are a spool's."""
        out = []
        for name, value in desired.items():
            digest = field_hash(value)
            if name not in bb_now:
                if pushed.get(name) != digest:
                    out.append(name)
                continue
            if not self.full and pushed.get(name) == digest:
                continue
            now = bb_now[name]
            seen = UNSEEN if bb_seen is None else bb_seen.get(name, UNSEEN)
            way: SyncDirection = direction(name)
            if marks is not None and way == "two_way":
                if value != now:
                    mark = marks.get(name)
                    if _row_wins(value, now, seen, mark):
                        out.append(name)
                    elif _conflict(value, now, seen, mark):
                        self.conflicts += 1
                continue
            seeded = None if bb_seen is None else seen_at
            needs_time = (
                way == "two_way"
                and value != now
                and (seen is UNSEEN or now != seen)
                and (bb_at is not None or (seen is UNSEEN and seeded is not None))
            )
            when = sb_at(name) if needs_time else datetime.min.replace(tzinfo=UTC)
            if decide(
                sb=value,
                bb_now=now,
                bb_seen=seen,
                sb_at=when,
                bb_at=bb_at,
                direction=way,
                seen_at=seeded,
            ):
                out.append(name)
        return out

    # -- rows of their own: suppliers, locations, insert types, filament entries --------

    def rows(
        self,
        kind: Kind,
        items: Sequence[_Item],
        bb_rows: Sequence[Any] | None,
        direction: SyncDirection,
        readable: frozenset[str],
    ) -> None:
        """Each item's write: a linked one's changed fields (``readable`` are the fields
        BB's listing carries; the rest compare by hash), an unlinked unarchived one's
        create, or the adoption of BB's unlinked row of the same upper case name (a lost
        answer's, Decision 10, or one made in Bambuddy, which a create would clash with:
        its 409). Under ``two_way`` an adoption with no intent (a person made the row), or
        the retry of one (``marked``), sends none of SB's differing values and records
        their marks (R-T4-32); one with an intent sends only the fields Bambuddy still
        holds as the create sent them (``sent``), and marks the rest (R-T4-35). A linked
        item missing from the listing gets a probe (`missing`, #6688). With the
        listing not read (``None``) nothing is written. A full pass
        seeds a linked item's missing seen copy (R-T4-22), and closes the intent of an
        archived item or of a create no row matches (R-T4-20)."""
        self.archived |= {(kind, item.sb_id) for item in items if item.archived}
        if bb_rows is None or not pushes(direction):
            return
        by_id = {row.id: row for row in bb_rows}
        linked = {item.bb_id for item in items if item.bb_id is not None}
        by_name: dict[str, Any] = {}
        for row in bb_rows:
            if row.id not in linked:
                by_name.setdefault(row.name.strip().upper(), row)
        for item in items:
            if item.bb_id is not None:
                row = by_id.get(item.bb_id)
                if row is None:
                    self.missing(kind, item)
                    continue
                if self.refused(kind, item.sb_id, create=False):
                    continue
                seen, seen_at, marks = item.seen, item.seen_at, item.unseen
                table = _SEEN_TABLES.get(kind)
                if seen is None and table is not None and self.full:
                    current = self.bb_now(row, readable, None, None, item.desired)
                    seen = {n: v for n, v in current.items() if item.desired.get(n, UNSEEN) == v}
                    marks = {
                        n: {"sb": field_hash(item.desired[n]), "bb": field_hash(v)}
                        for n, v in current.items()
                        if n in item.desired and n not in seen
                    }
                    seen_at = self.snap.read_at
                    self.seed(table, item.sb_id, seen, marks)
                now = self.bb_now(row, readable, seen, seen_at, item.desired)
                names = self.changed(
                    item.desired,
                    now,
                    seen,
                    item.pushed,
                    lambda _f: direction,
                    seen_at=seen_at,
                    marks=marks,
                )
                if "name" in names and str(item.desired["name"]).strip().upper() in by_name:
                    self.name_taken(kind, item.sb_id)
                    continue
                if names:
                    self.add(
                        0,
                        kind,
                        item.sb_id,
                        "update",
                        item.bb_id,
                        {n: item.body[n] for n in names},
                        {n: field_hash(item.desired[n]) for n in names},
                    )
                continue
            if item.archived:
                if (kind, item.sb_id) in self.intents:
                    self.expire(kind, item.sb_id)
                continue
            if self.refused(kind, item.sb_id, create=False):
                continue
            state = self.intent_state(kind, item.sb_id)
            if state == "young":
                self.deferred.add((kind, item.sb_id))
                continue
            if self.refused(kind, item.sb_id, create=True):
                continue
            hashes = {n: field_hash(v) for n, v in item.desired.items()}
            found = by_name.pop(item.name.strip().upper(), None)
            if found is None and state == "old" and self.full:
                self.expire(kind, item.sb_id)
                del self.intents[kind, item.sb_id]
            self.creating.add((kind, item.sb_id))
            if found is not None:
                now = self.bb_now(found, readable, None, None, item.desired)
                names = [n for n, v in item.desired.items() if n in now and now[n] != v]
                person = item.marked or (kind, item.sb_id) not in self.intents
                if direction == "two_way":
                    # R-T4-32: a person made this row, and its values have an unknown
                    # age: the adoption sends none of SB's differing values. R-T4-35: SB
                    # made it (a lost create's answer), and a field Bambuddy no longer
                    # holds as the create sent it is a person's later edit: it is not
                    # sent either. Each held field gets its seeding marks (R-T4-27), so
                    # the link acts as a seeded one. A person's row is marked even with
                    # none held: that tells the retry of a lost answer, which finds the
                    # intent this adoption recorded.
                    sent = {} if person else self.sent.get((kind, item.sb_id)) or {}
                    held = [n for n in names if person or sent.get(n) != field_hash(now[n])]
                    if person or held:
                        self.unseen_marks(
                            kind,
                            item.sb_id,
                            {
                                n: {"sb": field_hash(item.desired[n]), "bb": field_hash(now[n])}
                                for n in held
                            },
                        )
                    hashes = {n: h for n, h in hashes.items() if n not in held}
                    names = [n for n in names if n not in held]
                self.add(
                    0,
                    kind,
                    item.sb_id,
                    "update",
                    found.id,
                    {n: item.body[n] for n in names},
                    hashes,
                    intent=(kind, item.sb_id) not in self.intents,
                )
                continue
            self.add(0, kind, item.sb_id, "create", None, item.body, hashes, intent=True)

    def missing(self, kind: Kind, item: _Item) -> None:
        """#6688 (R-6688-1): a linked item missing from its kind's listing, which the pass
        read whole (a listing is read whole or not at all, `bb_sync_run._maybe`). Bambuddy
        may have deleted the row: it gets one probe, whose 404 drops the link (`results`),
        so a later round makes the row again. A link `results` wrote after the snapshot
        was read (a create or a probe earlier in this pass) is not probed; nor is an item
        whose last write Bambuddy refused in this pass (R-T4-1), which a spool naming it
        then waits for (``unsure``, R-6688-5). A probe is a phase-0 write even for an
        item with an error row from an earlier pass, so the cap takes phase 0 first, in
        this order (`take`): a probe that does not fit is not planned, and its row counts
        ``deferred``, as the cap's cut would count it (``capped``, R-6688-9)."""
        key = (kind, item.sb_id)
        if item.seen is not None and item.seen_at is not None and item.seen_at > self.snap.read_at:
            return
        if self.refused(kind, item.sb_id, create=False):
            self.unsure.add(key)
            return
        # `take` walks phase 0 first, in insertion order, and nothing leaves phase 0, so its
        # length is the number of writes (updates, adoptions, creates and probes) `take` keeps
        # ahead of this probe (R-6688-9). A reorder breaks the cap.
        if len(self.phases[0]) >= self.limit:
            self.capped.add(key)
            self.deferred.add(key)
            return
        self.probing.add(key)
        self.add(0, kind, item.sb_id, "probe", item.bb_id, {}, {}, retry_last=False)

    def unconfirmed(self) -> set[tuple[str, UUID]]:
        """The linked rows a spool must not name by their Bambuddy id in this round: their
        probe is planned, failed in this pass, or was cut by the cap (R-6688-5, R-6688-9)."""
        return self.probing | self.unsure | self.capped

    def name_taken(self, kind: Kind, sb_id: UUID) -> None:
        """R-T4-8: a linked row renamed in SB to the name of an unlinked Bambuddy row would
        get a 409 on every pass. It gets no write, and one error row (kept as it is when
        one is there, so ``attempts`` does not grow)."""
        self.conn.execute(
            "INSERT INTO filament_bb_push_errors (kind, sb_id, error) VALUES (%s, %s, %s)"
            " ON CONFLICT (kind, sb_id) DO NOTHING",
            (kind, sb_id, NAME_TAKEN),
        )
        self.failed.add((kind, sb_id))

    def bb_now(
        self,
        row: Any,
        readable: frozenset[str],
        seen: dict[str, Any] | None,
        seen_at: datetime | None,
        desired: Mapping[str, Any],
    ) -> dict[str, Any]:
        """What Bambuddy holds for a row, in the compare form (`row_seen`). A copy
        ``results`` stored after this snapshot was read (F11) also keeps the old seen
        value of a field its write did not send and its answer did not match (R-T4-28).
        So a stored value stands over the listing's only when it is SB's value
        (``desired``): the write sent it, or the answer matched SB (R-T4-30). The
        listing's value stands for the rest."""
        now = row_seen(row, readable)
        if seen is not None and seen_at is not None and seen_at > self.snap.read_at:
            fresh = {k: v for k, v in seen.items() if k in readable and desired.get(k, UNSEEN) == v}
            return now | fresh
        return now

    # -- spools -----------------------------------------------------------------------

    def spools(self) -> None:
        good, _ = parse_rows(self.snap.spools)
        by_id = {bb.id: bb for bb in good}
        raw = {row.get("id"): row for row in self.snap.spools}
        rows = self.conn.execute(
            "SELECT s.id, s.archived_at, l.bb_spool_id, l.pushed_fields, l.bb_seen, l.bb_seeded,"
            " l.bb_seen_at, l.bb_updated_at, l.bb_k_profiles"
            " FROM filament_spools s LEFT JOIN filament_bb_links l ON l.spool_id = s.id"
            " ORDER BY s.number"
        ).fetchall()
        linked = {row["bb_spool_id"] for row in rows if row["bb_spool_id"] is not None}
        unlinked_bb = [bb for bb in good if bb.id not in linked]
        self.facts = SpoolFacts.read(self.conn, [row["id"] for row in rows], self.sync)
        for row in rows:
            check(self.cancel)
            if row["bb_spool_id"] is not None:
                self.linked_spool(row, by_id.get(row["bb_spool_id"]))
            elif pushes(self.sync.spools) and row["archived_at"] is None:
                self.new_spool(row["id"], unlinked_bb, raw)

    def desired(self, sb_id: UUID) -> dict[str, Any] | None:
        """The spool's desired values with their `Pending` references settled: ``None``
        when one names a row whose create is in flight (the spool waits; ``deferred``
        when this pass read that row's listing, so a later round can clear it, else
        ``blocked``, R-T4-1; ``blocked`` too when that row is archived, as no round plans
        it, R-T4-19, and when that row's probe failed in this pass, R-6688-5; an archived
        row whose probe this round plans is ``deferred``, as its answer settles the spool
        in a later round, R-6688-7); else the fields naming a row Bambuddy will not have
        this pass are left out (Decision 35)."""
        desired, unmapped = desired_and_unmapped(self.conn, sb_id, self.refs, self.sync, self.facts)
        self.unmapped_k += unmapped
        out = {}
        for name, value in desired.items():
            refs = list(pendings(value))
            waiting = [r for r in refs if self.in_flight(r)]
            if waiting:
                read = all(
                    (r.kind, r.sb_id) in self.probing
                    or (r.kind, r.sb_id) in self.capped
                    or (
                        self.read(r.kind)
                        and (r.kind, r.sb_id) not in self.archived
                        and (r.kind, r.sb_id) not in self.unsure
                    )
                    for r in waiting
                )
                (self.deferred if read else self.blocked).add(("spool", sb_id))
                return None
            if not refs:
                out[name] = value
        return out

    def linked_spool(self, row: Mapping[str, Any], bb: BbSpool | None) -> None:
        sb_id: UUID = row["id"]
        bb_id: int = row["bb_spool_id"]
        if self.refused("spool", sb_id, create=False):
            return
        seen: dict[str, Any] | None = row["bb_seen"]
        seen_k = row["bb_k_profiles"]
        # F11, R-T4-3: what `results` stored after this snapshot was read comes first, so
        # a spool written earlier in this pass (absent from the snapshot when it was
        # created) is planned from the answer, never probed.
        fresh = (
            seen is not None
            and row["bb_seen_at"] is not None
            and row["bb_seen_at"] > self.snap.read_at
        )
        if bb is None and not fresh:
            # Decision 13, F16: missing from a complete listing, it gets one probe; a
            # partial listing never unlinks, so it waits (``blocked``).
            if self.snap.spools_complete:
                self.add(1, "spool", sb_id, "probe", bb_id, {}, {})
            else:
                self.blocked.add(("spool", sb_id))
            return
        desired = self.desired(sb_id)
        if desired is None:
            return
        if fresh and seen_k is None:
            # R-T4-5: the answer's K-profiles are unknown, not an empty list.
            desired.pop("k_profiles", None)
        compare = {n: compare_form(n, v) for n, v in desired.items()}
        current: dict[str, Any]
        if seen is not None and fresh:
            # R-T4-30: a stored copy also keeps the old seen value of a field its write
            # did not send and its answer did not match (R-T4-28). A stored value stands
            # over the listing's only when it is SB's value; the listing's row stands for
            # the rest.
            listed = {} if bb is None else seen_row(bb)
            kept = {
                n: v for n, v in seen.items() if bb_form(n, seen, seen_k) == compare.get(n, UNSEEN)
            }
            current, current_k, bb_at = listed | kept, seen_k, row["bb_updated_at"]
            if "k_profiles" in compare and k_form(seen_k) != compare["k_profiles"]:
                if bb is None:
                    # No listed K-profiles to read: no K write in this round.
                    del compare["k_profiles"]
                else:
                    current_k = bb.k_profiles
        elif bb is not None:
            current, current_k, bb_at = seen_row(bb), bb.k_profiles, bb.updated_at
        else:
            return
        now = {n: bb_form(n, current, current_k) for n in compare if n != "filament_presets"}
        seen_at: datetime | None = row["bb_seen_at"]
        pushed: dict[str, str] = row["pushed_fields"] or {}
        # Decisions 78 and 81 (R-T4-31): BB rows do not carry the preset list. Under
        # ``two_way`` a link that never pushed it (a legacy link, a link seeded while
        # presets did not push, a link the pull made) does not know Bambuddy's list, so
        # it plans no preset write, and a full pass records SB's hash as pushed.
        if (
            self.sync.presets == "two_way"
            and "filament_presets" in compare
            and "filament_presets" not in pushed
        ):
            digest = field_hash(compare.pop("filament_presets"))
            if self.full:
                self.conn.execute(
                    "UPDATE filament_bb_links SET pushed_fields = %s || pushed_fields"
                    " WHERE spool_id = %s",
                    (Jsonb({"filament_presets": digest}), sb_id),
                )
        if not row["bb_seeded"] and self.full:
            # R-T5-10: a link with no full seen copy (none at all, or only the one field
            # a list ``PUT`` answered) is seeded; the field it holds stays.
            seen, seeded_k = self.seed_spool(sb_id, seen, compare, now, current, current_k)
            seen_k = seeded_k if seen_k is None else seen_k
            seen_at = max(seen_at or self.snap.read_at, self.snap.read_at)
        before = None if seen is None else {n: _seen_form(n, seen, seen_k) for n in compare}
        names = self.changed(
            compare,
            now,
            before,
            pushed,
            self.direction,
            lambda f: sb_changed_at(self.conn, sb_id, f),
            bb_at,
            seen_at,
        )
        if "archived_at" in names:
            op: Op = "archive" if desired["archived_at"] is not None else "restore"
            self.add(
                2,
                "spool",
                sb_id,
                op,
                bb_id,
                {},
                {"archived_at": field_hash(compare["archived_at"])},
            )
        if row["archived_at"] is not None:
            # An archived spool takes its archive only, whatever pushes (Decision 40,
            # R-T4-6).
            return
        fields = [n for n in names if n not in _LISTS and n != "archived_at"]
        for name, partner in _GOES_WITH.items():
            if name in fields and partner in desired and partner not in fields:
                fields.append(partner)
        if fields:
            self.add(
                1,
                "spool",
                sb_id,
                "update",
                bb_id,
                {n: body_value(n, desired[n]) for n in fields},
                {n: field_hash(compare[n]) for n in fields},
            )
        for name, list_op in _LISTS.items():
            if name in names:
                self.add(
                    3,
                    "spool",
                    sb_id,
                    list_op,
                    bb_id,
                    cast("list[Any]", body_value(name, desired[name])),
                    {name: field_hash(compare[name])},
                )

    def seed_spool(
        self,
        sb_id: UUID,
        old: Mapping[str, Any] | None,
        compare: Mapping[str, Any],
        now: Mapping[str, Any],
        current: Mapping[str, Any],
        current_k: Any,
    ) -> tuple[dict[str, Any], Any]:
        """R-T4-22: a link with no full seen copy (``bb_seeded`` false) takes Bambuddy's row
        from this full snapshot as its seen copy, at ``snap.read_at`` (or the copy's later
        time), in the fields whose value is SB's (the K-profiles too when they agree),
        over ``old`` (what a list ``PUT``'s answer stored, R-T5-10). A field that differs
        stays out of it, so it is `UNSEEN` and no push goes blind."""
        seen = {
            n: current[n]
            for n, v in compare.items()
            if n in current and n in now and now[n] == v and n != "k_profiles"
        }
        merged = dict(old or {}) | seen
        agree_k = "k_profiles" in now and now["k_profiles"] == compare["k_profiles"]
        seen_k = json_form([k.model_dump() for k in current_k or ()]) if agree_k else None
        self.conn.execute(
            "UPDATE filament_bb_links SET bb_seen = %s,"
            " bb_seen_at = greatest(bb_seen_at, %s), bb_seeded = true,"
            " bb_k_profiles = coalesce(bb_k_profiles, %s)"
            " WHERE spool_id = %s AND NOT bb_seeded",
            (Jsonb(merged), self.snap.read_at, None if seen_k is None else Jsonb(seen_k), sb_id),
        )
        return merged, seen_k

    def direction(self, field: str) -> SyncDirection:
        """A spool field's direction: its data type's (Decision 14)."""
        way: SyncDirection = getattr(self.sync, _FIELD_TYPE.get(field, "spools"))
        return way

    def new_spool(
        self, sb_id: UUID, unlinked_bb: list[BbSpool], raw: Mapping[Any, dict[str, Any]]
    ) -> None:
        if self.refused("spool", sb_id, create=False):
            return
        state = self.intent_state("spool", sb_id)
        if state == "young":
            # A create whose answer this pass lost: the next pass reads Bambuddy again.
            self.deferred.add(("spool", sb_id))
            return
        if self.refused("spool", sb_id, create=True):
            return
        if state == "old" and not self.snap.spools_complete:
            # A create whose answer was lost is adopted only from a whole listing, and a
            # partial one does not clear in a later round (R-T4-1).
            self.blocked.add(("spool", sb_id))
            return
        desired = self.desired(sb_id)
        if desired is None:
            return
        compare, body = _create_form(desired)
        fields = list(compare)
        # R-T4-31: Bambuddy starts a new spool with an empty preset list.
        made = (
            {"filament_presets": field_hash(compare_form("filament_presets", []))}
            if "filament_presets" in desired
            else {}
        )
        self.creating.add(("spool", sb_id))
        if state == "old":
            found = self.adoptable(
                body,
                self.sent.get(("spool", sb_id)),
                {n: field_hash(v) for n, v in compare.items()},
                unlinked_bb,
                raw,
                self.intents["spool", sb_id],
            )
            if found is not None:
                unlinked_bb.remove(found)
                current = seen_row(found)
                diff = [n for n in fields if compare[n] != current.get(n)]
                # R-T4-35: under ``two_way`` a field Bambuddy no longer holds as SB's lost
                # create sent it is a person's later edit; it is not sent, and its hash
                # stays out, so `results` leaves it unseen. A weight held back holds its
                # lock back too, and the other way round (`_GOES_WITH`).
                sent = self.sent.get(("spool", sb_id)) or {}
                held = {
                    n
                    for n in diff
                    if self.direction(n) == "two_way" and sent.get(n) != field_hash(current.get(n))
                }
                for name, partner in _GOES_WITH.items():
                    if name in held or partner in held:
                        held |= {name, partner} & set(fields)
                diff = [n for n in diff if n not in held]
                for name, partner in _GOES_WITH.items():
                    if name in diff and partner in fields and partner not in diff:
                        diff.append(partner)
                self.add(
                    1,
                    "spool",
                    sb_id,
                    "update",
                    found.id,
                    {n: body_value(n, desired[n]) for n in diff},
                    {n: field_hash(v) for n, v in compare.items() if n not in held} | made,
                )
                return
            if self.full:
                # R-T4-20: a full pass newer than the intent found no row it made.
                self.expire("spool", sb_id)
        hashes = {n: field_hash(compare[n]) for n in body} | made
        self.add(1, "spool", sb_id, "create", None, body, hashes, intent=True)

    @staticmethod
    def adoptable(
        body: Mapping[str, Any],
        sent: Mapping[str, str] | None,
        now: Mapping[str, str],
        unlinked_bb: Sequence[BbSpool],
        raw: Mapping[Any, dict[str, Any]],
        started: datetime,
    ) -> BbSpool | None:
        """The unlinked BB spool a lost create made (Decision 10), made after the intent
        started (with the clocks' skew) when Bambuddy says when. It holds what the create
        sent (``sent``, its field hashes) in the fields `snapshot` reads, so an SB edit
        made since does not hide it (R-T5-6). A field may also hold SB's value now (its
        hash in ``now``): what an adoption sent before its own answer was lost (R-T5-9).
        An intent with no ``sent`` matches the
        `snapshot` values of the current create ``body`` instead, tags aside (SB sends
        none)."""
        want = _adopt_form(BbSpool.model_validate({"id": 0, **body}))
        checked = None if sent is None else _ADOPT_FIELDS & sent.keys()
        for bb in unlinked_bb:
            made = (raw.get(bb.id) or {}).get("created_at")
            if isinstance(made, str):
                at = as_utc(datetime.fromisoformat(made))
                if at < started - _CLOCK_SKEW:
                    continue
            if checked is not None and sent is not None:
                held = seen_row(bb)
                if all(field_hash(held.get(n)) in (sent[n], now.get(n)) for n in checked):
                    return bb
            elif _adopt_form(bb) == want:
                return bb
        return None

    # -- the round's writes -----------------------------------------------------------

    def take(self, limit: int) -> list[BbWrite]:
        """The writes in Decision 10's order, at most ``limit``: an item whose write does
        not fit counts ``deferred``, except a retry (R-T4-15: an item Bambuddy refused in
        an earlier pass), which the cap cuts without setting ``more``; a kept create (or
        adoption) records its intent."""
        writes: list[BbWrite] = []
        for phase in (0, 1, 2, 3, 4):
            for candidate in self.phases[phase]:
                write = candidate.write
                if len(writes) >= limit:
                    if phase != _RETRY:
                        self.deferred.add((write.kind, write.sb_id))
                    continue
                writes.append(write)
                if candidate.intent:
                    # R-T4-35: what the create sent, for the adoption of a lost answer.
                    self.conn.execute(
                        "INSERT INTO filament_bb_creates (kind, sb_id, sent) VALUES (%s, %s, %s)"
                        " ON CONFLICT (kind, sb_id) DO UPDATE"
                        " SET started_at = now(), sent = EXCLUDED.sent",
                        (write.kind, write.sb_id, Jsonb(write.hashes)),
                    )
        return writes


def _create_form(desired: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """A new spool's compare values and create body from its desired values: what
    `_Planner.new_spool` compares and sends, and so what `claim_form` gives the pull."""
    fields = [n for n in desired if n not in _LISTS and n != "archived_at"]
    compare = {n: compare_form(n, desired[n]) for n in fields}
    body = {n: body_value(n, desired[n]) for n in fields if n not in _NOT_ON_CREATE}
    return compare, body


def claim_form(desired: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """An unlinked spool's create body and SB's field hashes now, from its desired values
    with no `Pending` left: what `_Planner.new_spool` sends and compares (`_create_form`).
    The pull (Task 9) uses them to find the BB spool an open intent can claim (`claims`)."""
    compare, body = _create_form(desired)
    return body, {n: field_hash(v) for n, v in compare.items()}


def claims(
    bb: BbSpool,
    body: Mapping[str, Any],
    sent: Mapping[str, str] | None,
    now: Mapping[str, str],
    started: datetime,
) -> bool:
    """Decision 10: whether the open intent of a create that sent ``body`` (``sent`` its
    hashes, ``now`` SB's hashes now, `claim_form`) and started at ``started`` can adopt
    the unlinked BB spool ``bb``, by `_Planner.adoptable`'s rule."""
    made = None if bb.created_at is None else {"created_at": bb.created_at.isoformat()}
    raw = {} if made is None else {bb.id: made}
    return _Planner.adoptable(body, sent, now, [bb], raw, started) is not None


def _seen_form(name: str, seen: Mapping[str, Any], k_profiles: Any) -> Any:
    """What SB last saw of ``name`` in a seen copy: `UNSEEN` for a field a seeded copy left
    out (R-T4-22), else `bb_form`."""
    if name != "k_profiles" and name not in seen:
        return UNSEEN
    return bb_form(name, seen, k_profiles)


def _row_form(name: str, value: Any) -> Any:
    """A BB row's value as the plan compares it: a hex colour upper case."""
    if name == "color_hex" and isinstance(value, str):
        return value.upper()
    return value


def row_seen(row: Any, readable: frozenset[str]) -> dict[str, Any]:
    """A Bambuddy supplier, location, catalog or filament row's ``readable`` fields in
    the compare form: what the plan compares, and what ``results`` stores as its seen
    copy (R-T4-11)."""
    data = row.model_dump()
    return {k: _row_form(k, json_form(data.get(k))) for k in readable}


#: The spool fields, by BB's names, behind the `snapshot` values an adoption matches
#: (tags aside): what a lost create's row must still hold as the create sent it.
_ADOPT_FIELDS = frozenset(
    {"material", "subtype", "brand", "category", "effect_type", "color_name", "rgba",
     "extra_colors", "label_weight", "cost_per_kg", "nozzle_temp_min", "nozzle_temp_max",
     "weight_locked", "weight_used", "material_number", "core_weight_catalog_id"}
)  # fmt: skip


def _adopt_form(bb: BbSpool) -> dict[str, Any]:
    return {k: v for k, v in snapshot(bb).items() if k not in ("tag_uid", "tray_uuid")}


def pendings(value: Any) -> Iterator[Pending]:
    if isinstance(value, Pending):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from pendings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from pendings(item)


# -- the items of their own -----------------------------------------------------------

#: The tables whose rows keep a seen copy (R-T4-22; filament catalog entries too,
#: R-T4-25), and whose ``bb_pushed`` holds the marks (R-T4-27, R-T4-32).
_SEEN_TABLES: dict[str, str] = {
    "supplier": "filament_suppliers",
    "location": "filament_locations",
    "insert_type": "filament_insert_types",
    "filament": "filament_spool_types",
}
_MARK_TABLES = _SEEN_TABLES
_SUPPLIER_FIELDS = frozenset({"name", "website", "customer_number", "note"})
_LOCATION_FIELDS = frozenset({"name", "identifier"})
_CATALOG_FIELDS = frozenset({"name", "weight"})
#: The filament catalog fields ``GET /filament-catalog/`` answers (`CatalogFilament`);
#: the rest of a ``FilamentCreate`` compare by hash.
_FILAMENT_FIELDS = frozenset({"name", "type", "brand", "color_hex", "density"})


def _item(row: Mapping[str, Any], bb_column: str, name: str, desired: dict[str, Any]) -> _Item:
    compare = {k: json_form(v) for k, v in desired.items()}
    return _Item(
        sb_id=row["id"],
        bb_id=row[bb_column],
        archived=row["archived_at"] is not None,
        name=name,
        desired=compare,
        body=compare,
        pushed={k: v for k, v in (row["bb_pushed"] or {}).items() if k != UNSEEN_KEY},
        unseen=(row["bb_pushed"] or {}).get(UNSEEN_KEY, {}),
        marked=UNSEEN_KEY in (row["bb_pushed"] or {}),
        seen=row["bb_seen"],
        seen_at=row["bb_seen_at"],
    )


def _suppliers(conn: Conn) -> list[_Item]:
    rows = conn.execute(
        "SELECT id, name, website, customer_number, note, archived_at,"
        " bb_supplier_id, bb_pushed, bb_seen, bb_seen_at FROM filament_suppliers"
        " ORDER BY upper(name), id"
    ).fetchall()
    return [
        _item(row, "bb_supplier_id", row["name"], {k: row[k] for k in sorted(_SUPPLIER_FIELDS)})
        for row in rows
    ]


def _locations(conn: Conn) -> list[_Item]:
    """The SB places that are no printer place (F18), each named by its path joined with
    `` / `` (Decision 14)."""
    rows = conn.execute(
        "SELECT id, name, parent_id, identifier, printer_id, archived_at,"
        " bb_location_id, bb_pushed, bb_seen, bb_seen_at FROM filament_locations"
        " ORDER BY upper(name), id"
    ).fetchall()
    by_id = {row["id"]: row for row in rows}

    def path(row: Mapping[str, Any]) -> str:
        names, seen = [row["name"]], {row["id"]}
        parent = by_id.get(row["parent_id"])
        while parent is not None and parent["id"] not in seen:
            names.append(parent["name"])
            seen.add(parent["id"])
            parent = by_id.get(parent["parent_id"])
        return " / ".join(reversed(names))

    items = []
    for row in rows:
        if is_printer_place(row):
            continue
        name = path(row)
        items.append(
            _item(row, "bb_location_id", name, {"name": name, "identifier": row["identifier"]})
        )
    return items


def _insert_types(conn: Conn) -> list[_Item]:
    rows = conn.execute(
        "SELECT id, name, empty_weight_g, archived_at, bb_catalog_id, bb_pushed,"
        " bb_seen, bb_seen_at FROM filament_insert_types ORDER BY upper(name), id"
    ).fetchall()
    return [
        _item(
            row,
            "bb_catalog_id",
            row["name"],
            {"name": row["name"], "weight": row["empty_weight_g"]},
        )
        for row in rows
    ]


def _filaments(conn: Conn) -> list[_Item]:
    """One filament catalog entry per spool type (Decision 14), named by the type's code,
    which SB keeps unique."""
    rows = conn.execute(
        "SELECT t.id, t.code, t.colour_name, t.rgba, t.density_g_cm3, t.nozzle_temp_min,"
        " t.nozzle_temp_max, t.bed_temp_min, t.bed_temp_max, t.cost_per_kg, t.archived_at,"
        " t.bb_filament_id, t.bb_pushed, t.bb_seen, t.bb_seen_at, ma.name AS material,"
        " b.name AS brand"
        " FROM filament_spool_types t JOIN filament_materials ma ON ma.id = t.material_id"
        " LEFT JOIN filament_brands b ON b.id = t.brand_id ORDER BY upper(t.code), t.id"
    ).fetchall()
    items = []
    for row in rows:
        desired: dict[str, Any] = {
            "name": row["code"],
            "type": row["material"],
            "brand": row["brand"],
            "color": row["colour_name"],
            "color_hex": row["rgba"][:7],
            "density": row["density_g_cm3"],
            "print_temp_min": row["nozzle_temp_min"],
            "print_temp_max": row["nozzle_temp_max"],
            "bed_temp_min": row["bed_temp_min"],
            "bed_temp_max": row["bed_temp_max"],
        }
        if row["cost_per_kg"] is not None:
            desired["cost_per_kg"] = row["cost_per_kg"]
        items.append(_item(row, "bb_filament_id", row["code"], desired))
    return items


@dataclass(frozen=True)
class RowKind:
    """Where a kind of row of its own keeps its link and what Bambuddy's listing of it
    carries (``readable``)."""

    table: str
    bb_column: str
    readable: frozenset[str]
    items: Callable[[Conn], list[_Item]]


#: The kinds that are rows of their own, not spools.
ROW_KINDS: dict[str, RowKind] = {
    "supplier": RowKind("filament_suppliers", "bb_supplier_id", _SUPPLIER_FIELDS, _suppliers),
    "location": RowKind("filament_locations", "bb_location_id", _LOCATION_FIELDS, _locations),
    "insert_type": RowKind(
        "filament_insert_types", "bb_catalog_id", _CATALOG_FIELDS, _insert_types
    ),
    "filament": RowKind("filament_spool_types", "bb_filament_id", _FILAMENT_FIELDS, _filaments),
}


def plan(
    store: Filament,
    snap: BbSnapshot,
    sync: FilamentSync,
    *,
    interval_s: float,
    limit: int = 200,
    cancel: CancelToken | None = None,
) -> SyncPlan:
    """The writes SB wants Bambuddy to take next, from what ``snap`` read, at most
    ``limit`` (Decision 11), in this order: suppliers, locations, insert types and
    filament catalog entries, with the probes of those rows a complete listing lacks
    (#6688); then spool creates, updates and probes whose references are known (the
    others count ``deferred`` or ``blocked``); then archive and restore; then each
    spool's lists (K-profiles, presets, suppliers); last the writes of items
    Bambuddy refused in an earlier pass (R-T4-2). An item refused in this pass, or a
    create refused earlier on an event pass, counts ``failed`` (R-T4-1). Each write's
    ``key`` is ``kind:sb_id:op``. A kept create records its intent here, in this
    transaction (Decision 10). Nothing is planned when no data type pushes, or when the
    links belong to another Bambuddy (``instance_changed``, Decision 12).

    One plan or results call of an instance's sync runs at a time (`hold_sync`, #6902):
    another is `SyncBusyError`. ``cancel`` stops the plan, which then writes nothing,
    unless it had already passed its last check, just before the commit (`core/cancel.py`).
    A plan sent again finds the intents a committed one recorded."""
    empty = SyncPlan(writes=[], deferred=0, directions=sync, interval_s=interval_s)
    if not pushes_anything(sync):
        return empty
    with store.spools.transaction() as conn, watched(cancel, conn):
        hold_sync(conn, snap.instance)
        state = conn.execute("SELECT instance FROM filament_bb_sync_state").fetchone()
        if state is not None and state["instance"] not in (None, snap.instance):
            return empty.model_copy(update={"instance_changed": True})
        creates = conn.execute("SELECT kind, sb_id, started_at, sent FROM filament_bb_creates")
        rows = creates.fetchall()
        intents = {(row["kind"], row["sb_id"]): row["started_at"] for row in rows}
        sent = {(row["kind"], row["sb_id"]): row["sent"] for row in rows}
        errors = {
            (row["kind"], row["sb_id"]): row["at"]
            for row in conn.execute(
                "SELECT kind, sb_id, at FROM filament_bb_push_errors"
            ).fetchall()
        }
        planner = _Planner(
            conn, snap, sync, BbRefs.read(conn), intents, sent, errors, limit=limit, cancel=cancel
        )
        planner.rows("supplier", _suppliers(conn), snap.suppliers, sync.suppliers, _SUPPLIER_FIELDS)
        planner.rows("location", _locations(conn), snap.locations, sync.locations, _LOCATION_FIELDS)
        planner.rows("insert_type", _insert_types(conn), snap.catalog, sync.spools, _CATALOG_FIELDS)
        planner.rows("filament", _filaments(conn), snap.filaments, sync.spools, _FILAMENT_FIELDS)
        # R-6688-5: a spool names a row Bambuddy may have deleted as a `Pending`, so it
        # waits for the row's probe (and its create again) instead of sending its old id.
        unconfirmed = planner.unconfirmed()
        planner.refs = planner.refs.without(unconfirmed)
        planner.spools()
        writes = planner.take(limit)
        # `take` records the kept creates' intents: the last step before the commit.
        check(cancel)
    return SyncPlan(
        writes=writes,
        deferred=len(planner.deferred),
        failed=len(planner.failed),
        blocked=len(planner.blocked),
        unmapped_k=planner.unmapped_k,
        conflicts=planner.conflicts,
        directions=sync,
        interval_s=interval_s,
        unconfirmed=cast(
            "list[tuple[Kind, UUID]]", sorted(unconfirmed, key=lambda k: (k[0], str(k[1])))
        ),
    )
