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
from pydantic import AwareDatetime, BaseModel, Field

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

#: How the pull found the SB row of a Bambuddy supplier or location (Task 9,
#: `suppliers.SuppliersStore.link_bb_on`, `locations.LocationStore.link_bb_on`): linked
#: already, linked now by its name, or made.
BbLinked = Literal["found", "linked", "made"]

#: What one write does. ``probe`` asks, with ``GET`` only and an empty body, whether
#: Bambuddy still holds a linked row a complete listing lacks: a spool's ``spool_row(bb_id)``
#: (Decision 13), a filament entry's ``GET`` by id (a 404 there confirmed by its listing),
#: any other row's listing read again (#6688, decisions 187-192, 200 and 201).
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
    partial spool listing, a listing not read, a row whose probe failed in this pass,
    R-6688-5). Neither sets ``more`` (R-T4-1).
    ``unmapped_k``: K values left out for a printer with no Bambuddy id (Decision 14).
    ``conflicts``: fields of suppliers, locations, insert types and filament entries that
    both sides changed since SB last saw them, so neither side's value is pushed
    (R-T4-33). Also whether the snapshot's instance is not the links' one (Decision 12), and the
    directions and timer interval the plan read. ``pass_id``: made by the route for this plan
    round; the worker sends it back with the round's results (R-T6-5). ``unconfirmed``:
    the linked rows no spool of this round names by their Bambuddy id (their probe is
    planned, failed in this pass, or was cut by the cap), which the worker sends back
    with the results so a spool's compare names none of them either (R-6688-11).

    This model and `ResultsRequest` forbid extra keys, so ``unconfirmed`` crosses builds
    only when it is empty: both leave the key out then (`without_empty_unconfirmed`), and
    a build before it takes the body. A plan or results body that names a row is refused
    by such a build (the pass fails, and the next signal or the timer runs another), so
    roll the API and the print worker together, the API first."""

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
    unconfirmed: list[tuple[Kind, UUID]] = Field(default_factory=list)


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
    count once (R-T6-5). ``unconfirmed``: the plan's (`SyncPlan`), which a worker from
    before R-6688-11 does not send; `apply_results` then takes the round's probes only."""

    instance: str
    writes: list[BbWrite]
    results: list[BbWriteResult]
    pass_id: str | None = None
    unconfirmed: list[tuple[Kind, UUID]] = Field(default_factory=list)


def without_empty_unconfirmed(model: SyncPlan | ResultsRequest) -> dict[str, Any]:
    """``model`` as JSON with no ``unconfirmed`` key when it is empty, so a build that
    does not know the key takes the body (R-6688-11)."""
    dumped = model.model_dump(mode="json")
    if not dumped["unconfirmed"]:
        del dumped["unconfirmed"]
    return dumped


#: The pass activity's name (`bb_sync_run.FilamentSyncActivities`), as the workflow
#: (`workflows/filament_sync.py`) schedules it.
SYNC_PASS_ACTIVITY = "filament_bb_sync_pass"
#: The timer's interval a pass reports when it made no plan, unless told another.
DEFAULT_INTERVAL_S = 300.0


#: What started a pass (#6905): ``timer`` (the workflow's interval ran out), ``change`` (a
#: write's nudge), ``full`` (a nudge that asked for a full pass: "Sync now", a reset of the
#: links, the API's start), ``more`` (the pass before it left writes, so it ran at once).
PassTrigger = Literal["timer", "change", "full", "more"]


class SyncPassInput(BaseModel, extra="forbid"):
    """The activity's input: the Bambuddy instance the workflow serves, whether the pass
    is a full one (Decision 7), and the interval the workflow waits now, which a pass
    that made no plan reports back unchanged. ``trigger``: what started it (#6905);
    ``None`` from a history recorded before #6905."""

    instance: str
    full: bool
    interval_s: float = DEFAULT_INTERVAL_S
    trigger: PassTrigger | None = None


#: Why a pass did nothing: no Bambuddy is configured, or no data type pushes (R-T8-2).
SkipReason = Literal["no_bambuddy", "nothing_pushes"]


class SyncPassResult(BaseModel, extra="forbid"):
    """What one pass did. ``more``: writes were left, so the next pass runs at once
    (Decision 11). ``skipped``: why it did nothing. ``error``: a failed pass's
    ``error_label``; `record_pass` sets ``last_error`` from it, and ``last_ok_at`` only
    when it is ``None`` (Decision 19, F7). ``conflicts``: the largest ``SyncPlan.conflicts``
    of the pass's rounds (R-T4-33). ``unmapped_k``: the largest ``SyncPlan.unmapped_k`` of
    its rounds, for the same reason (decision 58). ``started_at``: when the pass began, by
    the worker's clock; `record_pass` stores it as ``last_pass_started_at`` (#6836). A
    result from a build before it has none, and the record falls back to the database's
    ``now()``.

    This model forbids extra keys, so ``started_at`` crosses builds in one direction only.
    An API from before #6836 refuses a worker's result that carries it (422), so the pass
    is lost until the API rolls; `RemoteFilamentSync.record_pass` sends no key at all when
    it is ``None``, so a newer API takes an older or start-less worker's result. Roll the
    API before the print worker: the manifests are separate apps and nothing in the
    README's "Deploying" orders them, so during a skew a pass can be lost, and the next
    signal or the timer runs another.

    #6905: ``pass_id`` names the pass's row in ``filament_bb_sync_passes`` (the worker
    makes it when the pass starts, so a retried record adds no second row), ``instance``
    is the Bambuddy the workflow serves, and ``trigger`` what started the pass. Each is
    ``None`` from an older worker, and `RemoteFilamentSync.record_pass` leaves it out then,
    as it does ``started_at``; the record makes an id of its own for a result with
    none."""

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
    started_at: AwareDatetime | None = None
    pass_id: UUID | None = None
    instance: str | None = None
    trigger: PassTrigger | None = None


