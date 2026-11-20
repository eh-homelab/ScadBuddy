"""The wire models of the ScadBuddy-to-Bambuddy sync (#6265, plan
`docs/superpowers/plans/2026-11-18-filament-bb-sync.md`), and `seen_row`, the one shape of
a spool's ``bb_seen`` (`migrations/20261118T2112Z_filament_bb_sync.sql`).

The print worker reads Bambuddy into a `BbSnapshot`, sends it to the plan route as a
`PlanRequest`, makes the `SyncPlan`'s `BbWrite` calls, sends their `BbWriteResult` rows to
``results``, and reports the pass as a `SyncPassResult`. `SyncStatus` is what a person and
the agent read.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from psycopg import Connection
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.models import (
    BbLocation,
    BbSupplier,
    CatalogFilament,
    SpoolCatalogEntry,
    as_utc,
)
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.core.settings import FilamentSync, pushes
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


#: The money and weight fields, as `SEEN_FIELDS` names and as a supplier link's price key:
#: a whole number in them is a float, so ``0`` and ``0.0`` are one value on both sides.
_FLOAT_FIELDS = frozenset(
    {
        "cost_per_kg",
        "label_weight",
        "core_weight",
        "weight_used",
        "quoted_price_per_kg",
    }
)


def forget_sync(conn: Connection[Any], kind: Kind, sb_id: UUID) -> None:
    """R-T3-5: an archived SB row of ``kind`` takes its create intent and its error row
    out of the sync, on ``conn``'s transaction, so the status no longer shows its error.
    The plan closes such an intent anyway (R-T4-20). A spool keeps its intent (its
    archive deletes the error row only): a restore adopts the row a lost create made."""
    for table in ("filament_bb_creates", "filament_bb_push_errors"):
        conn.execute(f"DELETE FROM {table} WHERE kind = %s AND sb_id = %s", (kind, sb_id))


def _amount(key: str, value: object) -> object:
    if key in _FLOAT_FIELDS and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def json_form(value: object) -> object:
    """``value`` in the one JSON form of a ``bb_seen`` value, item by item through lists and
    dicts: a `Decimal` is a float, a datetime is UTC ISO text ending in ``Z`` (a naive one
    counts as UTC), a date is ISO text, a UUID is text, an Enum is its value, anything else
    stays. `seen_row` and `seen_value` both use it, so the stored form and SB's form cannot
    differ."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return as_utc(value).astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return json_form(value.value)
    if isinstance(value, list | tuple):
        return [json_form(v) for v in value]
    if isinstance(value, dict):
        return {k: json_form(v) for k, v in value.items()}
    return value


