"""Bambuddy's spools pulled into ScadBuddy's own (#6261, spec §8.1, §8.2). The BB to SB
side only: nothing here writes to Bambuddy (SB to BB is the sync of #6265,
`bb_sync_plan.py`, `bb_sync_run.py`). Since #6772 the CRUD service reads no Bambuddy for
the pull: the print worker reads it (`bb_pull_run.py`) and sends the rows, and
`bb_pull_apply.py` runs `pull` on them. `try_assignments` stays here: the API's inventory
view reads the assignments too.

`pull` takes each BB spool in its own transaction, under an advisory lock on its id, in
this order, except a linked one on which nothing would change (`_quiet`, #6768): those are
read for every spool at once and get no transaction of their own.

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
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.errors import BAD_BODY_PROBLEM
from scadbuddy.bambuddy.models import (
    BbLocation,
    BbSupplier,
    SpoolAssignment,
    SpoolCatalogEntry,
    SpoolKProfile,
    SpoolUsage,
    as_utc,
)
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.core.cancel import CancelledError, CancelToken, check, watched
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import ledger
from scadbuddy.filament import models as m

# Re-exported: the wire of #6772 holds them, and the pull's callers import them from here.
from scadbuddy.filament.bb_pull_models import PullReport as PullReport
from scadbuddy.filament.bb_pull_models import Pulls as Pulls
from scadbuddy.filament.bb_sync_models import (
    PULL_SEEN_FIELDS,
    SEEN_FIELDS,
    BbLinked,
    json_form,
    seen_row,
)
from scadbuddy.filament.catalog import KIND_OF_TABLE, lookup_key, resolve_or_add
from scadbuddy.filament.colour import canonical_colours, canonical_rgba
from scadbuddy.filament.component import Filament
from scadbuddy.filament.dry_box import BoxFullError, occupant_on
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.presets import drop_migrated, effective_k_values, own_k_values, own_presets
from scadbuddy.filament.sb_presets import by_bb_id_on, links_elsewhere_on
from scadbuddy.filament.spools import FilamentSpoolStore, TagKeysClashError
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.operations.kinds import waiting_on_bambuddy
from scadbuddy.slicing.own_presets import sb_ref
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
    "data_origin",
    "added_full",
    "tag_encoded_at",
)
#: The snapshot fields that are no column: what is left, the tags, the product code and
#: the consumed counter's baseline (#7149 R-7149-7: SB's ledger keeps the counter).
_NOT_COLUMNS = frozenset(
    {
        "remaining_g",
        "tag_uid",
        "tray_uuid",
        "material_number",
        "core_weight_catalog_id",
        "consumed_baseline",
    }
)
#: The `seen_row` names a link's ``bb_seen`` holds, which a push's echo is judged by.
_WATCHED = frozenset({*SEEN_FIELDS, *PULL_SEEN_FIELDS})
#: The snapshot keys #7149 added. A reading from before it lacks them (review nit 2 on
#: #7181): `_seeded` fills them in quietly.
_NEW_KEYS = ("data_origin", "added_full", "tag_encoded_at", "consumed_baseline")
#: The snapshot name of each `PULL_SEEN_FIELDS` field.
_PULL_SEEN_SNAPSHOT = {
    "added_full": "added_full",
    "encode_time": "tag_encoded_at",
    "weight_used_baseline": "consumed_baseline",
}
#: Bambuddy's ``data_origin`` as the column takes it (its ``String(20)``, R-7149-1).
_ORIGIN = re.compile(r"[a-z0-9_]{1,20}")
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
    "data_origin": "data_origin",
    "added_full": "added_full",
    "tag_encoded_at": "encode_time",
    "consumed_baseline": "weight_used_baseline",
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
    "data_origin": "data_origin",
    "added_full": "added_full",
    "tag_encoded_at": "encode_time",
}
#: The catalog entries already warned about as skipped, so a clash is told once a process.
_WARNED_SKIPS: set[tuple[int, str]] = set()
_WARNED_SKIPS_MAX = 1000
#: The BB spools already warned about as duplicates, so each is told once a process.
_WARNED_DUPLICATES: set[int] = set()

#: The data types `FilamentSync`'s defaults pull: the default of `pull`.
EVERY = Pulls()


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
        "data_origin": _origin(bb.data_origin),
        "added_full": bb.added_full,
        "tag_encoded_at": None if bb.encode_time is None else json_form(bb.encode_time),
        "consumed_baseline": bb.weight_used_baseline,
    }


def _seeded(
    before: Mapping[str, Any], snap: Mapping[str, Any], holds: Callable[[str, Any], bool]
) -> dict[str, Any]:
    """Review nit 2 on #7181: a reading from before #7149 lacks `_NEW_KEYS`. Each missing
    key takes Bambuddy's value as if it had been read, so the first pull after the deploy
    books no event and no reset for it, when the value says nothing new: the baseline
    (a first reading of it is no reset, R-7149-6), a null, or a value SB already holds
    (``holds``). Any other value stays missing, so the pull takes it as a change."""
    if not before:
        return dict(before)
    out = dict(before)
    for key in _NEW_KEYS:
        if key in out:
            continue
        value = snap[key]
        if key == "consumed_baseline" or value is None or holds(key, value):
            out[key] = value
    return out


def _origin(value: str | None) -> str | None:
    """BB's ``data_origin`` when the column takes it, else none (R-7149-1)."""
    return value if value is not None and _ORIGIN.fullmatch(value) else None