#: The data types the engine pushes (`tags` is #6264's drain, Decision 6).
PUSHED_TYPES = ("spools", "weights", "presets", "suppliers", "locations")


def pushes_anything(sync: FilamentSync) -> bool:
    """True when some data type the engine pushes moves ScadBuddy's data into Bambuddy
    (R-T8-2). ``tags`` does not count: tags reach Bambuddy through the refresh's tag
    drain, not through this pass (Decision 6)."""
    return any(pushes(getattr(sync, name)) for name in PUSHED_TYPES)


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
        unconfirmed: Sequence[tuple[Kind, UUID]] = (),
    ) -> ResultsReport: ...

    async def record_pass(self, result: SyncPassResult) -> None: ...


class SyncError(BaseModel, extra="forbid"):
    """One `filament_bb_push_errors` row: an item whose last write failed, or was held
    back as ``name_taken`` (another Bambuddy row has its name; never sent). ``label`` names
    the item as the UI shows it (R-T11-1): a spool's ``#N``, a spool type's code, any other
    row's name; ``None`` when the item is gone."""

    kind: Kind
    sb_id: UUID
    label: str | None
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
    last_pass_started_at: datetime | None
    last_ok_at: datetime | None
    last_error: str | None
    counts: dict[str, int]
    errors: list[SyncError]
    directions: FilamentSync
    skipped: SkipReason | None
    #: The links belong to another Bambuddy than the configured one, so the sync is
    #: stopped until a person resets them (Decision 12, #6773).
    instance_changed: bool = False


class SyncPass(BaseModel, extra="forbid"):
    """One ``filament_bb_sync_passes`` row (#6905): one pass of the sync, failed ones too.
    ``started_at`` is by the worker's clock and ``ended_at`` the database's, when the pass
    was recorded. ``trigger`` is ``None`` for a pass whose worker did not say. ``error`` is
    a label (`bb_sync_run.failure_label`), never a message; ``None`` when it worked.
    ``instance_changed``: the links belong to another Bambuddy, or the pass's instance is
    not the configured one, so it wrote nothing and the workflow ended."""

    id: UUID
    instance: str | None
    trigger: PassTrigger | None
    started_at: datetime
    ended_at: datetime
    written: int
    failed: int
    deferred: int
    conflicts: int
    unmapped_k: int
    skipped: SkipReason | None
    instance_changed: bool
    error: str | None


class SyncPassPage(BaseModel, extra="forbid"):
    """``GET /filament/sync/passes``: a page of passes, newest first. ``next_cursor`` is
    opaque; ``None`` on the last page."""

    passes: list[SyncPass]
    next_cursor: str | None


class SyncRun(BaseModel, extra="forbid"):
    """``POST /filament/sync/run``'s answer: the full pass was asked for. ``requested_at`` is
    the API's clock when it asked; a pass whose ``last_pass_started_at`` is at or after it
    ran for this request (#6836)."""

    started: Literal[True] = True
    requested_at: AwareDatetime


class LinksResetCounts(BaseModel, extra="forbid"):
    """What a reset of the Bambuddy links deletes (#6773), or deleted: the spool links
    (`filament_bb_links`), the supplier, location, insert type and spool type links (each
    row's Bambuddy id, its hashes and its seen copy), the open create intents
    (`filament_bb_creates`), the error rows (`filament_bb_push_errors`) and the Bambuddy
    spools the pull ignores (`filament_bb_ignored`). No ScadBuddy row is deleted."""

    spools: int = 0
    suppliers: int = 0
    locations: int = 0
    insert_types: int = 0
    filaments: int = 0
    intents: int = 0
    errors: int = 0
    ignored: int = 0

    def links(self) -> int:
        """Every link of every kind."""
        return self.spools + self.suppliers + self.locations + self.insert_types + self.filaments


class LinksResetPreview(BaseModel, extra="forbid"):
    """``GET /filament/sync/reset-links``: the instance the links belong to (what
    ``confirm_instance`` must name), the configured one, and what a reset would delete.
    ``same_bambuddy``: the configured Bambuddy holds the linked spools as ScadBuddy last
    saw them (`bb_sync_results.same_bambuddy`, #6845), so a reset with ``expect_kept``
    keeps every link and deletes nothing. ``bambuddy_unreadable``: the configured
    Bambuddy could not be read, so ``same_bambuddy`` is false and only the delete is
    offered (Decision 198)."""

    instance: str | None
    configured_instance: str | None
    counts: LinksResetCounts
    same_bambuddy: bool
    bambuddy_unreadable: bool = False


class LinksResetRequest(BaseModel, extra="forbid"):
    """``POST /filament/sync/reset-links``'s body: the instance the person confirms the
    links belong to, so a stale page cannot reset another Bambuddy's links.
    ``expect_kept`` (#6845 R-6845-6): the preview's ``same_bambuddy``. True keeps the
    links only while the configured Bambuddy is still proven the links' own, and is
    refused ``proof_changed`` otherwise, deleting nothing; false is the confirmed
    delete, which reads no Bambuddy."""

    confirm_instance: str = Field(min_length=1, max_length=64)
    expect_kept: bool = False


class LinksResetDone(BaseModel, extra="forbid"):
    """``POST /filament/sync/reset-links``'s answer: the instance whose links were
    reset, whether they were ``kept`` (the configured Bambuddy is the same one, #6845:
    nothing deleted, the links now belong to it), and what was deleted."""

    instance: str
    kept: bool
    deleted: LinksResetCounts