def _suppliers_form(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The supplier links as the sync compares them: all four keys of what ``PUT
    …/suppliers`` sends (a missing one is ``None``), sorted by ``supplier_id``."""
    forms: list[dict[str, Any]] = [
        {k: _amount(k, json_form(link.get(k))) for k in sorted(_SUPPLIER_FIELDS)} for link in links
    ]
    return sorted(forms, key=lambda r: r["supplier_id"])


def seen_value(field: str, value: object) -> object:
    """An SB ``value`` for the `SEEN_FIELDS` name ``field``, in exactly the form `seen_row`
    stores for it (`json_form`; ``rgba`` and ``extra_colors`` canonical; a money or weight
    field's int a float; the supplier links as `_suppliers_form`). The plan compares and hashes SB's
    side through this, never raw (M2 of the Task 3 review). The archive state is compared
    as ``archived_at is None``, not by time."""
    if field == "suppliers":
        links = [] if value is None else list(cast("list[Any]", value))
        return _suppliers_form(cast("list[dict[str, Any]]", links))
    if value is None:
        return None
    if field == "rgba" and isinstance(value, str):
        return canonical_rgba(value)
    if field == "extra_colors":
        text = ",".join(value) if isinstance(value, list | tuple) else value
        return canonical_colours(text if isinstance(text, str) else None)
    return _amount(field, json_form(value))


def seen_row(row: BbSpool) -> dict[str, Any]:
    """Bambuddy's spool ``row`` as a link's ``bb_seen`` (F5): the `SEEN_FIELDS` only,
    keyed by BB's names, in `json_form` (times as UTC ISO text). ``rgba`` and
    ``extra_colors`` are in the canonical form the plan hashes and compares (Decision 14,
    F17): ``#RRGGBBAA``, ``extra_colors`` a list of them; the supplier links keep what
    ``PUT …/suppliers`` sends, by ``supplier_id``.

    `bambuddy_pull.snapshot` is another shape, for the ``synced_from_bb`` event only."""
    raw = row.model_dump(include=set(SEEN_FIELDS))
    return {k: seen_value(k, v) for k, v in raw.items()}


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
    """The writes of one plan round, and the items it wrote nothing for. ``deferred``: they
    wait for a write planned earlier in this pass, which a later round of it can clear
    (Decisions 10 and 11), or the write cap cut their write; only these set the pass's
    ``more``. A retry the cap cut (an item refused in an earlier pass) is not counted
    (R-T4-15).
    ``failed``: Bambuddy refused the item's last write; it is tried again in a later pass,
    a create only on a full one. ``blocked``: a read this pass did not get holds it (a
    partial spool listing, a listing not read). Neither sets ``more`` (R-T4-1).
    ``unmapped_k``: K values left out for a printer with no Bambuddy id (Decision 14).
    ``conflicts``: fields of suppliers, locations, insert types and filament entries that
    both sides changed since SB last saw them, so neither side's value is pushed
    (R-T4-33). Also whether the snapshot's instance is not the links' one (Decision 12), and the
    directions and timer interval the plan read. ``pass_id``: made by the route for this plan
    round; the worker sends it back with the round's results (R-T6-5)."""

    writes: list[BbWrite]
    deferred: int
    failed: int = 0
    blocked: int = 0
    unmapped_k: int = 0
    conflicts: int = 0
    instance_changed: bool = False
    directions: FilamentSync
    interval_s: float
    pass_id: str | None = None


class PlanRequest(BaseModel, extra="forbid"):
    """The plan route's body: the snapshot, and at most how many writes to plan (the
    pass's ``200 - written``, Decision 11, F10)."""

    snapshot: BbSnapshot
    limit: int = Field(ge=1, le=200)


class ResultsReport(BaseModel, extra="forbid"):
    """What `apply_results` did: the items it linked, the other items whose write it
    recorded, the items whose write failed, and the links a 404 dropped. An ignored
    result and an ``ok`` probe (it writes nothing to Bambuddy) count in none of them."""

    linked: int = 0
    updated: int = 0
    failed: int = 0
    unlinked: int = 0


class ResultsRequest(BaseModel, extra="forbid"):
    """The results route's body: the Bambuddy the answers came from, the writes of the
    plan round (`apply_results` checks each against the links, the plan is not kept) and
    what Bambuddy answered to each. ``pass_id``: the plan's, so results sent again
    count once (R-T6-5)."""

    instance: str
    writes: list[BbWrite]
    results: list[BbWriteResult]
    pass_id: str | None = None


#: The pass activity's name (`bb_sync_run.FilamentSyncActivities`), as the workflow
#: (`workflows/filament_sync.py`) schedules it.
SYNC_PASS_ACTIVITY = "filament_bb_sync_pass"
#: The timer's interval a pass reports when it made no plan, unless told another.
DEFAULT_INTERVAL_S = 300.0


class SyncPassInput(BaseModel, extra="forbid"):
    """The activity's input: the Bambuddy instance the workflow serves, whether the pass
    is a full one (Decision 7), and the interval the workflow waits now, which a pass
    that made no plan reports back unchanged."""

    instance: str
    full: bool
    interval_s: float = DEFAULT_INTERVAL_S


#: Why a pass did nothing: no Bambuddy is configured, or no data type pushes (R-T8-2).
SkipReason = Literal["no_bambuddy", "nothing_pushes"]


class SyncPassResult(BaseModel, extra="forbid"):
    """What one pass did. ``more``: writes were left, so the next pass runs at once
    (Decision 11). ``skipped``: why it did nothing. ``error``: a failed pass's
    ``error_label``; `record_pass` sets ``last_error`` from it, and ``last_ok_at`` only
    when it is ``None`` (Decision 19, F7). ``conflicts``: the largest ``SyncPlan.conflicts``
    of the pass's rounds (R-T4-33). ``unmapped_k``: the largest ``SyncPlan.unmapped_k`` of
    its rounds, for the same reason (decision 58)."""

    written: int
    failed: int
    deferred: int
    more: bool
    conflicts: int = 0
    unmapped_k: int = 0
    instance_changed: bool
    interval_s: float
    skipped: SkipReason | None = None
    error: str | None = None


def pushes_anything(sync: FilamentSync) -> bool:
    """True when some data type moves ScadBuddy's data into Bambuddy (R-T8-2)."""
    return any(pushes(getattr(sync, name)) for name in FilamentSync.model_fields)


def skip_reason(bambuddy_url: str | None, sync: FilamentSync) -> SkipReason | None:
    """Why a pass started now would be ``skipped``, by the checks `run_pass` and the
    nudge make: no ``bambuddy_url``, else no data type that pushes. ``None`` when a pass
    would run."""
    if not bambuddy_url:
        return "no_bambuddy"
    if not pushes_anything(sync):
        return "nothing_pushes"
    return None


class FilamentSyncApi(Protocol):
    """What the pass reads of SB and writes to it (#6265 Task 6): `LocalFilamentSync` in
    the API's process, `RemoteFilamentSync` (the CRUD routes) in the print worker."""

    async def directions(self) -> FilamentSync: ...

    async def plan(self, snap: BbSnapshot, limit: int) -> SyncPlan: ...

    async def results(
        self,
        instance: str,
        writes: Sequence[BbWrite],
        results: Sequence[BbWriteResult],
        pass_id: str | None,
    ) -> ResultsReport: ...

    async def record_pass(self, result: SyncPassResult) -> None: ...


class SyncError(BaseModel, extra="forbid"):
    """One `filament_bb_push_errors` row: an item whose last write failed, or was held
    back as ``name_taken`` (another Bambuddy row has its name; never sent)."""

    kind: Kind
    sb_id: UUID
    error: str
    attempts: int
    at: datetime


class SyncStatus(BaseModel, extra="forbid"):
    """The sync's state (Decision 19): the instance the links belong to, the last pass and
    the last clean one, the last pass's error and counts, the items whose last write
    failed or was held back, and the directions. ``skipped`` is `skip_reason` of the
    settings now: why no pass runs (the nudge starts none), else ``None``."""

    instance: str | None
    last_pass_at: datetime | None
    last_ok_at: datetime | None
    last_error: str | None
    counts: dict[str, int]
    errors: list[SyncError]
    directions: FilamentSync
    skipped: SkipReason | None


class SyncRun(BaseModel, extra="forbid"):
    """``POST /filament/sync/run``'s answer: the full pass was asked for."""

    started: Literal[True] = True