def _column_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field == "cost_per_kg":
        return Decimal(value)
    if field == "tag_encoded_at":
        return as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    return value


#: Some SB row, spool or preset is linked to a Bambuddy row (Decision 12).
ANY_LINK = (
    "EXISTS (SELECT 1 FROM filament_bb_links)"
    " OR EXISTS (SELECT 1 FROM filament_suppliers WHERE bb_supplier_id IS NOT NULL)"
    " OR EXISTS (SELECT 1 FROM filament_locations WHERE bb_location_id IS NOT NULL)"
    " OR EXISTS (SELECT 1 FROM filament_insert_types WHERE bb_catalog_id IS NOT NULL)"
    " OR EXISTS (SELECT 1 FROM filament_spool_types WHERE bb_filament_id IS NOT NULL)"
    " OR EXISTS (SELECT 1 FROM filament_colours WHERE bb_colour_id IS NOT NULL)"
    # ScadBuddy's presets' Bambuddy ids (#7176): the mirror's and the adopted ones.
    " OR EXISTS (SELECT 1 FROM filament_presets WHERE bb_preset_id IS NOT NULL)"
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


def links_elsewhere(store: Filament, configured: str) -> bool:
    """Whether the links' recorded instance is another than ``configured`` (Decision 12):
    the pull then takes nothing, or it would write another Bambuddy's values onto linked
    spools (#6845 R-6845-5). One statement on a pooled connection, with no transaction
    block of its own: `LocalBbPull._check` runs it on every body."""
    return links_elsewhere_on(store.spools.pool, configured)


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
    cancel: CancelToken | None = None,
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
    (`record_instance`, Decision 12).

    ``cancel`` (the CRUD route's, cancelled when the print worker goes away, #6902) stops
    the pull between BB spools and cancels the running statement, so the spool in flight
    rolls back (unless its transaction had already reached its commit, after its last
    check) and `CancelledError` or ``QueryCanceled`` is raised; the spools pulled before it
    stay, each a whole transaction that the next pull leaves as it is."""
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
    check(cancel)
    run = _Run(store, now, actor, pulls, defer_archived, *_context(store, pulls), instance=instance)
    try:
        quiet = _quiet(store, run, spools, cancel)
    except (CancelledError, errors.QueryCanceled):
        raise
    except Exception as error:
        # R-6768-4: the batch read never fails the pull; each spool takes its own path.
        logger.warning(
            "could not read which Bambuddy spools are unchanged; each is pulled",
            extra={"error": type(error).__name__},
            exc_info=True,
        )
        quiet = {}
    for bb in sorted(spools, key=lambda s: s.archived_at is not None):
        check(cancel)
        skipped: list[str] = []
        try:
            if bb.id in quiet:
                outcome = quiet[bb.id]
            else:
                with store.spools.transaction() as conn, watched(cancel, conn):
                    conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK, bb.id))
                    outcome = _one(conn, run, bb, skipped, newest.get(bb.id))
                    record_instance(conn, instance)
                    check(cancel)
        except (CancelledError, errors.QueryCanceled):
            # The caller went away: the pull stops, never a spool that failed.
            raise
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
    #: The Bambuddy this pull reads (`pull`'s ``instance``): a preset id maps only to a
    #: preset of it (`sb_presets.by_bb_id_on`).
    instance: str | None
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


#: What `_one` answers for a BB spool: what came of it, its SB spool, whether it needs a type.
_Outcome = tuple[str, UUID | None, bool] | None


def _quiet(
    store: Filament, run: _Run, spools: Sequence[BbSpool], cancel: CancelToken | None = None
) -> dict[int, _Outcome]:
    """#6768 (R-6768-1, R-6768-2): the linked BB spools on which `_one` would change
    nothing, by BB id, with what it would answer (``archived`` for an archived SB spool,
    else None). Read for every spool at once, in a fixed number of queries, after the
    catalog, suppliers and locations are pulled: the pull then opens no transaction for
    them. A BB spool is quiet only when each step of `_one` is a no-op for it:

    - BB's ``updated_at`` is the link's ``bb_updated_at`` (an archived SB spool: not
      older), and the link has a seen copy with a time;
    - its archive state and place agree with the seen copy, or the copy has none
      (`_archive_state`, `_follow_location`);
    - for a spool that is not archived, the last ``synced_from_bb`` reading is BB's row as
      `_update` records it (`_reading`, `_seen`: an unmapped catalog id kept or left out,
      a type that does not pull kept, R-6768-5), and after the push's echo every seen
      field is BB's (`_update`); the insert type BB names is unmapped, the spool's own or
      its type's, or a person edited it since the link (`_backfill_insert_type`,
      R-6768-6); under ``weights`` BB's weighing and drying are booked
      (`_book`); under ``presets`` the link holds BB's preset and K-profiles
      (`_bb_presets`, `_bb_k_values`);
    - the seen copy `_write_seen` would merge is the one the link holds.

    Anything else, or a doubt (a value of another JSON form), goes through `_one`. The
    reads are not under the spools' locks: a write that lands after them is taken by the
    next pull (R-6768-3). ``cancel`` stops the read as it stops a spool's transaction; any
    other failure is the caller's to log, and then no spool is quiet (R-6768-4)."""
    from scadbuddy.filament import bb_sync_plan as plan

    timed = {bb.id: bb for bb in spools if bb.updated_at is not None}
    if not timed:
        return {}
    pulls = run.pulls
    with store.spools.transaction() as conn, watched(cancel, conn):
        links: dict[int, Any] = {}
        for row in conn.execute(
            "SELECT l.bb_spool_id, l.spool_id, l.bb_updated_at, l.bb_seen, l.bb_seen_at,"
            " l.bb_k_profiles, l.bb_slicer_filament, l.bb_slicer_filament_name,"
            " s.archived_at IS NOT NULL AS archived, s.insert_type_id AS own,"
            " t.insert_type_id AS typed"
            " FROM filament_bb_links l JOIN filament_spools s ON s.id = l.spool_id"
            " LEFT JOIN filament_spool_types t ON t.id = s.type_id"
            " WHERE l.bb_spool_id = ANY(%s)",
            (list(timed),),
        ).fetchall():
            at, held = as_utc(timed[row["bb_spool_id"]].updated_at), row["bb_updated_at"]
            same = held is not None and (at == held or (row["archived"] and at > held))
            if same and row["bb_seen"] is not None and row["bb_seen_at"] is not None:
                links[row["bb_spool_id"]] = row
        if not links:
            return {}
        ids = [row["spool_id"] for row in links.values()]
        last = {
            row["spool_id"]: row
            for row in conn.execute(
                "SELECT DISTINCT ON (spool_id) spool_id, data -> 'bb' AS bb,"
                " data @> '{\"pushed\": true}' AS pushed FROM filament_spool_events"
                " WHERE spool_id = ANY(%s) AND kind = 'synced_from_bb' AND data ? 'bb'"
                " ORDER BY spool_id, at DESC, seq DESC",
                (ids,),
            ).fetchall()
        }
        refs = [ref for bb_id in links for _, _, ref in _book_refs(timed[bb_id])]
        booked = {
            (row["spool_id"], row["kind"], row["ref_kind"], row["ref_id"])
            for row in conn.execute(
                "SELECT spool_id, kind, ref_kind, ref_id FROM filament_spool_events"
                " WHERE spool_id = ANY(%s) AND ref_kind IN ('bb_scale', 'bb_dried')"
                " AND ref_id = ANY(%s)",
                (ids, refs),
            ).fetchall()
        }
        inserts = {
            row["bb_catalog_id"]: row["id"]
            for row in conn.execute(
                "SELECT bb_catalog_id, id FROM filament_insert_types"
                " WHERE bb_catalog_id = ANY(%s) AND archived_at IS NULL",
                ([timed[i].core_weight_catalog_id for i in links],),
            ).fetchall()
        }
        edited = {
            row["spool_id"]
            for row in conn.execute(
                "SELECT DISTINCT e.spool_id FROM filament_spool_events e"
                " JOIN filament_bb_links l ON l.spool_id = e.spool_id"
                " WHERE e.spool_id = ANY(%s) AND e.kind = 'edited' AND e.at > l.linked_at"
                " AND jsonb_typeof(e.data -> 'changes') = 'object'"
                " AND e.data -> 'changes' ? 'insert_type_id'",
                (ids,),
            ).fetchall()
        }
        named = {timed[i].location_id for i in links} | {
            row["bb_seen"].get("location_id") for row in links.values()
        }
        places = conn.execute(
            "SELECT bb_location_id, archived_at IS NULL AND printer_id IS NULL AS open"
            " FROM filament_locations WHERE bb_location_id = ANY(%s)",
            ([p for p in named if isinstance(p, int)],),
        ).fetchall()
        ever = {row["bb_location_id"] for row in places}
        open_places = {row["bb_location_id"] for row in places if row["open"]}
        steady = {
            bb_id: link
            for bb_id, link in links.items()
            if _steady(
                timed[bb_id], link, last.get(link["spool_id"]), booked, inserts, edited, pulls
            )
        }
        if not steady:
            return {}
        # The pull compares values only; a reset is the push's (#7149), so it reads none.
        facts = plan.SpoolFacts.read(
            conn, [link["spool_id"] for link in steady.values()], _ALL, resets=False
        )
        quiet: dict[int, _Outcome] = {}
        for bb_id, link in steady.items():
            stored: dict[str, Any] = link["bb_seen"]
            desired, _ = plan.desired_and_unmapped(conn, link["spool_id"], run.refs, _ALL, facts)
            seeds, stale, agree = _seen_parts(
                run,
                timed[bb_id],
                desired,
                set(),
                held=partial(stored.get, "location_id"),
                mapped=lambda place: place in open_places,
                ever_mapped=lambda place: place in ever,
            )
            kept = {k: v for k, v in stored.items() if not (stale and k == "location_id")}
            if seeds | kept | agree != stored:
                continue
            quiet[bb_id] = ("archived", link["spool_id"], False) if link["archived"] else None
    return quiet


def _steady(
    bb: BbSpool,
    link: Mapping[str, Any],
    last: Mapping[str, Any] | None,
    booked: set[tuple[UUID, str, str, str]],
    inserts: Mapping[int, UUID],
    edited: set[UUID],
    pulls: Pulls,
) -> bool:
    """`_quiet`'s checks of one linked BB spool but the seen copy's merge."""
    from scadbuddy.filament.bb_sync_plan import k_form

    stored: dict[str, Any] = link["bb_seen"]
    seen = seen_row(bb)
    if (
        pulls.spools
        and "archived_at" in stored
        and (stored["archived_at"] is None) != (bb.archived_at is None)
    ):
        return False
    if pulls.locations and "location_id" in stored and stored["location_id"] != seen["location_id"]:
        return False
    if link["archived"]:
        return True
    if last is None:
        return False
    # A value the link's seen copy holds already is one SB took or agreed with.
    before = _seeded(
        last["bb"] or {},
        snapshot(bb),
        lambda key, _value: (
            _SEEN_NAMES[key] in stored and stored[_SEEN_NAMES[key]] == seen.get(_SEEN_NAMES[key])
        ),
    )
    insert = None if bb.core_weight_catalog_id is None else inserts.get(bb.core_weight_catalog_id)
    after = _reading(bb, before, insert)
    if _seen(after, before, pulls) != before:
        return False
    if last["pushed"] and any(
        pulls.of_field(k)
        and (_SEEN_NAMES[k] not in stored or stored[_SEEN_NAMES[k]] != seen.get(_SEEN_NAMES[k]))
        for k in after
        if _SEEN_NAMES.get(k) in _WATCHED
    ):
        return False
    spool_id = link["spool_id"]
    if (
        pulls.spools
        and insert is not None
        and link["own"] is None
        and link["typed"] != insert
        and spool_id not in edited
    ):
        return False
    if pulls.weights and any((spool_id, *ref) not in booked for ref in _book_refs(bb)):
        return False
    if pulls.presets:
        if (link["bb_slicer_filament"], link["bb_slicer_filament_name"]) != _preset_sent(bb):
            return False
        sent = json_form([k.model_dump() for k in bb.k_profiles])
        if link["bb_k_profiles"] is None or k_form(link["bb_k_profiles"]) != k_form(
            cast("list[Any]", sent)
        ):
            return False
    return True


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
        presets = pulls.presets and _bb_presets(conn, spool_id, bb, actor, run.instance)
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
            _bb_presets(conn, held_id, bb, actor, run.instance)
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
        _bb_presets(conn, made, bb, actor, run.instance)
        _bb_k_values(conn, run, made, bb)
    if pulls.locations:
        _follow_location(conn, run, made, bb, set(), new=True)
    _write_seen(conn, run, made, bb, set())
    return "created", made, typeless


def _preset_sent(bb: BbSpool) -> tuple[str | None, str | None]:
    """BB's ``slicer_filament`` and its name as `_bb_presets` keeps them on the link."""
    return (
        _text(bb.slicer_filament),
        _text(bb.slicer_filament_name) if bb.slicer_filament else None,
    )


#: The H2C nozzle sizes a Bambuddy spool's one preset is written for (ruling R1).
BB_PRESET_NOZZLES = ("0.2", "0.4", "0.6", "0.8")


def _bb_presets(conn: Conn, spool_id: UUID, bb: BbSpool, actor: str, instance: str | None) -> bool:
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
    type gives its presets (F27, `drop_migrated`). A Bambuddy local preset ScadBuddy
    holds (`filament/sb_presets.py`, #7176) is written as its ``sb-`` ref, and a row
    holding that ref holds Bambuddy's last value; one it does not hold yet is written as
    Bambuddy's id, which the same activity's presets import then adopts (`adopt_held`,
    while ``filament_sync.presets`` pulls, #7244).

    The spool's row is locked first (after the pull's advisory lock and any tags, the
    pull's order), as `FilamentSpoolStore.set_presets` locks it, so a person's edit in
    flight is waited for and then seen (re-review 2 I-A)."""
    # Here, not at the top: `filament.presets` reads `bambuddy.resolver`.
    from scadbuddy.bambuddy.resolver import PRINTER_MODEL

    conn.execute("SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,))
    sent = _preset_sent(bb)
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
    # A Bambuddy local preset ScadBuddy holds is named by its ScadBuddy ref (#7176): a row
    # holding that ref holds what Bambuddy sent, and what Bambuddy sends is written so.
    held = by_bb_id_on(
        conn,
        [int(ref) for ref in (last[0], sent[0]) if ref is not None and ref.isdigit()],
        instance,
    )

    def as_sb(value: tuple[str | None, str | None]) -> tuple[str | None, str | None]:
        ref = value[0]
        if ref is not None and ref.isdigit() and int(ref) in held:
            return sb_ref(held[int(ref)]), value[1]
        return value

    lasts = {last, as_sb(last)}
    written = as_sb(sent) if sent[0] else None
    # Bambuddy's own rows: none, or migrated ones still holding what it sent last. A
    # person's row (not migrated) is never Bambuddy's, even when its value matches.
    ours = [
        size
        for size in BB_PRESET_NOZZLES
        if size not in rows or (last[0] is not None and size in migrated and rows[size] in lasts)
    ]
    wanted = [size for size in ours if rows.get(size) != written]
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
                (spool_id, PRINTER_MODEL, size, *as_sb(sent)),
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
    held_row: dict[str, Any] = {}

    def sb_holds(key: str, value: Any) -> bool:
        if not held_row:
            held_row.update(
                conn.execute(
                    "SELECT data_origin, added_full, tag_encoded_at FROM filament_spools"
                    " WHERE id = %s",
                    (spool_id,),
                ).fetchone()
                or {}
            )
        return key != "consumed_baseline" and _column_value(key, value) == held_row.get(key)

    before = _seeded(before, snapshot(bb), sb_holds)

    def moved_off(field: str, value: Any) -> bool:
        name = _SEEN_NAMES.get(field)
        if echo is not None and name in _WATCHED:
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
    after = _reading(bb, before, insert_id)
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
    # R-7149-6: a baseline that moved off one SB saw is BB's reset; a first reading is none.
    # R-7149-8: after a push's echo, what SB saw is the link's ``bb_seen``, not the echo.
    old_baseline = (
        echo.get("weight_used_baseline") if echo is not None else before.get("consumed_baseline")
    )
    reset = bool(
        pulls.weights and old_baseline is not None and after["consumed_baseline"] != old_baseline
    )
    if seen == before and not fresh and not reset:
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
    current = spool.effective.model_dump() | {
        "weight_locked": spool.weight_locked,
        "data_origin": spool.data_origin,
        "added_full": spool.added_full,
        "tag_encoded_at": spool.tag_encoded_at,
    }
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
    if (
        seen == before
        and not changes
        and not retag
        and reading is None
        and weight is None
        and not reset
    ):
        # Nothing taken after the push's echo: it stays the last reading.
        return False
    _synced(conn, spool_id, bb, seen, changes, reading, actor, weight=weight, ref=ref)
    if reset:
        _bb_reset(conn, spool_id, bb, actor, old_baseline)
    FilamentSpoolStore.read(conn, spool_id, check=True)
    if taken is not None:
        taken |= {_COLUMN_SEEN[c] for c in changes if c in _COLUMN_SEEN}
        if reading is not None:
            taken.add("weight_used")
    return bool(changes) or retag or reading is not None or reset


def _bb_reset(conn: Conn, spool_id: UUID, bb: BbSpool, actor: str, old: Any) -> None:
    """R-7149-6: Bambuddy's consumed-counter reset as a ``consumed_reset`` event of source
    ``bb``, once per BB change (its ref). The push never sends a ``bb`` reset back
    (`bb_sync_plan.SpoolFacts`).

    R-7149-9: Bambuddy keeps no reset time, and its ``updated_at`` moves with any later
    edit, so the reset is dated at the earlier of ``updated_at`` and the newest reading
    that still held the ``old`` baseline. A plate between that reading and the reset then
    counts as used since the reset: never a plate lost, at worst one counted twice."""
    stamp = None if bb.updated_at is None else bb.updated_at.isoformat()
    held = conn.execute(
        "SELECT max(at) AS at FROM filament_spool_events WHERE spool_id = %s"
        " AND kind = 'synced_from_bb'"
        " AND (data -> 'bb' ->> 'consumed_baseline')::float8 = %s",
        (spool_id, float(old)),
    ).fetchone()
    at = bb.updated_at
    if held is not None and held["at"] is not None:
        at = held["at"] if at is None else min(as_utc(at), held["at"])
    ledger.record(
        conn,
        spool_id,
        ledger.CONSUMED_RESET,
        source="bb",
        ref_kind=REF_KIND if stamp else None,
        ref_id=f"{bb.id}@{stamp}" if stamp else None,
        actor=actor,
        at=at,
        data={"bb_spool_id": bb.id, "bb_baseline": bb.weight_used_baseline},
    )


def _reading(bb: BbSpool, before: Mapping[str, Any], insert_id: UUID | None) -> dict[str, Any]:
    """`snapshot` as `_update` records it, ``insert_id`` the insert type BB's catalog id
    maps to. When the entry is not mapped (the read failed, or it was skipped or
    archived), the reading keeps the last id, so the pull that maps it sees the change;
    with no last id the key stays absent, so the pull that maps it reads it for the first
    time."""
    after = snapshot(bb)
    if insert_id is None and bb.core_weight_catalog_id is not None:
        if "core_weight_catalog_id" in before:
            after["core_weight_catalog_id"] = before["core_weight_catalog_id"]
        else:
            del after["core_weight_catalog_id"]
    return after


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


def _book_ref(bb: BbSpool, at: datetime) -> str:
    """The ref id of a weighing or drying `_book` books once: BB's spool and its time."""
    return f"{bb.id}/{at.isoformat()}"


def _book_refs(bb: BbSpool) -> set[tuple[str, str, str]]:
    """What `_book` would book under ``weights``: each event's kind, ref kind and ref id."""
    refs: set[tuple[str, str, str]] = set()
    if bb.last_weighed_at is not None and bb.last_scale_weight is not None:
        refs.add(("weighed", "bb_scale", _book_ref(bb, bb.last_weighed_at)))
    if bb.last_dried_at is not None:
        refs.add(("dried", "bb_dried", _book_ref(bb, bb.last_dried_at)))
    return refs


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
                ref_id=_book_ref(bb, bb.last_weighed_at),
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
                ref_id=_book_ref(bb, bb.last_dried_at),
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


def _mapped_place(conn: Conn, bb_location_id: int) -> UUID | None:
    """The SB location BB's place maps to now: open, and in no printer's place."""
    row = conn.execute(
        "SELECT id FROM filament_locations WHERE bb_location_id = %s"
        " AND archived_at IS NULL AND printer_id IS NULL",
        (bb_location_id,),
    ).fetchone()
    return None if row is None else row["id"]


def _place_ever_mapped(conn: Conn, bb_location_id: int) -> bool:
    """SB has mapped BB's place, in any state (archived and printer places included)."""
    return (
        conn.execute(
            "SELECT 1 FROM filament_locations WHERE bb_location_id = %s", (bb_location_id,)
        ).fetchone()
        is not None
    )


def _write_seen(conn: Conn, run: _Run, spool_id: UUID, bb: BbSpool, taken: set[str]) -> None:
    """R-T4-28 (decision 75): merge ``seen_row(bb)`` into the link's ``bb_seen``, at
    ``run.now``. A field goes in when the pull took it (``taken``) or Bambuddy's value is
    SB's compare value; any other keeps its old seen value, or stays out when unseen.
    R-T9-3 and R-T9-5 make two exceptions: a link with no seen location (under
    ``pulls.locations``; BB's place must map to an SB location, decision 164) or no seen
    archive state (under ``pulls.spools``) takes Bambuddy's value as seen,
    so only a later Bambuddy change moves or archives it.
    ``bb_seen_at`` moves only when ``bb_seen`` changes (review M2): a running pass takes a
    newer one as `results` writing it after its snapshot (F11)."""
    from scadbuddy.filament import bb_sync_plan as plan

    desired, _ = plan.desired_and_unmapped(conn, spool_id, run.refs, _ALL)
    seeds, stale, agree = _seen_parts(
        run,
        bb,
        desired,
        taken,
        held=lambda: _link_seen(conn, spool_id).get("location_id"),
        mapped=lambda place: _mapped_place(conn, place) is not None,
        ever_mapped=lambda place: _place_ever_mapped(conn, place),
    )
    # The seeds go first, so a seen value the link holds already wins over them.
    conn.execute(
        "UPDATE filament_bb_links SET bb_seen = n.seen, bb_seen_at = CASE WHEN"
        " n.seen IS DISTINCT FROM l.bb_seen OR l.bb_seen_at IS NULL THEN %s"
        " ELSE l.bb_seen_at END"
        " FROM filament_bb_links l, LATERAL (SELECT %s ||"
        " (coalesce(l.bb_seen, '{}'::jsonb) - CASE WHEN %s THEN 'location_id' ELSE '' END)"
        " || %s AS seen) n"
        " WHERE filament_bb_links.spool_id = %s AND l.spool_id = filament_bb_links.spool_id",
        (run.now, Jsonb(seeds), stale, Jsonb(agree), spool_id),
    )


def _seen_parts(
    run: _Run,
    bb: BbSpool,
    desired: Mapping[str, Any],
    taken: set[str],
    *,
    held: Callable[[], Any],
    mapped: Callable[[int], bool],
    ever_mapped: Callable[[int], bool],
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    """What `_write_seen` merges into the link's ``bb_seen``, in order: the seeds, whether
    the held location goes (``stale``), and the fields that agree with SB's ``desired``
    values (`bb_sync_plan.desired_and_unmapped`). ``held`` is the link's seen location,
    ``mapped`` whether BB's place maps to an open SB place in no printer's place, and
    ``ever_mapped`` whether SB has mapped it in any state; `_quiet` answers them from its
    one read, `_write_seen` with a query each."""
    from scadbuddy.filament import bb_sync_plan as plan

    seen = seen_row(bb)
    k_rows = json_form([k.model_dump() for k in bb.k_profiles])
    sb = {
        n: plan.compare_form(n, v)
        for n, v in desired.items()
        if next(plan.pendings(v), None) is None
    }
    agree = {
        n: v
        for n, v in seen.items()
        if n in taken
        or (n in sb and plan.bb_form(n, seen, k_rows) == sb[n])
        # R-7149-8: only Bambuddy writes these, so SB's seen copy is its last value.
        or (n in _PULL_SEEN_SNAPSHOT and run.pulls.of_field(_PULL_SEEN_SNAPSHOT[n]))
    }
    stale = False
    seeds = {
        n: seen[n]
        for n, on in (("location_id", run.pulls.locations), ("archived_at", run.pulls.spools))
        if on
    }
    if "location_id" in seeds:
        if seen["location_id"] is not None and not mapped(seen["location_id"]):
            del seeds["location_id"]
        place = held()
        # Decision 171: a seed of a place SB has never mapped is no seed. The merge
        # drops the link's seen value, so `bb_seen_at` moves with it (M2) and the first
        # pull where BB's place maps seeds it fresh.
        stale = place is not None and not ever_mapped(place)
    return seeds, stale, agree


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
    if not bb_archived:
        _free_taken_box(conn, run, spool_id, at)
    run.store.spools.update_on(
        conn,
        spool_id,
        m.SpoolPatch(archived=bb_archived),
        actor=run.actor,
        at=_bb_event_at(conn, spool_id, at),
    )
    return True


def _free_taken_box(conn: Conn, run: _Run, spool_id: UUID, at: datetime) -> None:
    """A restore (#7167): an archived spool keeps its place, and when that is a dry box
    another spool holds now, the restore would be `BoxFullError`. The pull never fails
    over it: the spool is moved to no place first, and the pull logs it. The spool is
    locked, then its place, as `LocationStore.move_on` does."""
    row = conn.execute(
        "SELECT location_id FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,)
    ).fetchone()
    if row is None or row["location_id"] is None:
        return
    held = occupant_on(conn, row["location_id"], but=spool_id)
    if held is None:
        return
    logger.warning(
        "BB restored spool %s, whose dry box %s holds %s: restored with no place",
        spool_id,
        row["location_id"],
        held,
    )
    LocationStore.move_on(
        conn, spool_id, None, actor=run.actor, at=_bb_event_at(conn, spool_id, at)
    )


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
        target = _mapped_place(conn, bb.location_id)
        if target is None:
            return False
    if target == row["location_id"]:
        return False
    if not new:
        seen = _link_seen(conn, spool_id)
        held = seen.get("location_id")
        if held is None and "location_id" not in seen:
            return False
        if held is not None and not _place_ever_mapped(conn, held):
            return False  # Decision 171: a never-mapped seed is absent; `_write_seen` repairs it.
        if held == bb.location_id:
            return False
    at = as_utc(bb.updated_at) or run.now
    if _sb_newer(conn, spool_id, "location_id", at):
        return False
    try:
        LocationStore.move_on(
            conn, spool_id, target, actor=run.actor, at=_bb_event_at(conn, spool_id, at)
        )
    except BoxFullError as full:
        # #7167: the pull never fails over a full dry box; the spool is left where it was.
        logger.warning(
            "BB placed spool %s in dry box %s, which holds %s: left where it was",
            spool_id,
            target,
            full.occupant,
        )
        return False
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


#: How long :func:`read_assignments` waits for Bambuddy. It runs inside an item print's
#: check, whose whole budget is 6 s (`workflows/operation_activities.py`
#: `CHECK_BUDGET_SECONDS`), so it may take at most part of it; the client's own 30 s
#: would take all of it.
ASSIGNMENTS_TIMEOUT = 2.5


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
