"""Bambuddy's spools pulled into ScadBuddy's own (#6261, spec §8.1, §8.2). The BB to SB
side only: nothing here writes to Bambuddy (SB to BB is the sync of #6265,
`bb_sync_plan.py`, `bb_sync_run.py`).

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
   the newer change wins (§8.2): a field whose SB change time (`bb_sync_plan.sb_changed_at`,
   #6265 Decision 8: its ``edited`` events, its type's ``updated_at`` for a value it
   inherits, its ledger events) is after BB's ``updated_at`` keeps SB's value. The spool's
   ``created_at``, that rule's fallback, is no SB change here: a spool the pull made took
   BB's values, and a linked one kept its own at the link. After the push's echo event
   (Decision 9, ``data.pushed``), which holds Bambuddy's whole answer, a field is "changed"
   when it moved off ``filament_bb_links.bb_seen``, so a person's Bambuddy edit of a field
   the push did not send is not hidden (R-T5-5). A value BB no longer has never clears SB's, and the
   fields BB does not have (note, location, bed temperatures ...) are never touched. A
   change of what is left (``label_weight - weight_used``) is a ``bb`` absolute reading
   on the ``synced_from_bb`` event, unless the SB spool's weight is locked: that value
   is BB's own estimate, and the lock guards against estimates (§5.5), so it is recorded
   in the event's data and never moves ``grams_left``. A BB weighing is a
   ``scale`` reading (#6263): it is physical, so it counts under the lock (`_book`).
   A BB unlock clears the lock only when a pull set it (`_lock_from_bb`).
2. Else an SB spool already is that spool: it holds the BB spool's ``tag_uid`` or
   ``tray_uuid`` through an open binding. It is linked; a spool that needs a
   type takes the one BB's values match, and one with no weight takes BB's.
3. Else a new SB spool: its type by ``material_number`` (`SpoolTypeStore.match`), else
   by identity (brand, material, subtype, colour, label weight), else none (it needs a
   type) and BB's values as its own. A spool with a type overrides only what BB says
   differently. It takes BB's tags unless another linked spool holds them, a
   ``created`` event with a ``bb`` reading of what is left, and its link. A BB spool whose
   tag keys an SB spool holds through an open binding (override or not), where that spool
   is already linked to another BB spool, is one Bambuddy added itself for the tag (#6264):
   it makes no SB spool, and is counted in ``PullReport.rfid_duplicates``. It is remembered
   (``filament_bb_ignored``), so it stays skipped after the push cleared its tag. A tag with
   a tray UUID binds as ``bambu_mifare``; one BB read as OpenTag3D
   (``data_origin == "opentag3d"``) as ``ntag_opentag``; any other as ``other``.
   A BB spool Bambuddy archived (#6707) is made archived (`_create_archived`): BB's
   ``archived_at``, what is left as BB says (``0`` with no label weight), no tag, an
   ``archived`` event, and none of BB's weighings, dryings or presets. It is counted as
   ``created`` and as ``archived_created``. Its BB usage rows are booked, the ones before
   its reading too (`bambuddy_usage.py`).

The pull reads BB's archived spools too (#6707). An archived BB spool takes the steps above
as any other, and one whose tag an SB spool holds is linked or skipped as above. Under
``pulls.spools`` a linked spool takes BB's archive state (#6265 Task 9): BB archiving it
archives the SB spool, and BB restoring it restores it, when BB's state moved off what SB
last saw (``bb_seen``; a restore needs a seen archive) and the SB spool's newest
``archived`` or ``restored`` event is not after BB's change. An archived SB spool takes
nothing else.
BB's live spools are pulled first: of a live and an archived BB record with one tag, the
live one is linked or made, and the archived one is its RFID duplicate (R-6707-13). When
BB's usage rows could not be read, an archived BB spool is not made in that pull; it waits
for one that has them (``archived_deferred``, R-6707-16).

Catalog names from BB (material, subtype, brand, category, effect) resolve by their key
against every name and alias of the catalog (#6706, `catalog.find_by_key`: `DUAL-COLOR` is
`Dual Color`), and are added when missing. A spool's type by identity resolves them the
same way (`SpoolTypeStore.match`, R-6706-19). BB's spool catalog (``GET /inventory/catalog``,
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
take that time; with none sent, the pull's). BB's usage rows (`bambuddy_usage.py`) sit at
their ``created_at``, the same instant as the deduction, and at one instant a delta comes
before a reading, so a BB reading holds its own usage row and that plate is not taken off
again. SB's own plates pair with BB's usage rows by job and count once (§8.2,
"Consumption is counted once"; the rules are `ledger.py`'s).

#6265 Task 9 widens the pull. Every pull writes a link's ``bb_seen`` from the row it read
(`seen_row`), merged by R-T4-28: a field the pull took, or whose BB value is SB's compare
value, goes in; any other keeps its old seen value or stays out. A BB spool an open create
intent can claim (Decision 10) is the push's to adopt: it is skipped (``claimed``). Under
``pulls.presets`` BB's K-profiles become the spool's own K values (`_bb_k_values`). Under
``pulls.suppliers`` and ``pulls.locations`` BB's suppliers and locations become SB's rows
(`_pull_rows`; spool supplier links and prices never, Decision 15), and a linked spool
follows BB's location (`_follow_location`), never out of a printer's place (F18).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.errors import BAD_BODY_PROBLEM
from scadbuddy.bambuddy.linking import ARCHIVE_OVERLAP, ARCHIVE_PAGE, MAX_ARCHIVE_PAGES
from scadbuddy.bambuddy.models import (
    Archive,
    BbLocation,
    BbSupplier,
    SpoolAssignment,
    SpoolCatalogEntry,
    SpoolKProfile,
    SpoolUsage,
    as_utc,
)
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.core import settings as core_settings
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import bambuddy_usage, ledger, sensors
from scadbuddy.filament import models as m
from scadbuddy.filament.bb_sync_models import BbLinked, json_form, seen_row
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.filament.catalog import KIND_OF_TABLE, lookup_key, resolve_or_add
from scadbuddy.filament.colour import canonical_colours, canonical_rgba
from scadbuddy.filament.component import Filament
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.presets import drop_migrated, effective_k_values, own_k_values, own_presets
from scadbuddy.filament.spools import FilamentSpoolStore, TagKeysClashError
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.operations.kinds import waiting_on_bambuddy
from scadbuddy.spools.models import clean_tag_uid, clean_tray_uuid

if TYPE_CHECKING:
    # `bb_sync_plan` reads this module: its names are imported where they are used.
    from scadbuddy.filament.bb_sync_plan import BbRefs

logger = logging.getLogger(__name__)

Conn = Connection[DictRow]

#: Who the pull's events name.
ACTOR = "bambuddy"
#: The ``ref_kind`` of the pull's events; the ``ref_id`` is the BB spool id, with BB's
#: ``updated_at`` on a ``synced_from_bb`` event, so a retried pull books nothing twice.
REF_KIND = ledger.BB_SPOOL
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
#: The snapshot fields of a data type other than ``spools`` (#6265 Decision 14): each is
#: taken, and recorded as seen, only while its type pulls.
_TYPE_FIELDS: dict[str, str] = {
    "remaining_g": "weights",
    "weight_locked": "weights",
    "tag_uid": "tags",
    "tray_uuid": "tags",
}
#: The `snapshot` fields by their `seen_row` name (#6265 Task 9); the tags have none.
_SEEN_NAMES: dict[str, str] = {
    "material": "material",
    "subtype": "subtype",
    "brand": "brand",
    "category": "category",
    "effect": "effect_type",
    "colour_name": "color_name",
    "rgba": "rgba",
    "extra_colours": "extra_colors",
    "label_weight_g": "label_weight",
    "cost_per_kg": "cost_per_kg",
    "nozzle_temp_min": "nozzle_temp_min",
    "nozzle_temp_max": "nozzle_temp_max",
    "weight_locked": "weight_locked",
    "remaining_g": "weight_used",
    "material_number": "material_number",
    "core_weight_catalog_id": "core_weight_catalog_id",
}
#: The SB spool columns the pull writes, by the `seen_row` name of the BB field it took.
_COLUMN_SEEN: dict[str, str] = {
    "material_id": "material",
    "subtype_id": "subtype",
    "brand_id": "brand",
    "category_id": "category",
    "effect_id": "effect_type",
    "colour_name": "color_name",
    "rgba": "rgba",
    "extra_colours": "extra_colors",
    "label_weight_g": "label_weight",
    "cost_per_kg": "cost_per_kg",
    "nozzle_temp_min": "nozzle_temp_min",
    "nozzle_temp_max": "nozzle_temp_max",
    "weight_locked": "weight_locked",
    "type_id": "material_number",
    "insert_type_id": "core_weight_catalog_id",
}
#: The catalog entries already warned about as skipped, so a clash is told once a process.
_WARNED_SKIPS: set[tuple[int, str]] = set()
_WARNED_SKIPS_MAX = 1000
#: The BB spools already warned about as duplicates, so each is told once a process.
_WARNED_DUPLICATES: set[int] = set()

Outcome = Literal["pulled", "skipped", "failed"]


@dataclass(frozen=True)
class Pulls:
    """Which data types a pull takes from Bambuddy (#6265, `FilamentSync`). ``spools``: a
    linked spool's own fields, new SB spools (made or linked) and the spool catalog; with
    it False an unlinked BB spool is left alone. ``weights``: ``bb`` readings and scale
    weighings on a spool SB already tracks (a new spool keeps its first reading, #6268).
    ``weight_locked`` and BB's last drying are ``weights`` data too (Decision 14).
    ``tags``: a linked spool's binding to a tag BB names (a new spool takes its tags
    whatever this says, Decision 6). ``presets``: BB's ``slicer_filament`` as the spool's
    ``migrated`` presets, and its K-profiles as the spool's own K values. ``suppliers``
    and ``locations``: BB's supplier and location rows, whatever ``spools`` says
    (Decision 16); with ``locations`` a linked spool follows BB's location. A child type
    acts only on a linked spool (Decision 16).

    BB's values are recorded as seen only for the types that pull, so a type switched on
    later takes what BB changed while it was off, even when BB's row has not moved since.
    The defaults are `FilamentSync`'s: ``suppliers`` and ``locations`` are off."""

    def of_field(self, field: str) -> bool:
        """True when the snapshot field ``field`` is of a data type this pull takes."""
        return bool(getattr(self, _TYPE_FIELDS.get(field, "spools")))

    spools: bool = True
    weights: bool = True
    tags: bool = True
    presets: bool = True
    suppliers: bool = False
    locations: bool = False

    @classmethod
    def of(cls, sync: FilamentSync) -> Pulls:
        return cls(
            spools=core_settings.pulls(sync.spools),
            weights=core_settings.pulls(sync.weights),
            tags=core_settings.pulls(sync.tags),
            presets=core_settings.pulls(sync.presets),
            suppliers=core_settings.pulls(sync.suppliers),
            locations=core_settings.pulls(sync.locations),
        )


#: The data types `FilamentSync`'s defaults pull: the default of `pull` and
#: `read_and_pull`.
EVERY = Pulls()


class PullReport(BaseModel):
    """What one pull did: how many SB spools it made, linked and changed, how many of
    those it made or linked still need a type, and how many it left alone because they
    are archived, with their ids; the BB spools it could not pull (each logged and rolled
    back); and the BB rows it could not read (each logged), with their ids when the row
    had one; whether BB's spool catalog failed (all or nothing: then no entry was mapped),
    and how many of its entries were skipped (no name or weight, or a name another type
    has), so a spool naming one gets no insert type; and the BB spools it left alone
    because they are Bambuddy's copy of a tag a linked SB spool holds
    (``rfid_duplicates``, with their ids); and how many catalog names it skipped because
    they have no letter or digit (``names_skipped``). ``archived_created`` counts the
    spools it made archived because Bambuddy archived them (#6707); they are in
    ``created`` too. ``archived_deferred`` counts the archived BB spools it did not make
    because BB's usage rows could not be read (R-6707-16), with their ids: the next pull
    that reads them makes those spools. ``claimed`` counts the BB spools, suppliers and
    locations left to an open create intent (Decision 10). ``suppliers`` and
    ``locations`` count the SB rows the pull made, linked or changed; ``moved`` the
    linked spools it moved to BB's location; and ``conflicts`` the supplier and location
    fields both sides changed, which it left as SB holds them (R-T4-33)."""

    created: int = 0
    archived_created: int = 0
    linked: int = 0
    updated: int = 0
    needs_type: int = 0
    archived: int = 0
    invalid: int = 0
    #: BB's spool catalog could not be read or mapped; the insert types were left alone.
    catalog_failed: bool = False
    catalog_skipped: int = 0
    #: Catalog names (material, subtype, brand, category, effect) with no letter or digit,
    #: so no key: each field was left as if BB sent none, and its spool pulled (R-6706-18).
    names_skipped: int = 0
    created_ids: list[UUID] = Field(default_factory=list)
    linked_ids: list[UUID] = Field(default_factory=list)
    updated_ids: list[UUID] = Field(default_factory=list)
    needs_type_ids: list[UUID] = Field(default_factory=list)
    archived_ids: list[UUID] = Field(default_factory=list)
    failed_bb_ids: list[int] = Field(default_factory=list)
    invalid_bb_ids: list[int] = Field(default_factory=list)
    #: BB's usage rows the pull booked as print ends, and those of a BB spool no SB
    #: spool is linked to (`bambuddy_usage.py`).
    usage_booked: int = 0
    usage_unlinked: int = 0
    #: BB spools made for a tag a linked SB spool holds: no SB spool is made.
    rfid_duplicates: int = 0
    rfid_duplicate_bb_ids: list[int] = Field(default_factory=list)
    #: Archived BB spools not made yet because the usage rows could not be read.
    archived_deferred: int = 0
    archived_deferred_bb_ids: list[int] = Field(default_factory=list)
    #: #6265 Task 9.
    claimed: int = 0
    suppliers: int = 0
    locations: int = 0
    moved: int = 0
    conflicts: int = 0


def _text(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


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
        "rgba": canonical_rgba(bb.rgba),
        "extra_colours": canonical_colours(bb.extra_colors),
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


#: Some SB row or spool is linked to a Bambuddy row (Decision 12).
ANY_LINK = (
    "EXISTS (SELECT 1 FROM filament_bb_links)"
    " OR EXISTS (SELECT 1 FROM filament_suppliers WHERE bb_supplier_id IS NOT NULL)"
    " OR EXISTS (SELECT 1 FROM filament_locations WHERE bb_location_id IS NOT NULL)"
    " OR EXISTS (SELECT 1 FROM filament_insert_types WHERE bb_catalog_id IS NOT NULL)"
    " OR EXISTS (SELECT 1 FROM filament_spool_types WHERE bb_filament_id IS NOT NULL)"
)


def record_instance(conn: Conn, instance: str | None) -> None:
    """Decision 12 (review I2): when no instance is recorded and a link exists, the links
    belong to ``instance``, the Bambuddy they were made against. Run in the transaction
    that made the link, so no link is committed without it. ``None`` (a caller that does
    not know the Bambuddy) records nothing."""
    if instance is not None:
        conn.execute(
            "UPDATE filament_bb_sync_state SET instance = %s"
            f" WHERE instance IS NULL AND ({ANY_LINK})",
            (instance,),
        )


def pull(
    store: Filament,
    spools: Sequence[BbSpool],
    *,
    now: datetime,
    actor: str = ACTOR,
    catalog: Sequence[SpoolCatalogEntry] = (),
    pulls: Pulls = EVERY,
    usage: Sequence[SpoolUsage] = (),
    defer_archived: bool = False,
    suppliers: Sequence[BbSupplier] | None = None,
    locations: Sequence[BbLocation] | None = None,
    instance: str | None = None,
) -> PullReport:
    """Pull ``spools`` (BB's ``GET /inventory/spools``) into ``store``, as the module
    says, after ``catalog`` (BB's spool catalog) is mapped to insert types, and BB's
    ``suppliers`` and ``locations`` (``None``: not read) to SB's rows under ``pulls``.
    ``now`` is the link's time, its ``bb_updated_at`` when BB sends none, and its
    ``bb_seen_at``; ``actor`` is who the events name (a person's import, else
    ``bambuddy``); ``pulls`` the data types it takes. One BB spool that fails is rolled
    back, logged and listed; the rest go on, as when the catalog, the suppliers or the
    locations fail.

    BB's live spools are pulled before its archived ones, so of two BB records with one
    tag the live one is linked or made, and the archived one is its RFID duplicate,
    whatever BB's order (R-6707-13). ``usage`` is BB's usage rows: a spool made archived
    takes its reading no earlier than its newest row (`_create_archived`). With
    ``defer_archived`` (BB's usage rows could not be read) no archived spool is made: its
    reading could not be timed after its rows, so one would be taken off twice. It waits
    for a pull that has them (R-6707-16).

    ``instance`` is the Bambuddy read (`bb_sync_nudge.instance_for`): each transaction
    that links a row or a spool records it as the links' instance when none is
    (`record_instance`, Decision 12)."""
    report = PullReport()
    newest: dict[int, datetime] = {}
    for row in usage:
        if row.weight_used > 0:
            newest[row.spool_id] = max(row.created_at, newest.get(row.spool_id, row.created_at))
    try:
        report.catalog_skipped = _insert_types(store, catalog if pulls.spools else (), instance)
    except Exception:
        logger.warning("could not map Bambuddy's spool catalog", exc_info=True)
        report.catalog_failed = True
    row_kinds: tuple[tuple[Literal["supplier", "location"], Any, bool], ...] = (
        ("supplier", suppliers, pulls.suppliers),
        ("location", locations, pulls.locations),
    )
    for kind, rows, wanted in row_kinds:
        if rows is None or not wanted:
            continue
        try:
            changed, conflicts, claimed = _pull_rows(store, kind, rows, now, instance)
        except Exception:
            logger.warning("could not pull Bambuddy's %ss", kind, exc_info=True)
            continue
        setattr(report, f"{kind}s", changed)
        report.conflicts += conflicts
        report.claimed += claimed
    run = _Run(store, now, actor, pulls, defer_archived, *_context(store, pulls))
    for bb in sorted(spools, key=lambda s: s.archived_at is not None):
        try:
            with store.spools.transaction() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK, bb.id))
                skipped: list[str] = []
                outcome = _one(conn, run, bb, skipped, newest.get(bb.id))
                record_instance(conn, instance)
        except Exception as error:
            logger.warning(
                "could not pull a Bambuddy spool",
                extra={"bb_spool_id": bb.id, "error": type(error).__name__},
                exc_info=True,
            )
            report.failed_bb_ids.append(bb.id)
            continue
        if skipped:
            logger.info(
                "skipped Bambuddy catalog names with no letter or digit",
                extra={"bb_spool_id": bb.id, "fields": skipped},
            )
            report.names_skipped += len(skipped)
        if outcome is None:
            continue
        what, spool_id, typeless = outcome
        if what == "duplicate":
            report.rfid_duplicate_bb_ids.append(bb.id)
            continue
        if what == "archived_deferred":
            report.archived_deferred_bb_ids.append(bb.id)
            continue
        if what == "claimed":
            report.claimed += 1
            continue
        assert spool_id is not None  # only a skipped duplicate has none
        if what == "archived_created":
            report.archived_created += 1
            what = "created"
        getattr(report, f"{what}_ids").append(spool_id)
        if typeless:
            report.needs_type_ids.append(spool_id)
    for what in ("created", "linked", "updated", "needs_type", "archived"):
        setattr(report, what, len(getattr(report, f"{what}_ids")))
    report.rfid_duplicates = len(report.rfid_duplicate_bb_ids)
    report.archived_deferred = len(report.archived_deferred_bb_ids)
    report.moved = run.moved
    return report


