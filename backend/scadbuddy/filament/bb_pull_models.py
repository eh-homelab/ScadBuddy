"""The Bambuddy pull's wire (#6772, plan `docs/superpowers/plans/2026-11-18-filament-bb-sync.md`
Decisions 172-180): what the print worker read of Bambuddy, sent to the CRUD service, which
applies it to SB's tables and makes no Bambuddy call.

The API starts ``FilamentBbPull`` (`workflows/filament_pull.py`) on the ``bambuddy`` queue
with a `PullStart`; the print worker's activity (`filament/bb_pull_run.py`) reads Bambuddy
and sends what it read through `BbPullApi`: `RemoteBbPull` (`crud/print.py`, the routes
``…/print/filament/pull*``, area ``filament-pull``) on the print worker, `LocalBbPull`
(`filament/bb_pull_apply.py`) where the API serves the queue itself. Every body names the
Bambuddy instance it read; one that is not the configured Bambuddy's is refused
(`PullInstanceChangedError`, a 409 ``instance_changed`` on the route), so a read of an old
URL never writes SB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.bambuddy.models import (
    Archive,
    BbLocation,
    BbSupplier,
    SpoolAssignment,
    SpoolCatalogEntry,
    SpoolUsage,
)
from scadbuddy.core import settings as core_settings
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import FilamentSync

FILAMENT_BB_PULL_WORKFLOW = "FilamentBbPull"
PULL_READ_ACTIVITY = "filament_bb_pull_read"

#: The routes refuse a body past this many bytes (413) before they read it.
PULL_BODY_BYTES_MAX = 32 * 1024 * 1024
#: At most this many rows of one listing in a body (422 past it).
ROWS_MAX = 20_000
#: Bambuddy's archives a usage booking matches: `bb_pull_run._read_archives` reads at most
#: `linking.MAX_ARCHIVE_PAGES` pages of `linking.ARCHIVE_PAGE`.
ARCHIVES_MAX = 2_000
#: The reason of the routes' 409 for a read of another Bambuddy than the configured one.
INSTANCE_CHANGED = "instance_changed"

Instance = Annotated[str, Field(pattern=r"^[0-9a-f]{12}$")]
Actor = Annotated[str, Field(min_length=1, max_length=256)]


#: The snapshot fields of a data type other than ``spools`` (#6265 Decision 14): each is
#: taken, and recorded as seen, only while its type pulls.
TYPE_FIELDS: dict[str, str] = {
    "remaining_g": "weights",
    "weight_locked": "weights",
    "tag_uid": "tags",
    "tray_uuid": "tags",
}


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
        return bool(getattr(self, TYPE_FIELDS.get(field, "spools")))

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


class PullInstanceChangedError(Exception):
    """The body's Bambuddy is not the configured one: nothing is written."""


class PullStart(BaseModel):
    """``FilamentBbPull``'s input. ``pulls`` is None when no data type pulls: then only the
    Home Assistant states are read (``ha``). ``reconcile`` sets the untagged trays right by
    Bambuddy's assignments after the pull (the Schedule's pull, not an import). ``strict``
    is a person's import: a Bambuddy refusal of the spool listing, or a Bambuddy URL changed
    since the start, comes back as `PullOutcome.error`, for the route to answer as the
    import answered before #6772; any other failure fails the activity."""

    model_config = ConfigDict(extra="forbid")

    instance: Instance
    actor: Actor
    pulls: Pulls | None
    ha: bool = False
    reconcile: bool = False
    strict: bool = False


class PullProblem(BaseModel):
    """An `ApiError`'s problem, carried back to the import route."""

    status: int
    detail: str
    title: str | None = None
    type: str = "about:blank"
    extensions: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def of(cls, error: ApiError) -> PullProblem:
        return cls(
            status=error.status,
            detail=error.detail,
            title=error.title,
            type=error.type,
            extensions=dict(error.extensions),
        )

    def error(self) -> ApiError:
        return ApiError(
            self.status, self.detail, title=self.title, type_=self.type, **self.extensions
        )


class PullOutcome(BaseModel):
    """What one ``FilamentBbPull`` did: ``pulled`` (with the report), ``skipped`` or
    ``failed``; ``error`` only for a strict start (an import) that Bambuddy refused."""

    outcome: Literal["pulled", "skipped", "failed"]
    report: PullReport | None = None
    error: PullProblem | None = None


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance: Instance


class PullBody(_Body):
    """What the worker read: BB's spool rows as Bambuddy sent them (each validated here,
    `bambuddy_pull.parse_rows`, a malformed one counted ``invalid``), its usage rows
    (``usage_failed``: the read failed, so no archived BB spool is made), its suppliers and
    locations (None: not read), and its spool catalog (None: the read failed)."""

    actor: Actor
    pulls: Pulls
    spools: list[Any] = Field(max_length=ROWS_MAX)
    usage: list[SpoolUsage] = Field(default_factory=list, max_length=ROWS_MAX)
    usage_failed: bool = False
    suppliers: list[BbSupplier] | None = Field(default=None, max_length=ROWS_MAX)
    locations: list[BbLocation] | None = Field(default=None, max_length=ROWS_MAX)
    catalog: list[SpoolCatalogEntry] | None = Field(default=None, max_length=ROWS_MAX)


class PullApplied(BaseModel):
    """The pull's report, and the date from which Bambuddy's archives must be read for
    the usage rows that would book and are not booked yet (None: none, or the check
    failed, logged), as `read_and_pull` read them before #6772."""

    report: PullReport
    archives_since: date | None = None


class UsageBody(_Body):
    """BB's usage rows to book on linked spools, with the archives read for them (only
    ``id``, ``printer_id`` and ``completed_at`` are used, `bambuddy_usage.match_archive`)."""

    actor: Actor
    usage: list[SpoolUsage] = Field(max_length=ROWS_MAX)
    archives: list[Archive] = Field(default_factory=list, max_length=ARCHIVES_MAX)


class UsageBooked(BaseModel):
    booked: int
    unlinked: int


class HaBody(_Body):
    """Bambuddy's Home Assistant states by entity id; None when the read failed, so every
    Home Assistant sensor on a live place is marked unreachable."""

    states: Annotated[dict[str, str | None], Field(max_length=ROWS_MAX)] | None


class TraysBody(_Body):
    """Bambuddy's tray assignments, to set right the untagged trays (#6713)."""

    assignments: list[SpoolAssignment] = Field(max_length=ROWS_MAX)


class TraysReconciled(BaseModel):
    moved: int


class BbPullApi(Protocol):
    """SB's side of the pull. Each call raises `PullInstanceChangedError` for a body of
    another Bambuddy than the configured one."""

    async def apply(self, body: PullBody) -> PullApplied: ...

    async def book_usage(self, body: UsageBody) -> UsageBooked: ...

    async def ha_states(self, body: HaBody) -> None: ...

    async def reconcile_trays(self, body: TraysBody) -> int: ...
