"""The wire models of the ScadBuddy-to-Bambuddy sync (#6265, plan
`docs/superpowers/plans/2026-11-18-filament-bb-sync.md`), and `seen_row`, the one shape of
a spool's ``bb_seen`` (`migrations/20261118T2112Z_filament_bb_sync.sql`).

The print worker reads Bambuddy into a `BbSnapshot`, sends it to the plan route as a
`PlanRequest`, makes the `SyncPlan`'s `BbWrite` calls, sends their `BbWriteResult` rows to
``results``, and reports the pass as a `SyncPassResult`. `SyncStatus` is what a person and
the agent read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from pydantic_core import to_jsonable_python

from scadbuddy.bambuddy.models import (
    BbLocation,
    BbSupplier,
    CatalogFilament,
    SpoolCatalogEntry,
    as_utc,
)
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament.colour import canonical_colours, canonical_rgba

#: The SB rows the sync writes to Bambuddy, as `filament_bb_creates.kind` and
#: `filament_bb_push_errors.kind` name them.
Kind = Literal["spool", "supplier", "location", "insert_type", "filament"]

#: What one write does. ``probe`` is Decision 13's ``GET spool_row(bb_id)``, with an
#: empty body.
Op = Literal[
    "create", "update", "archive", "restore", "k_profiles", "presets", "suppliers", "probe"
]

#: A spool's fields the sync carries (Decision 14), by Bambuddy's names: ``spools``, then
#: ``weights``, ``presets``, ``suppliers`` and ``locations``. ``archived_at`` is the
#: archive state. The K-profiles are kept apart, in `filament_bb_links.bb_k_profiles`.
SEEN_FIELDS: tuple[str, ...] = (
    "material",
    "subtype",
    "brand",
    "category",
    "effect_type",
    "color_name",
    "rgba",
    "extra_colors",
    "label_weight",
    "core_weight",
    "core_weight_catalog_id",
    "nozzle_temp_min",
    "nozzle_temp_max",
    "note",
    "cost_per_kg",
    "low_stock_threshold_pct",
    "material_number",
    "archived_at",
    "weight_used",
    "weight_locked",
    "last_dried_at",
    "slicer_filament",
    "slicer_filament_name",
    "suppliers",
    "location_id",
)

#: A spool's supplier link as the sync compares it: what ``PUT …/suppliers`` sends.
_SUPPLIER_FIELDS = frozenset(
    {"supplier_id", "supplier_article_number", "quoted_price_per_kg", "is_purchase_source"}
)


#: The `SEEN_FIELDS` for which Bambuddy gives a float, so ``0`` and ``0.0`` are one value.
_FLOAT_FIELDS = frozenset({"cost_per_kg", "weight_used"})


def seen_value(field: str, value: object) -> object:
    """An SB ``value`` for the `SEEN_FIELDS` name ``field``, in exactly the JSON form
    `seen_row` stores for it: a `Decimal` or an int of a float field is a float, a
    datetime is UTC ISO text ending in ``Z``, ``rgba`` is ``#RRGGBBAA``, ``None`` stays
    ``None``. The plan compares and hashes SB's side through this, never raw (M2 of the
    Task 3 review). The archive state is compared as ``archived_at is None``, not by time."""
    if value is None:
        return None
    if field in _FLOAT_FIELDS and isinstance(value, Decimal | int):
        return float(value)
    if field == "rgba" and isinstance(value, str):
        return canonical_rgba(value)
    if isinstance(value, datetime):
        value = as_utc(value).astimezone(UTC)
    return to_jsonable_python(value)


def seen_row(row: BbSpool) -> dict[str, Any]:
    """Bambuddy's spool ``row`` as a link's ``bb_seen`` (F5): the `SEEN_FIELDS` only,
    keyed by BB's names, as JSON (times as ISO text). ``rgba`` and ``extra_colors`` are in
    the canonical form the plan hashes and compares (Decision 14, F17): ``#RRGGBBAA``
    through `normalise_rgba`, ``extra_colors`` a list of them; the supplier links keep
    what ``PUT …/suppliers`` sends, by ``supplier_id``.

    `bambuddy_pull.snapshot` is another shape, for the ``synced_from_bb`` event only."""
    seen = row.model_dump(mode="json", include=set(SEEN_FIELDS))
    seen["rgba"] = canonical_rgba(row.rgba)
    seen["extra_colors"] = canonical_colours(row.extra_colors)
    seen["suppliers"] = sorted(
        (
            {k: v for k, v in s.model_dump(mode="json").items() if k in _SUPPLIER_FIELDS}
            for s in row.suppliers
        ),
        key=lambda s: s["supplier_id"],
    )
    return seen


class BbSnapshot(BaseModel, extra="forbid"):
    """What one pass read from Bambuddy. A ``None`` list was not read, which is never the
    same as an empty one. ``spools`` are BB's raw rows; a consumer parses them with
    `bambuddy_pull.parse_rows`, and ``spools_complete`` is False when any of them did not
    parse (then no spool is unlinked for missing, Decision 13). ``full``: a timer or "Sync
    now" pass, which ignores the hashes (Decision 7). ``instance``: the Bambuddy it was
    read from (Decision 12)."""

    instance: str
    full: bool
    spools: list[dict[str, Any]]
    spools_complete: bool
    suppliers: list[BbSupplier] | None
    locations: list[BbLocation] | None
    catalog: list[SpoolCatalogEntry] | None
    filaments: list[CatalogFilament] | None
    read_at: datetime


class BbWrite(BaseModel, extra="forbid"):
    """One Bambuddy call a plan asks for: ``key`` names it in `BbWriteResult`; ``bb_id``
    is the BB row's id (``None`` for a create); ``hashes`` are the field hashes `results`
    stores once Bambuddy answers (Decision 7)."""

    key: str
    kind: Kind
    sb_id: UUID
    op: Op
    bb_id: int | None
    body: dict[str, Any] | list[Any]
    hashes: dict[str, str]


class BbWriteResult(BaseModel, extra="forbid"):
    """What Bambuddy answered to one `BbWrite`: its row (``answer``) when ``ok``, else the
    ``error`` label and the HTTP ``status`` when it had one."""

    key: str
    ok: bool
    bb_id: int | None = None
    answer: dict[str, Any] | None = None
    error: str | None = None
    status: int | None = None


class SyncPlan(BaseModel, extra="forbid"):
    """The writes of one plan round, how many items it left for later (``deferred``,
    Decisions 10 and 11), whether the snapshot's instance is not the links' one
    (Decision 12), and the directions and timer interval the plan read."""

    writes: list[BbWrite]
    deferred: int
    instance_changed: bool = False
    directions: FilamentSync
    interval_s: float


class PlanRequest(BaseModel, extra="forbid"):
    """The plan route's body: the snapshot, and at most how many writes to plan (the
    pass's ``200 - written``, Decision 11, F10)."""

    snapshot: BbSnapshot
    limit: int = Field(ge=1, le=200)


class SyncPassResult(BaseModel, extra="forbid"):
    """What one pass did. ``more``: writes were left, so the next pass runs at once
    (Decision 11). ``skipped``: why it did nothing. ``error``: a failed pass's
    ``error_label``; `record_pass` sets ``last_error`` from it, and ``last_ok_at`` only
    when it is ``None`` (Decision 19, F7)."""

    written: int
    failed: int
    deferred: int
    more: bool
    instance_changed: bool
    interval_s: float
    skipped: Literal["no_bambuddy", "nothing_pushes"] | None = None
    error: str | None = None


class SyncError(BaseModel, extra="forbid"):
    """One `filament_bb_push_errors` row: an item whose last write failed."""

    kind: Kind
    sb_id: UUID
    error: str
    attempts: int
    at: datetime


class SyncStatus(BaseModel, extra="forbid"):
    """The sync's state (Decision 19): the instance the links belong to, the last pass and
    the last clean one, the last pass's error and counts, the items whose last write
    failed, and the directions."""

    instance: str | None
    last_pass_at: datetime | None
    last_ok_at: datetime | None
    last_error: str | None
    counts: dict[str, int]
    errors: list[SyncError]
    directions: FilamentSync