@dataclass
class _Run:
    """One pull's settings and what it read of SB before the spools: the Bambuddy ids of
    SB's rows (`bb_sync_plan.BbRefs`, for the seen copy's compare values) and the open
    spool create intents (`_claims`)."""

    store: Filament
    now: datetime
    actor: str
    pulls: Pulls
    defer_archived: bool
    refs: BbRefs
    claims: list[_Claim]
    moved: int = 0

    def claimed(self, bb: BbSpool) -> bool:
        """Decision 10: an open spool create intent can adopt ``bb``."""
        from scadbuddy.filament.bb_sync_plan import claims

        return any(claims(bb, c.body, c.sent, c.now, c.started) for c in self.claims)


@dataclass(frozen=True)
class _Claim:
    """An open spool create intent (Decision 10), as the plan adopts with it: the create's
    body, its sent hashes, SB's hashes now and when it started (`bb_sync_plan.claim_form`)."""

    body: dict[str, Any]
    sent: dict[str, str] | None
    now: dict[str, str]
    started: datetime


def _context(store: Filament, pulls: Pulls) -> tuple[BbRefs, list[_Claim]]:
    """`_Run`'s reads of SB: the refs once the rows and insert types are mapped, and the
    claims of the open spool intents (only under ``pulls.spools``: nothing else makes a
    spool)."""
    # Here, not at the top: `bb_sync_plan` reads this module.
    from scadbuddy.filament import bb_sync_plan as plan

    with store.spools.transaction() as conn:
        refs = plan.BbRefs.read(conn)
        claims: list[_Claim] = []
        if not pulls.spools:
            return refs, claims
        rows = conn.execute(
            "SELECT c.sb_id, c.started_at, c.sent FROM filament_bb_creates c"
            " JOIN filament_spools s ON s.id = c.sb_id WHERE c.kind = 'spool'"
            " AND NOT EXISTS (SELECT 1 FROM filament_bb_links l WHERE l.spool_id = c.sb_id)"
        ).fetchall()
        for row in rows:
            desired, _ = plan.desired_and_unmapped(conn, row["sb_id"], refs, _ALL)
            known = {n: v for n, v in desired.items() if next(plan.pendings(v), None) is None}
            body, now = plan.claim_form(known)
            claims.append(_Claim(body, row["sent"], now, row["started_at"]))
    return refs, claims


#: Every data type both ways: the compare values of every field a seen copy holds.
_ALL = FilamentSync(
    spools="two_way",
    weights="two_way",
    tags="two_way",
    presets="two_way",
    suppliers="two_way",
    locations="two_way",
)


def _insert_types(
    store: Filament, catalog: Sequence[SpoolCatalogEntry], instance: str | None = None
) -> int:
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
        record_instance(conn, instance)
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
    conn: Conn,
    run: _Run,
    bb: BbSpool,
    skipped: list[str] | None = None,
    newest_use: datetime | None = None,
) -> tuple[str, UUID | None, bool] | None:
    """What came of one BB spool. A linked spool takes BB's archive state under
    ``pulls.spools`` (`_archive_state`); an archived SB spool, linked or named by the BB
    record's tag, then takes nothing else (``archived``). With ``pulls.spools`` False an
    unlinked BB spool is left alone (``None``), and so is one an open create intent can
    claim (``claimed``, Decision 10). ``newest_use`` is the time of BB's newest usage row
    of it, if any; with ``run.defer_archived`` an archived BB spool that would be made is
    not (``archived_deferred``). Every link the BB spool has after this gets its seen
    copy (`_write_seen`). A linked row strictly older than the link's ``bb_updated_at``
    (read before a push's results recorded a newer one) is left alone (review M1)."""
    store, now, actor, pulls = run.store, run.now, run.actor, run.pulls
    link = conn.execute(
        "SELECT l.spool_id, l.bb_updated_at, s.archived_at IS NOT NULL AS archived"
        " FROM filament_bb_links l JOIN filament_spools s ON s.id = l.spool_id"
        " WHERE l.bb_spool_id = %s",
        (bb.id,),
    ).fetchone()
    if link is not None:
        spool_id: UUID = link["spool_id"]
        if bb.updated_at is not None and as_utc(bb.updated_at) < link["bb_updated_at"]:
            return None
        taken: set[str] = set()
        if link["archived"]:
            restored = pulls.spools and _archive_state(conn, run, spool_id, bb, archived=True)
            if not restored:
                _write_seen(conn, run, spool_id, bb, taken)
                return "archived", spool_id, False
            taken.add("archived_at")
        # The move and the archive first, before this pull books any event at its own
        # time, so `_bb_event_at` books them at Bambuddy's (#6265 decision 158).
        moved = pulls.locations and _follow_location(conn, run, spool_id, bb, taken)
        archived = (
            not link["archived"]
            and pulls.spools
            and _archive_state(conn, run, spool_id, bb, archived=False)
        )
        if archived:
            taken.add("archived_at")
        changed = _update(
            conn, run, bb, spool_id, link["bb_updated_at"], skipped, taken, archived=archived
        )
        # BB may not move ``updated_at`` for a weighing: its events are booked anyway,
        # once each.
        booked = _book(conn, spool_id, bb, actor, pulls.weights)
        presets = pulls.presets and _bb_presets(conn, spool_id, bb, actor)
        k_values = pulls.presets and _bb_k_values(conn, run, spool_id, bb)
        _write_seen(conn, run, spool_id, bb, taken)
        if link["archived"] or changed or booked or presets or k_values or moved or archived:
            return "updated", spool_id, False
        return None
    if not pulls.spools:
        return None
    if run.claimed(bb):
        return "claimed", None, False
    after = snapshot(bb)
    held = _holder(conn, after, bb.id)
    if held is not None:
        held_id, archived_holder = held
        if archived_holder:
            return "archived", held_id, False
        typeless = _link(conn, store, bb, held_id, after, now, actor, pulls)
        _book(conn, held_id, bb, actor, pulls.weights)
        if pulls.presets:
            _bb_presets(conn, held_id, bb, actor)
            _bb_k_values(conn, run, held_id, bb)
        _write_seen(conn, run, held_id, bb, set())
        return "linked", held_id, typeless
    linked_holder = _linked_holder(conn, after)
    if linked_holder is not None:
        if len(_WARNED_DUPLICATES) > _WARNED_SKIPS_MAX:
            _WARNED_DUPLICATES.clear()
        log = logger.debug if bb.id in _WARNED_DUPLICATES else logger.warning
        _WARNED_DUPLICATES.add(bb.id)
        log(
            "a Bambuddy spool made for a tag a linked spool holds is left alone",
            extra={"bb_spool_id": bb.id, "holder": str(linked_holder)},
        )
        conn.execute(
            "INSERT INTO filament_bb_ignored (bb_spool_id, reason, tag_uid, tray_uuid)"
            " VALUES (%s, 'rfid_duplicate', %s, %s) ON CONFLICT (bb_spool_id) DO NOTHING",
            (bb.id, after["tag_uid"], after["tray_uuid"]),
        )
        return "duplicate", linked_holder, False
    ignored = conn.execute(
        "SELECT 1 FROM filament_bb_ignored WHERE bb_spool_id = %s", (bb.id,)
    ).fetchone()
    if ignored is not None:
        # Skipped as a duplicate once; the push has since cleared its tag, so no holder
        # shows, but it is still the spool Bambuddy added for a tag SB holds.
        return "duplicate", None, False
    if bb.archived_at is not None:
        if run.defer_archived:
            return "archived_deferred", None, False
        # R-T9-2: no weighing, drying, preset or K value.
        made, typeless = _create_archived(conn, store, bb, after, now, actor, newest_use, skipped)
        _write_seen(conn, run, made, bb, set())
        return "archived_created", made, typeless
    made, typeless = _create(conn, store, bb, after, now, actor, skipped)
    _book(conn, made, bb, actor, pulls.weights)
    if pulls.presets:
        _bb_presets(conn, made, bb, actor)
        _bb_k_values(conn, run, made, bb)
    if pulls.locations:
        _follow_location(conn, run, made, bb, set(), new=True)
    _write_seen(conn, run, made, bb, set())
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


def _linked_holder(conn: Conn, after: dict[str, Any]) -> UUID | None:
    """The unarchived SB spool, linked to another BB spool, that holds the BB spool's tag
    keys through an open binding (override or not): Bambuddy added this BB spool itself
    for that tag (#6264), and a second SB spool for one physical spool is never right."""
    uid, tray = after["tag_uid"], after["tray_uuid"]
    if uid is None and tray is None:
        return None
    row = conn.execute(
        "SELECT b.spool_id FROM filament_tag_bindings b JOIN filament_tags t ON t.id = b.tag_id"
        " JOIN filament_spools s ON s.id = b.spool_id"
        " WHERE b.unbound_at IS NULL AND s.archived_at IS NULL"
        " AND (t.tag_uid = %s OR t.tray_uuid = %s)"
        " AND EXISTS (SELECT 1 FROM filament_bb_links l WHERE l.spool_id = b.spool_id)"
        " ORDER BY b.bound_at, b.id LIMIT 1",
        (uid, tray),
    ).fetchone()
    return None if row is None else row["spool_id"]


def _link(
    conn: Conn,
    store: Filament,
    bb: BbSpool,
    spool_id: UUID,
    after: dict[str, Any],
    now: datetime,
    actor: str,
    pulls: Pulls,
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
    if not pulls.weights or current["grams_left"] is not None or current["weight_locked"]:
        reading = None
    _insert_link(conn, spool_id, bb, now)
    _synced(conn, spool_id, bb, _seen(after, {}, pulls), changes, reading, actor, linked=True)
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
    conn: Conn,
    store: Filament,
    bb: BbSpool,
    after: dict[str, Any],
    now: datetime,
    actor: str,
    skipped: list[str] | None = None,
    *,
    archived: bool = False,
) -> tuple[UUID, bool]:
    """A new SB spool for ``bb``. ``archived``: what is left is a reading even with no label
    weight (``0``), and BB's tag is not bound (an archived spool holds none)."""
    kind = _match(conn, store, after)
    values = _values(conn, after, after.keys() - _NOT_COLUMNS, skipped=skipped)
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
    if archived and reading is None:
        reading = 0.0
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
    if not archived:
        _bind(conn, spool_id, after, bb.data_origin, actor)
    _insert_link(conn, spool_id, bb, now)
    # A lock BB set is BB's to clear (`_lock_from_bb`).
    locked = {"weight_locked": [False, True]} if values.get("weight_locked") else {}
    _synced(conn, spool_id, bb, after, locked, None, actor)
    FilamentSpoolStore.read(conn, spool_id, check=True)
    return spool_id, kind is None


def _create_archived(
    conn: Conn,
    store: Filament,
    bb: BbSpool,
    after: dict[str, Any],
    now: datetime,
    actor: str,
    newest_use: datetime | None = None,
    skipped: list[str] | None = None,
) -> tuple[UUID, bool]:
    """A BB spool Bambuddy archived before any pull linked it (#6707): a new SB spool as
    `_create` makes one, archived at BB's ``archived_at`` with an ``archived`` event then.
    Its ``data.bb_archived_at`` marks it as one the pull made archived, whose usage rows
    are booked (`bambuddy_usage._links`). The event is never earlier than the ``created``
    one, so the history reads in order when BB moved ``updated_at`` after the archive.
    No weighing, drying or preset is taken: the pull never writes to an archived spool.

    BB may not move ``updated_at`` for every change, so a usage row (``newest_use``) can
    be later than it. BB's ``weight_used`` holds that row already, so the reading is timed
    at the later of the two: every row lies before it, and none is taken off it again
    (R-6707-14). That time only times the reading: the link keeps BB's own ``updated_at``,
    so a BB edit after it still reaches the spool if a person unarchives it (R-6707-17)."""
    archived_at = bb.archived_at
    assert archived_at is not None
    real = bb.updated_at
    if newest_use is not None and (real is None or newest_use > real):
        bb = bb.model_copy(update={"updated_at": newest_use})
    spool_id, typeless = _create(conn, store, bb, after, now, actor, skipped, archived=True)
    if bb.updated_at != real:
        conn.execute(
            "UPDATE filament_bb_links SET bb_updated_at = %s WHERE spool_id = %s",
            (real or now, spool_id),
        )
    conn.execute(
        "UPDATE filament_spools SET archived_at = %s, updated_at = now() WHERE id = %s",
        (archived_at, spool_id),
    )
    at = archived_at if bb.updated_at is None else max(archived_at, bb.updated_at)
    ledger.record(
        conn,
        spool_id,
        "archived",
        actor=actor,
        at=at,
        data={"bb_spool_id": bb.id, "bb_archived_at": archived_at},
    )
    return spool_id, typeless


def _bind(
    conn: Conn, spool_id: UUID, after: dict[str, Any], data_origin: str | None, actor: str
) -> None:
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
    kind: m.TagKind = (
        "bambu_mifare"
        if tray is not None
        else "ntag_opentag"
        if data_origin == "opentag3d"
        else "other"
    )
    try:
        with conn.transaction():
            FilamentSpoolStore.bind_on(conn, spool_id, uid, tray, kind, None, actor)
    except TagKeysClashError:
        logger.warning(
            "a Bambuddy spool's tag keys name two tags; not bound",
            extra={"spool_id": str(spool_id)},
        )


# -- step 1: a linked spool takes what BB changed --------------------------------------


def _update(
    conn: Conn,
    run: _Run,
    bb: BbSpool,
    spool_id: UUID,
    pulled_at: datetime | None,
    skipped: list[str] | None = None,
    taken: set[str] | None = None,
    *,
    archived: bool = False,
) -> bool:
    """True when a field of the SB spool changed: a column, its type or insert type, its
    tag binding or its weight (a ``bb`` reading). With ``pulls.spools`` False no field,
    type or insert type moves; ``pulls.weights`` gates the ``bb`` reading and
    ``weight_locked``, ``pulls.tags`` the binding of a tag BB names, which a spool this
    pull just archived (``archived``) does not take: its archive unbound its tags. A field of a type
    that does not pull keeps its last value in the recorded reading (`_seen`), so the pull
    after the type is switched on reads it, BB's row moved or not. A catalog name with no
    key goes in ``skipped``. The `seen_row` names of the fields it took go in ``taken``.

    After the push's echo event (``data.pushed``), a field moved when BB's value is off
    the link's ``bb_seen`` (or absent from it): the echo holds Bambuddy's whole answer,
    which hides a person's edit of a field the push did not send (R-T5-5). Nothing is
    booked when nothing was taken, so the echo stays the last reading."""
    store, now, actor, pulls = run.store, run.now, run.actor, run.pulls
    unmoved = bb.updated_at is not None and pulled_at is not None and bb.updated_at <= pulled_at
    # A row BB did not move is read again: its event takes no ref, which the same stamp
    # would otherwise share with the one it replaces (`ledger.record` drops a repeat).
    ref = not unmoved
    last = conn.execute(
        "SELECT data -> 'bb' AS bb, data @> '{\"pushed\": true}' AS pushed"
        " FROM filament_spool_events"
        " WHERE spool_id = %s AND kind = 'synced_from_bb' AND data ? 'bb'"
        " ORDER BY at DESC, seq DESC LIMIT 1",
        (spool_id,),
    ).fetchone()
    before: dict[str, Any] = (last["bb"] if last is not None else None) or {}
    echo: dict[str, Any] | None = None
    if last is not None and last["pushed"]:
        held = conn.execute(
            "SELECT bb_seen FROM filament_bb_links WHERE spool_id = %s", (spool_id,)
        ).fetchone()
        echo = None if held is None else held["bb_seen"]
    seen_now = seen_row(bb) if echo is not None else {}

    def moved_off(field: str, value: Any) -> bool:
        name = _SEEN_NAMES.get(field)
        if echo is not None and name is not None:
            return name not in echo or seen_now.get(name) != echo[name]
        return bool(value != before.get(field))

    if unmoved:
        # A value BB changed while its type did not pull is read once the type does.
        current = snapshot(bb)
        unmoved = not any(
            pulls.of_field(k) and moved_off(k, current[k])
            for k in current.keys() - {"core_weight_catalog_id"}
        )
    if unmoved and not pulls.spools:
        # Only the insert type is read again on a row BB did not move.
        return False
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
    retag = (
        pulls.tags
        and not archived
        and keys != (before.get("tag_uid"), before.get("tray_uuid"))
        and keys != (None, None)
    )
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
    seen = _seen(after, before, pulls)
    moved = {
        f for f in after.keys() - _NOT_COLUMNS if moved_off(f, after[f]) and after[f] is not None
    }
    fresh = {f for f in moved if pulls.of_field(f)}
    if pulls.weights and moved_off("remaining_g", after["remaining_g"]):
        fresh.add("remaining_g")
    if seen == before and not fresh:
        return False
    # The newer change wins per field (Decision 8): what SB changed after BB's change
    # stays SB's.
    bb_at = bb.updated_at or now
    wanted = {
        f
        for f in moved
        if pulls.of_field(f) and not _sb_newer(conn, spool_id, _SEEN_NAMES[f], bb_at)
    }
    # `_values` resolves a BB material once, and scopes the subtype by it.
    values = _values(conn, after, wanted, material=spool.effective.material_id, skipped=skipped)
    current = spool.effective.model_dump() | {"weight_locked": spool.weight_locked}
    changes = {c: [current.get(c), v] for c, v in values.items() if current.get(c) != v}
    if changes.get("weight_locked") == [True, False] and not _lock_from_bb(conn, spool_id):
        # A BB unlock never clears a lock SB set.
        del changes["weight_locked"]
    if pulls.spools and spool.type_id is None:
        kind = _match(conn, store, after)
        if kind is not None:
            changes["type_id"] = [None, kind.id]
    if (
        pulls.spools
        and insert_id is not None
        and after["core_weight_catalog_id"] != before.get("core_weight_catalog_id")
    ):
        own, typed = _insert_state(conn, spool_id)
        # A key the last reading does not have is read for the first time: only where the
        # spool has none of its own, and a person has not cleared it since the link. A key
        # it has (even None) moved: BB's value wins unless an ``edited`` event since BB's
        # change names the field.
        if "core_weight_catalog_id" not in before:
            free = own is None and not _insert_edited_since_link(conn, spool_id)
        else:
            free = not _sb_newer(conn, spool_id, "core_weight_catalog_id", bb_at)
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
        _bind(conn, spool_id, after, bb.data_origin, actor)
    reading = after["remaining_g"]
    locked = bool(changes.get("weight_locked", [None, spool.weight_locked])[1])
    weight: str | None = None
    if not pulls.weights or reading is None or not moved_off("remaining_g", reading):
        reading = None
    elif locked:
        reading, weight = None, "locked"
    if seen == before and not changes and not retag and reading is None and weight is None:
        # Nothing taken after the push's echo: it stays the last reading.
        return False
    _synced(conn, spool_id, bb, seen, changes, reading, actor, weight=weight, ref=ref)
    FilamentSpoolStore.read(conn, spool_id, check=True)
    if taken is not None:
        taken |= {_COLUMN_SEEN[c] for c in changes if c in _COLUMN_SEEN}
        if reading is not None:
            taken.add("weight_used")
    return bool(changes) or retag or reading is not None


def _seen(after: dict[str, Any], before: dict[str, Any], pulls: Pulls) -> dict[str, Any]:
    """BB's reading as the pull records it: ``after`` for the fields of a type that pulls,
    the last recorded value (or none) for the rest."""
    seen = {k: v for k, v in after.items() if pulls.of_field(k)}
    return seen | {k: before[k] for k in after.keys() - seen.keys() if k in before}


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


def _book(conn: Conn, spool_id: UUID, bb: BbSpool, actor: str, weights: bool = True) -> bool:
    """BB's last weighing as a ``weighed`` event and its last drying as a ``dried`` one,
    each once: ref ``bb_scale``/``bb_dried`` and ``<bb id>/<time>``, at BB's time. The
    weighing has source ``scale`` (it is a scale reading, #6263), the drying ``bb``. The
    weighing's reading is the scale's gross weight less the core
    (`_core_g`), the gross kept in ``data``; with no core weight known it books no
    reading. A weighing is a physical reading, so it counts under the weight lock, which
    guards against estimates (§5.5). The ledger's rule picks the anchor: the weighing wins
    when it is the newest reading. With ``weights`` False (``filament_sync.weights`` does
    not pull) the weighing is not booked, and `_update` and `_link` book no ``bb`` reading
    either; `_create` keeps its first one, as a new spool needs a starting weight. The
    drying is ``weights`` data too (Decision 14), so it is not booked either. True when
    either is new."""
    booked = False
    if weights and bb.last_weighed_at is not None and bb.last_scale_weight is not None:
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
                source="scale",
                absolute_g=net,
                at=bb.last_weighed_at,
                ref_kind="bb_scale",
                ref_id=f"{bb.id}/{bb.last_weighed_at.isoformat()}",
                actor=actor,
                data=data,
            )
            is not None
        )
    if weights and bb.last_dried_at is not None:
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


# -- #6265 Task 9: newer wins, the seen copy, archive, K values, places, rows ----------


def _sb_newer(conn: Conn, spool_id: UUID, field: str, bb_at: datetime) -> bool:
    """Decision 8, F12: SB changed the spool's ``field`` (its `seen_row` name) strictly
    after ``bb_at``, by `bb_sync_plan.sb_changed_at`. The spool's ``created_at``, that
    rule's fallback, is no change here (the module says why): a tie goes to Bambuddy."""
    # Here, not at the top: `bb_sync_plan` reads this module.
    from scadbuddy.filament.bb_sync_plan import sb_changed_at

    at = sb_changed_at(conn, spool_id, field)
    if at <= bb_at:
        return False
    row = conn.execute(
        "SELECT created_at FROM filament_spools WHERE id = %s", (spool_id,)
    ).fetchone()
    return row is None or at != row["created_at"]


def _write_seen(conn: Conn, run: _Run, spool_id: UUID, bb: BbSpool, taken: set[str]) -> None:
    """R-T4-28 (decision 75): merge ``seen_row(bb)`` into the link's ``bb_seen``, at
    ``run.now``. A field goes in when the pull took it (``taken``) or Bambuddy's value is
    SB's compare value; any other keeps its old seen value, or stays out when unseen.
    R-T9-3 and R-T9-5 make two exceptions: a link with no seen location (under
    ``pulls.locations``) or no seen archive state (under ``pulls.spools``) takes
    Bambuddy's value as seen, so only a later Bambuddy change moves or archives it.
    ``bb_seen_at`` moves only when ``bb_seen`` changes (review M2): a running pass takes a
    newer one as `results` writing it after its snapshot (F11)."""
    from scadbuddy.filament import bb_sync_plan as plan

    seen = seen_row(bb)
    k_rows = json_form([k.model_dump() for k in bb.k_profiles])
    desired, _ = plan.desired_and_unmapped(conn, spool_id, run.refs, _ALL)
    sb = {
        n: plan.compare_form(n, v)
        for n, v in desired.items()
        if next(plan.pendings(v), None) is None
    }
    agree = {
        n: v
        for n, v in seen.items()
        if n in taken or (n in sb and plan.bb_form(n, seen, k_rows) == sb[n])
    }
    seeds = {
        n: seen[n]
        for n, on in (("location_id", run.pulls.locations), ("archived_at", run.pulls.spools))
        if on
    }
    # The seeds go first, so a seen value the link holds already wins over them.
    conn.execute(
        "UPDATE filament_bb_links SET bb_seen = n.seen, bb_seen_at = CASE WHEN"
        " n.seen IS DISTINCT FROM l.bb_seen OR l.bb_seen_at IS NULL THEN %s"
        " ELSE l.bb_seen_at END"
        " FROM filament_bb_links l, LATERAL (SELECT %s || coalesce(l.bb_seen, '{}'::jsonb)"
        " || %s AS seen) n"
        " WHERE filament_bb_links.spool_id = %s AND l.spool_id = filament_bb_links.spool_id",
        (run.now, Jsonb(seeds), Jsonb(agree), spool_id),
    )


def _link_seen(conn: Conn, spool_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        "SELECT bb_seen FROM filament_bb_links WHERE spool_id = %s", (spool_id,)
    ).fetchone()
    return (None if row is None else row["bb_seen"]) or {}


def _archive_state(conn: Conn, run: _Run, spool_id: UUID, bb: BbSpool, *, archived: bool) -> bool:
    """Take BB's archive state into the linked SB spool (``archived``: whether it is
    archived now); True when it changed. Only when BB's state moved off what SB last saw
    (``bb_seen``) and the spool's newest
    ``archived`` or ``restored`` event is not after BB's change (Decision 8). With no seen
    state it changes nothing: `_write_seen` seeds it (R-T9-5)."""
    bb_archived = bb.archived_at is not None
    if bb_archived == archived:
        return False
    seen = _link_seen(conn, spool_id)
    if "archived_at" not in seen or (seen["archived_at"] is not None) == bb_archived:
        return False
    times = [as_utc(t) for t in (bb.updated_at, bb.archived_at) if t is not None]
    at = max(times) if times else run.now
    if _sb_newer(conn, spool_id, "archived_at", at):
        return False
    run.store.spools.update_on(
        conn,
        spool_id,
        m.SpoolPatch(archived=bb_archived),
        actor=run.actor,
        at=_bb_event_at(conn, spool_id, at),
    )
    return True


def _bb_event_at(conn: Conn, spool_id: UUID, bb_at: datetime) -> datetime:
    """When the pull books an event that takes Bambuddy's change (review M3): at
    Bambuddy's change time, so `sb_changed_at` never counts the pull's own write as an SB
    change newer than Bambuddy's; but never before the spool's newest event, so the
    ledger keeps its order."""
    row = conn.execute(
        "SELECT max(at) + interval '1 microsecond' AS at FROM filament_spool_events"
        " WHERE spool_id = %s",
        (spool_id,),
    ).fetchone()
    after: datetime | None = None if row is None else row["at"]
    return bb_at if after is None else max(bb_at, after)


def _follow_location(
    conn: Conn, run: _Run, spool_id: UUID, bb: BbSpool, taken: set[str], *, new: bool = False
) -> bool:
    """Move a linked spool to the SB place BB's ``location_id`` is (none: out of every
    place), as Bambuddy's move (`LocationStore.move_on`); True when it moved. Only when
    BB's value moved off what SB last saw, the spool's newest ``moved`` event is not after
    BB's ``updated_at`` (Decision 8), the place is mapped, and the spool is in no
    printer's place (F18: an AMS unit, a tray, the external spool place, a plain printer
    place). With no seen location it never moves: `_write_seen` seeds it (R-T9-3). A
    spool the pull just made (``new``) has none yet, and is placed: a creation, not a
    move."""
    row = conn.execute(
        "SELECT s.location_id, l.printer_id FROM filament_spools s"
        " LEFT JOIN filament_locations l ON l.id = s.location_id WHERE s.id = %s",
        (spool_id,),
    ).fetchone()
    if row is None or row["printer_id"] is not None:
        return False
    target: UUID | None = None
    if bb.location_id is not None:
        found = conn.execute(
            "SELECT id FROM filament_locations WHERE bb_location_id = %s"
            " AND archived_at IS NULL AND printer_id IS NULL",
            (bb.location_id,),
        ).fetchone()
        if found is None:
            return False
        target = found["id"]
    if target == row["location_id"]:
        return False
    if not new:
        seen = _link_seen(conn, spool_id)
        if "location_id" not in seen or seen["location_id"] == bb.location_id:
            return False
    at = as_utc(bb.updated_at) or run.now
    if _sb_newer(conn, spool_id, "location_id", at):
        return False
    LocationStore.move_on(
        conn, spool_id, target, actor=run.actor, at=_bb_event_at(conn, spool_id, at)
    )
    taken.add("location_id")
    run.moved += 1
    return True


def _k4(value: Any) -> float:
    return round(float(value), 4)


def _bb_k_values(conn: Conn, run: _Run, spool_id: UUID, bb: BbSpool) -> bool:
    """BB's K-profiles as the spool's own K values, for the printers an SB printer links
    (``bambuddy_printer_id``), by `_bb_presets`' rule: nothing moves unless BB's list
    changed since the link's ``bb_k_profiles`` (which then takes BB's list); a key changes
    only where the spool has no row of its own, or its row still holds BB's last value of
    that key. Any other row (a person's) stays, and a clear in Bambuddy deletes only rows
    that still hold its last value. With no last list a link fills the keys it has no row
    for. A key whose effective value (the type's) is already BB's is not written. Rows
    that change record one ``synced_from_bb`` event (``changes.k_values``)."""
    from scadbuddy.filament.bb_sync_plan import k_form

    conn.execute("SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,))
    link = conn.execute(
        "SELECT bb_k_profiles FROM filament_bb_links WHERE spool_id = %s", (spool_id,)
    ).fetchone()
    if link is None:
        return False
    sent = json_form([k.model_dump() for k in bb.k_profiles])
    last = link["bb_k_profiles"]
    if last is not None and k_form(last) == k_form(cast("list[Any]", sent)):
        return False
    conn.execute(
        "UPDATE filament_bb_links SET bb_k_profiles = %s WHERE spool_id = %s",
        (Jsonb(sent), spool_id),
    )
    printers = {
        row["bb"]: row["id"]
        for row in conn.execute(
            "SELECT id, bambuddy_printer_id AS bb FROM printers"
            " WHERE bambuddy_printer_id IS NOT NULL"
        ).fetchall()
    }

    def keyed(rows: Sequence[Any]) -> dict[tuple[UUID, int, str], SpoolKProfile]:
        out: dict[tuple[UUID, int, str], SpoolKProfile] = {}
        for raw in rows:
            k = raw if isinstance(raw, SpoolKProfile) else SpoolKProfile.model_validate(raw)
            if (
                k.printer_id in printers
                and k.extruder is not None
                and k.nozzle_diameter
                and k.k_value is not None
            ):
                out[printers[k.printer_id], k.extruder, str(k.nozzle_diameter)] = k
        return out

    wants = keyed(bb.k_profiles)
    was = None if last is None else keyed(last)
    before = own_k_values(conn, spool_id)
    own = {(r["printer_id"], r["extruder"], r["nozzle_diameter"]): r for r in before}
    effective = {
        (k.printer_id, k.extruder, k.nozzle_diameter): _k4(k.k_value)
        for k in effective_k_values(conn, spool_id)
    }
    for key in sorted(wants.keys() | (was or {}).keys(), key=str):
        want, old = wants.get(key), (was or {}).get(key)
        same = (want is None and old is None) or (
            want is not None and old is not None and _k4(want.k_value) == _k4(old.k_value)
        )
        if was is not None and same:
            continue  # Bambuddy did not change this key.
        mine = own.get(key)
        if mine is not None and (old is None or _k4(mine["k_value"]) != _k4(old.k_value)):
            continue  # A row of the spool's own that is not Bambuddy's last value.
        printer, extruder, nozzle = key
        if want is None:
            if mine is not None:
                conn.execute(
                    "DELETE FROM filament_spool_k_values WHERE spool_id = %s"
                    " AND printer_id = %s AND extruder = %s AND nozzle_diameter = %s",
                    (spool_id, printer, extruder, nozzle),
                )
            continue
        if mine is None and effective.get(key) == _k4(want.k_value):
            continue
        conn.execute(
            "INSERT INTO filament_spool_k_values (spool_id, printer_id, extruder,"
            " nozzle_diameter, nozzle_type, k_value, name, cali_idx, setting_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (spool_id, printer_id, extruder, nozzle_diameter) DO UPDATE SET"
            " nozzle_type = EXCLUDED.nozzle_type, k_value = EXCLUDED.k_value,"
            " name = EXCLUDED.name, cali_idx = EXCLUDED.cali_idx,"
            " setting_id = EXCLUDED.setting_id",
            (
                spool_id,
                printer,
                extruder,
                nozzle,
                want.nozzle_type,
                Decimal(str(_k4(want.k_value))),
                want.name,
                want.cali_idx,
                want.setting_id,
            ),
        )
    after = own_k_values(conn, spool_id)
    if after == before:
        return False
    ledger.record(
        conn,
        spool_id,
        "synced_from_bb",
        actor=run.actor,
        data={"bb_spool_id": bb.id, "changes": {"k_values": [before, after]}},
    )
    return True


Verdict = Literal["take", "keep", "conflict"]


def _row_field(
    sb: Any,
    bb_now: Any,
    seen: Any,
    mark: Mapping[str, str] | None,
    sb_at: datetime,
    bb_at: datetime | None,
) -> Verdict:
    """One field of a supplier or location (compare forms): BB's value is taken when
    Bambuddy changed it since SB last saw it (off the seen value, or for a field the seen
    copy leaves out, off the mark's hash) and SB did not; when both did, nothing is taken
    whatever the row times say (R-T4-33, ``conflict``). With neither a seen value nor a
    mark, Decision 8's rule against the rows' ``updated_at``: SB's row strictly after
    BB's keeps SB's (a tie, or BB's time unknown, goes to Bambuddy)."""
    from scadbuddy.filament.bb_sync_plan import UNSEEN, field_hash

    if sb == bb_now:
        return "keep"
    if seen is UNSEEN and mark is None:
        return "keep" if bb_at is not None and sb_at > bb_at else "take"
    if seen is UNSEEN:
        assert mark is not None
        sb_moved = field_hash(sb) != mark["sb"]
        bb_moved = field_hash(bb_now) != mark["bb"]
    else:
        sb_moved, bb_moved = sb != seen, bb_now != seen
    if bb_moved and sb_moved:
        return "conflict"
    return "take" if bb_moved else "keep"


_ROW_LOCK = {"supplier": -1, "location": -2}


def _pull_rows(
    store: Filament,
    kind: Literal["supplier", "location"],
    rows: Sequence[BbSupplier] | Sequence[BbLocation],
    now: datetime,
    instance: str | None = None,
) -> tuple[int, int, int]:
    """BB's suppliers or locations into SB's rows, in one transaction under its own
    advisory lock: each is the SB row `link_bb_on` finds, links or makes. A row it made
    sees all of BB's values. A linked one takes each field `_row_field` says, unless
    another SB row holds the new name, and a nested place takes a new name only when it
    keeps its parents' path (`` / ``, Decision 14). A field kept by the time rule gets its
    seeding marks in ``bb_pushed["unseen"]`` (R-T4-27), so only a real change moves it
    later. The seen copy is merged (R-T4-28) at ``now``. An archived SB row is left alone.
    A BB row no SB row links, whose upper case name is the name an unlinked SB row with
    an open create intent pushes (a location's path), is the push's to adopt (Decision 10,
    review I1): it is skipped, as `_Run.claimed` skips a spool. The transaction records
    ``instance`` (`record_instance`). The rows made, linked or changed, the conflicts
    (R-T4-33), and the rows skipped for an intent."""
    from scadbuddy.filament import bb_sync_plan as plan

    spec = plan.ROW_KINDS[kind]
    changed = conflicts = claimed = 0
    with store.spools.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK, _ROW_LOCK[kind]))
        intents = {
            r["sb_id"]
            for r in conn.execute(
                "SELECT sb_id FROM filament_bb_creates WHERE kind = %s", (kind,)
            ).fetchall()
        }
        before = spec.items(conn)
        linked = {item.bb_id for item in before if item.bb_id is not None}
        wanted = {
            item.name.strip().upper()
            for item in before
            if item.bb_id is None and not item.archived and item.sb_id in intents
        }
        found: list[tuple[Any, UUID, BbLinked]] = []
        for row in rows:
            name = _text(row.name)
            if name is None:
                continue
            if row.id not in linked and name.upper() in wanted:
                claimed += 1
                continue
            bb_now = plan.row_seen(row, spec.readable)
            if isinstance(row, BbSupplier):
                values = {k: v for k, v in bb_now.items() if k != "name"}
                hit = SuppliersStore.link_bb_on(conn, row.id, name, values)
            else:
                hit = LocationStore.link_bb_on(conn, row.id, name, _text(row.identifier))
            if hit is None:
                logger.warning(
                    "a Bambuddy %s is not pulled: an SB row holds its name",
                    kind,
                    extra={"bb_id": row.id},
                )
                continue
            found.append((row, *hit))
        items = {item.sb_id: item for item in spec.items(conn)}
        for row, sb_id, how in found:
            item = items.get(sb_id)
            if item is None or item.archived:
                continue
            bb_now = plan.row_seen(row, spec.readable)
            at = conn.execute(
                f"SELECT updated_at FROM {spec.table} WHERE id = %s", (sb_id,)
            ).fetchone()
            assert at is not None
            written: dict[str, Any] = {}
            marks: dict[str, dict[str, str]] = {}
            for name, value in bb_now.items():
                sb = item.desired.get(name, plan.UNSEEN)
                seen = plan.UNSEEN if item.seen is None else item.seen.get(name, plan.UNSEEN)
                mark = item.unseen.get(name)
                verdict = _row_field(sb, value, seen, mark, at["updated_at"], row.updated_at)
                if verdict == "conflict":
                    conflicts += 1
                elif verdict == "take":
                    if _write_field(conn, spec.table, sb_id, name, value, item.name):
                        written[name] = value
                elif sb != value and seen is plan.UNSEEN and mark is None:
                    marks[name] = {"sb": plan.field_hash(sb), "bb": plan.field_hash(value)}
            after = item.desired | written
            agree = {n: v for n, v in bb_now.items() if after.get(n, plan.UNSEEN) == v}
            conn.execute(
                f"UPDATE {spec.table} SET bb_seen = coalesce(bb_seen, '{{}}'::jsonb) || %s,"
                " bb_seen_at = %s WHERE id = %s",
                (Jsonb(agree), now, sb_id),
            )
            if marks:
                conn.execute(
                    f"UPDATE {spec.table} SET bb_pushed = bb_pushed || jsonb_build_object("
                    "%s::text, coalesce(bb_pushed -> %s, '{}'::jsonb) || %s) WHERE id = %s",
                    (plan.UNSEEN_KEY, plan.UNSEEN_KEY, Jsonb(marks), sb_id),
                )
            if how != "found" or written:
                changed += 1
        record_instance(conn, instance)
    return changed, conflicts, claimed


def _write_field(conn: Conn, table: str, sb_id: UUID, name: str, value: Any, path: str) -> bool:
    """Write one field Bambuddy changed into an SB supplier or location, in a savepoint;
    False when it was not written (another SB row has the name, or a nested place's new
    name leaves its parents' path ``path`` names)."""
    if table == "filament_locations" and name == "name":
        own = conn.execute(
            "SELECT name, parent_id FROM filament_locations WHERE id = %s", (sb_id,)
        ).fetchone()
        assert own is not None
        if own["parent_id"] is not None:
            prefix = path[: len(path) - len(own["name"])]
            if not (isinstance(value, str) and value.startswith(prefix) and value[len(prefix) :]):
                return False
            value = value[len(prefix) :]
    try:
        with conn.transaction():
            conn.execute(
                f"UPDATE {table} SET {name} = %s, updated_at = now() WHERE id = %s",
                (value, sb_id),
            )
    except errors.UniqueViolation:
        logger.warning(
            "a Bambuddy name is not pulled: another SB row has it",
            extra={"table": table, "sb_id": str(sb_id)},
        )
        return False
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
    conn: Conn,
    after: dict[str, Any],
    fields: Any,
    *,
    material: UUID | None = None,
    skipped: list[str] | None = None,
) -> dict[str, Any]:
    """The SB columns of the BB ``fields`` that have a value; catalog names resolved, and
    added when missing. A subtype belongs to ``material`` (the new one, else this). A
    catalog name with no letter or digit has no key: its field is left out, as if BB sent
    none, and named in ``skipped`` (R-6706-18)."""
    values: dict[str, Any] = {}

    def keyed(field: str, table: str, scope: UUID | None) -> UUID | None:
        found = _resolve(conn, table, after[field], scope)
        if found is None and skipped is not None:
            skipped.append(field)
        return found

    if "material" in fields and after["material"] is not None:
        found = keyed("material", "filament_materials", None)
        if found is not None:
            values["material_id"] = material = found
    for field, (column, table) in _CATALOGS.items():
        if field == "material" or field not in fields or after[field] is None:
            continue
        found = keyed(field, table, material)
        if found is not None:
            values[column] = found
    for field in _PLAIN:
        if field in fields and after[field] is not None:
            values[field] = _column_value(field, after[field])
    return values


def _catalog_id(conn: Conn, table: str, name: str, material: UUID | None) -> UUID:
    """The row of ``table`` whose name or alias has ``name``'s key (`catalog.find_by_key`,
    archived or not), else a new one. A subtype is ``material``'s own when it has one of
    the key, else one for every material, else a new one of ``material``. The CSV import
    resolves its names with it (`csv_import.py`)."""
    return resolve_or_add(
        conn, KIND_OF_TABLE[table], name, sort_order=_ADDED_SORT, material=material
    )


def _resolve(conn: Conn, table: str, name: str, material: UUID | None = None) -> UUID | None:
    """The row of ``table`` whose name or alias has ``name``'s key (`catalog.lookup_key`,
    archived or not, in one query with the key check, R-T9-0b), else a new one; ``None``
    for a name with no letter or digit (no key, R-6706-18). A subtype is ``material``'s
    own when it has one of the key, else one for every material, else a new one of
    ``material``."""
    kind = KIND_OF_TABLE[table]
    keyed, found = lookup_key(conn, kind, name, material=material)
    if not keyed:
        return None
    if found is not None:
        return found
    return resolve_or_add(conn, kind, name, sort_order=_ADDED_SORT, material=material)


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
    store: Filament, settings: StoredSettings, *, actor: str = ACTOR, pulls: Pulls = EVERY
) -> PullReport:
    """Read BB's spools, the archived ones too (#6707), and pull them (``pulls`` the data
    types taken); a malformed row is skipped and counted as ``invalid``. Then book BB's
    usage rows (`bambuddy_usage.py`) on linked spools, while ``pulls.spools`` or
    ``pulls.weights`` (#6265 F1). BB not configured is `client_for`'s 409; BB's own errors
    on the spools are its client's.

    The usage rows are read right after the spools, so a deduction BB writes between the
    two reads shows in the usage rows, never only in a spool's value. The archives are
    read after the pull, and only when a usage row that would book is not booked yet, so
    the rows of a spool this pull made are matched with their archives too. The usage
    read, the query for unbooked rows, the archive read and the booking each fail alone:
    logged, and the spool pull's report stands. When the usage read fails, no archived
    BB spool is made in this pull (``archived_deferred``, R-6707-16).

    BB's suppliers are read only under ``pulls.suppliers``, and its locations only under
    ``pulls.locations``, whatever ``pulls.spools`` says (#6265 Decision 16); each read
    fails alone, logged, and leaves its rows alone."""
    usage: list[SpoolUsage] = []
    usage_failed = False
    archives: list[Archive] = []
    suppliers: list[BbSupplier] | None = None
    locations: list[BbLocation] | None = None
    async with client_for(settings) as client:
        rows = await client.spool_rows(include_archived=True)
        try:
            if pulls.spools or pulls.weights:
                usage = await client.spool_usage()
        except Exception as error:
            usage_failed = True
            _failed("could not read Bambuddy's usage rows", error)
        try:
            if pulls.suppliers:
                suppliers = await client.suppliers()
        except Exception as error:
            _failed("could not read Bambuddy's suppliers", error)
        try:
            if pulls.locations:
                locations = await client.locations()
        except Exception as error:
            _failed("could not read Bambuddy's locations", error)
    spools, bad = parse_rows(rows)
    catalog = await read_catalog(settings) if pulls.spools else []
    report = await asyncio.to_thread(
        lambda: pull(
            store,
            spools,
            now=datetime.now(UTC),
            actor=actor,
            catalog=catalog or (),
            pulls=pulls,
            usage=usage,
            defer_archived=usage_failed,
            suppliers=suppliers,
            locations=locations,
            instance=instance_for(settings.bambuddy_url) if settings.bambuddy_url else None,
        )
    )
    report.catalog_failed = report.catalog_failed or catalog is None
    report.invalid = len(bad)
    report.invalid_bb_ids = [i for i in bad if i is not None]
    pending: set[int] = set()
    try:
        pending = await asyncio.to_thread(_unbooked, store, usage) if usage else set()
    except Exception as error:
        _failed("could not find Bambuddy's unbooked usage rows", error)
    if pending:
        try:
            since = min(row.created_at for row in usage if row.id in pending).date()
            async with client_for(settings) as client:
                archives = await _read_archives(client, since - ARCHIVE_LEAD)
        except Exception as error:
            _failed("could not read Bambuddy's archives", error)
    if usage:
        try:
            booked = await asyncio.to_thread(_book_usage, store, usage, archives, actor)
        except Exception as error:
            _failed("could not book Bambuddy's usage rows", error)
        else:
            report.usage_booked, report.usage_unlinked = booked.booked, booked.unlinked
    logger.info(
        "pulled Bambuddy's spools",
        extra={"usage_booked": report.usage_booked, "usage_unlinked": report.usage_unlinked},
    )
    return report


#: How long before its usage row an archive may have been made. The archive window is on
#: its ``created_at`` (the dispatch), and the row comes when the print ends; a day covers
#: a print that long, and the window is on dates.
ARCHIVE_LEAD = timedelta(days=1)


async def _read_archives(client: BambuddyClient, since: date) -> list[Archive]:
    """The archives made from ``since`` on, paged as `linking` pages them (an overlap, at
    most `MAX_ARCHIVE_PAGES` pages): the newest 50 would miss the archives of the older
    usage rows a first pull books (#6598). A read that hits the cap logs it and keeps
    what it has."""
    found: dict[int, Archive] = {}
    for page in range(MAX_ARCHIVE_PAGES):
        rows = await client.archives(
            date_from=since, limit=ARCHIVE_PAGE, offset=page * (ARCHIVE_PAGE - ARCHIVE_OVERLAP)
        )
        found.update((row.id, row) for row in rows)
        if len(rows) < ARCHIVE_PAGE:
            break
    else:
        logger.warning(
            "stopped reading Bambuddy's archives for usage rows; the archives of the oldest"
            " pending rows were not read (pages probably come newest first)",
            extra={"since": since.isoformat(), "pages": MAX_ARCHIVE_PAGES},
        )
    return list(found.values())


def _failed(message: str, error: Exception) -> None:
    logger.warning(message, extra={"error": type(error).__name__}, exc_info=True)


def _unbooked(store: Filament, usage: Sequence[SpoolUsage]) -> set[int]:
    with store.spools.transaction() as conn:
        return bambuddy_usage.unbooked(conn, usage)


def _book_usage(
    store: Filament, usage: Sequence[SpoolUsage], archives: Sequence[Archive], actor: str
) -> bambuddy_usage.UsageReport:
    with store.spools.transaction() as conn:
        return bambuddy_usage.book_usage(conn, usage, archives, actor=actor)


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
            if error.type == BAD_BODY_PROBLEM:
                # No JSON, or not a list (R-T7-10): the client's own detail, no Bambuddy text.
                logger.warning(
                    "no spool catalog from Bambuddy: an answer of another shape (%s)", error.detail
                )
            else:
                logger.warning("no spool catalog from Bambuddy: %s", error.detail)
        except ValueError as error:
            # A pydantic ValidationError: rows of another shape. Only the type is logged: its
            # text quotes Bambuddy's values (#6580).
            logger.warning(
                "no spool catalog from Bambuddy: an answer of another shape (%s)",
                type(error).__name__,
            )
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
            if error.type == BAD_BODY_PROBLEM:
                # No JSON, or not a list (R-T7-10): the client's own detail, no Bambuddy text.
                logger.warning(
                    "no tray assignments from Bambuddy: an answer of another shape (%s)",
                    error.detail,
                )
            else:
                logger.warning("no tray assignments from Bambuddy: %s", error.detail)
        except ValueError as error:
            # A pydantic ValidationError: rows of another shape. Only the type is logged: its
            # text quotes Bambuddy's values (#6580).
            logger.warning(
                "no tray assignments from Bambuddy: an answer of another shape (%s)",
                type(error).__name__,
            )
    return None


async def read_ha_sensors(store: Filament, settings: StoredSettings, now: datetime) -> None:
    """Read Bambuddy's Home Assistant states into the location sensors (#6268): one GET,
    and none when no Home Assistant sensor is set up. The GET reads every entity, because
    Bambuddy 1.3.1's route takes no list of ids (#6679, plan Decision 26). A state is a
    reading at ``now`` (see `sensors.apply_ha_states_on`). It never fails the pull: a
    failure is logged with its type only, and every Home Assistant sensor on a live place
    is marked unreachable (it got no number)."""
    try:
        if not any(
            s.source == "home_assistant" for s in await asyncio.to_thread(store.sensors.list, None)
        ):
            return
        async with client_for(settings) as client:
            entities = await client.ha_entities(None)
        states = {e.entity_id: e.state for e in entities}
        await asyncio.to_thread(_apply_ha, store, states, now)
    except Exception as error:
        _failed("could not read Bambuddy's Home Assistant sensors", error)
        try:
            await asyncio.to_thread(_apply_ha, store, {}, now)
        except Exception as again:
            _failed("could not mark the Home Assistant sensors unreachable", again)


def _apply_ha(store: Filament, states: dict[str, str | None], now: datetime) -> None:
    with store.spools.transaction() as conn:
        sensors.apply_ha_states_on(conn, states, now)


async def scheduled_pull(
    store: Filament, load: Callable[[], StoredSettings], sync: Callable[[], FilamentSync]
) -> Outcome:
    """The Schedule's pull (`SpoolRefresh`): ``skipped`` when no data type of
    ``filament_sync`` pulls (#6265 F1) or no BB URL,
    ``failed`` when BB or the store fails (logged, never raised),
    else ``pulled``. ``sync`` reads the setting as it is now (#6262 Task 13). Whenever a
    BB URL is set, it first reads the Home Assistant sensors (`read_ha_sensors`), before
    the spools gate and whatever ``sync`` says: sensors are not spool data. After a pull
    it reconciles the untagged trays with Bambuddy's assignments (`reconcile_trays`)."""
    settings = await asyncio.to_thread(load)
    if settings.bambuddy_url:
        # Sensors are not spool data: read whatever ``filament_sync`` says.
        await read_ha_sensors(store, settings, datetime.now(UTC))
    filament_sync = await asyncio.to_thread(sync)
    if not any(core_settings.pulls(d) for d in filament_sync.model_dump().values()):
        logger.info("Bambuddy's spools are not pulled: no Filament sync pulls")
        return "skipped"
    if not settings.bambuddy_url:
        logger.info("no Bambuddy configured; its spools are not pulled")
        return "skipped"
    try:
        report = await read_and_pull(store, settings, pulls=Pulls.of(filament_sync))
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
    await reconcile_trays(store, settings)
    return "pulled"


async def reconcile_trays(store: Filament, settings: StoredSettings) -> None:
    """Set right each untagged tray whose placed spool disagrees with what the printer
    reports, by Bambuddy's assignments (`FilamentInventory.reconcile_trays`, #6713). The
    pull runs it: it is the one place that reads Bambuddy's assignments and has the
    printers' stored reports (the capture backstop holds no Bambuddy settings). A failed
    assignment read (`try_assignments`) reconciles nothing this pass. It never fails the
    pull: a failure is logged with its type only."""
    try:
        assignments = await try_assignments(settings)
        if assignments is None:
            logger.info("no tray reconcile: Bambuddy's assignments could not be read")
            return
        moved = await asyncio.to_thread(store.inventory.reconcile_trays, assignments)
    except Exception as error:
        _failed("could not reconcile the trays with Bambuddy's assignments", error)
        return
    if moved:
        logger.info("reconciled trays with Bambuddy's assignments", extra={"moved": len(moved)})
