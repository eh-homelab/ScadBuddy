"""ScadBuddy's own spool inventory (#4643, tier B of #3818): ``native_spools``, the
records the native spool provider (`spools/spool_providers.py`) offers the overlay.

Unlike ``spool_provider_records``, which is a cache each provider's refresh rewrites,
these are the records themselves: a person keeps them (``/api/v1/native-spools``,
`api/native_spools.py`), and a native print takes what it used off them
(``native_spool_uses``, :meth:`NativeSpoolStore.settle_uses`). A record's RFID keys are
cleaned as the overlay cleans a tray's (`spools.models.clean_tray_uuid`,
`clean_tag_uid`), so a record a person types from a tray's report finds that tray's
spool; one unarchived record holds a key (the migration's partial unique indexes).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from scadbuddy.spools.models import SpoolSnapshot, clean_tag_uid, clean_tray_uuid

if TYPE_CHECKING:
    from scadbuddy.bambuddy.filaments import SpoolOption


Conn = Connection[DictRow]

#: What a tag may be typed as: hex digits, with the separators a reader prints.
_TAG_TEXT = re.compile(r"^[0-9A-Fa-f:\- ]*$")


def _tag(clean: Callable[[str | None], str | None]) -> Callable[[str | None], str | None]:
    def check(value: str | None) -> str | None:
        if value is None:
            return None
        if not _TAG_TEXT.fullmatch(value):
            raise ValueError("a tag is hex digits, as the printer reports it")
        return clean(value)

    return check


def _colour(value: str | None) -> str | None:
    return None if value is None else value.upper()


TrayUuid = Annotated[str | None, Field(max_length=64), AfterValidator(_tag(clean_tray_uuid))]
TagUid = Annotated[str | None, Field(max_length=64), AfterValidator(_tag(clean_tag_uid))]
Colour = Annotated[
    str | None,
    Field(pattern=r"^#[0-9A-Fa-f]{6}$", description="#RRGGBB"),
    AfterValidator(_colour),
]
Text = Annotated[str | None, Field(max_length=128)]
Grams = Annotated[float | None, Field(ge=0, le=100_000)]
LabelWeight = Annotated[int | None, Field(gt=0, le=100_000)]
Note = Annotated[str | None, Field(max_length=2000)]

#: Every field a person writes, in column order.
FIELDS = (
    "material",
    "colour",
    "brand",
    "name",
    "label_weight_g",
    "remaining_g",
    "tray_uuid",
    "tag_uid",
    "preset",
    "note",
)
_COLUMNS = ", ".join(("id", *FIELDS, "archived_at", "created_at", "updated_at"))


class NativeSpoolCreate(BaseModel):
    """A spool on ScadBuddy's own shelf. Every field may be left out; a tag of all zeros
    (an untagged spool's report) is stored as none."""

    model_config = ConfigDict(extra="forbid")

    material: Text = None
    colour: Colour = None
    brand: Text = None
    #: What a person calls it: the colour's name, "Galaxy Black".
    name: Text = None
    #: The filament's weight when full, as the label says.
    label_weight_g: LabelWeight = None
    remaining_g: Grams = None
    tray_uuid: TrayUuid = None
    tag_uid: TagUid = None
    #: The filament preset id it prints with (``GFA00``), as a tray's ``tray_info_idx``.
    preset: Text = None
    note: Note = None


class NativeSpoolPatch(NativeSpoolCreate):
    """Only the fields sent change (``model_fields_set``); ``null`` clears one.
    ``archived`` takes the spool off the shelf (``true``) or puts it back."""

    archived: bool | None = None


class NativeSpool(BaseModel):
    id: UUID
    material: str | None
    colour: str | None
    brand: str | None
    name: str | None
    label_weight_g: int | None
    remaining_g: float | None
    tray_uuid: str | None
    tag_uid: str | None
    preset: str | None
    note: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def snapshot(self) -> SpoolSnapshot:
        """The record as the overlay reads every provider's (`SpoolSnapshot`)."""
        return SpoolSnapshot(
            record_id=str(self.id),
            material=self.material,
            colour=self.colour,
            brand=self.brand,
            name=self.name,
            remaining_g=self.remaining_g,
            label_weight_g=self.label_weight_g,
            tray_uuid=self.tray_uuid,
            tag_uid=self.tag_uid,
            preset=self.preset,
        )


class SpoolUse(BaseModel):
    """What one slot of a LAN start uses of a native spool (``native_spool_uses``)."""

    slot_id: int
    spool_id: UUID
    grams: float


def spool_uses(spools: Mapping[int, SpoolOption], grams: Mapping[int, float]) -> list[SpoolUse]:
    """Each slot's use of the native spool in its tray, by slot: the overlay's native
    record of the slot's chosen tray (`SpoolOption.records`, found there by RFID or a
    person's tray link), and the grams the sliced plate gives the slot
    (`slicing.slice_info.plate_grams`). A slot with no native record, or that prints
    nothing, uses none."""
    uses: list[SpoolUse] = []
    for slot_id in sorted(spools):
        record = spools[slot_id].records.get("native")
        used = grams.get(slot_id, 0.0)
        if record is None or used <= 0:
            continue
        try:
            spool_id = UUID(record.record_id)
        except ValueError:
            continue
        uses.append(SpoolUse(slot_id=slot_id, spool_id=spool_id, grams=used))
    return uses


class NativeSpoolExistsError(ValueError):
    """An unarchived spool already carries the tag."""

    def __init__(self) -> None:
        super().__init__("another spool on the shelf carries that tag")


class NativeSpoolStore:
    """``native_spools`` over a pool whose connections autocommit and return dict rows
    (the settings store's, `SettingsStore.pool`, which every process that builds the
    providers has). Synchronous: callers run it in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    def list(self, *, include_archived: bool = False) -> list[NativeSpool]:
        where = "" if include_archived else " WHERE archived_at IS NULL"
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM native_spools{where} ORDER BY created_at, id"
            ).fetchall()
        return [NativeSpool.model_validate(row) for row in rows]

    def any(self) -> bool:
        """Whether any spool is on the shelf, unarchived."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM native_spools WHERE archived_at IS NULL) AS any"
            ).fetchone()
        return bool(row and row["any"])

    def get(self, spool_id: UUID) -> NativeSpool | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM native_spools WHERE id = %s",
                (spool_id,),
            ).fetchone()
        return None if row is None else NativeSpool.model_validate(row)

    def create(self, spool: NativeSpoolCreate) -> NativeSpool:
        values = [getattr(spool, name) for name in FIELDS]
        placeholders = ", ".join("%s" for _ in FIELDS)
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    f"INSERT INTO native_spools ({', '.join(FIELDS)}) VALUES ({placeholders})"
                    f" RETURNING {_COLUMNS}",
                    values,
                ).fetchone()
        except errors.UniqueViolation:
            raise NativeSpoolExistsError() from None
        assert row is not None
        return NativeSpool.model_validate(row)

    def update(self, spool_id: UUID, patch: NativeSpoolPatch) -> NativeSpool | None:
        """The fields ``patch`` sends; ``None`` for no such spool."""
        sent = patch.model_fields_set
        sets = [f"{name} = %s" for name in FIELDS if name in sent]
        values: list[Any] = [getattr(patch, name) for name in FIELDS if name in sent]
        if "archived" in sent and patch.archived is not None:
            sets.append(
                "archived_at = COALESCE(archived_at, now())"
                if patch.archived
                else "archived_at = NULL"
            )
        sets.append("updated_at = now()")
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    f"UPDATE native_spools SET {', '.join(sets)} WHERE id = %s"
                    f" RETURNING {_COLUMNS}",
                    [*values, spool_id],
                ).fetchone()
        except errors.UniqueViolation:
            raise NativeSpoolExistsError() from None
        return None if row is None else NativeSpool.model_validate(row)

    def delete(self, spool_id: UUID) -> bool:
        """Forget the record; ``False`` when there was none. Its cached copy goes at the
        provider's next refresh, as an archived one's does."""
        with self._pool.connection() as conn:
            cursor = conn.execute("DELETE FROM native_spools WHERE id = %s", (spool_id,))
        return cursor.rowcount > 0

    # -- what native prints use ------------------------------------------------------

    def record_uses(self, run_id: str, subtask_name: str, uses: Sequence[SpoolUse]) -> None:
        """What a LAN start uses, as the printer takes it. A start saved already (an
        attempt that starts again) keeps its rows; a spool deleted since is left out."""
        if not uses:
            return
        with self._pool.connection() as conn, conn.transaction():
            for use in uses:
                conn.execute(
                    "INSERT INTO native_spool_uses (run_id, subtask_name, slot_id, spool_id, grams)"
                    " SELECT %s, %s, %s, id, %s FROM native_spools WHERE id = %s"
                    " ON CONFLICT DO NOTHING",
                    (run_id, subtask_name, use.slot_id, use.grams, use.spool_id),
                )

    def settle_uses(self, run_id: str, stages: Sequence[str]) -> set[UUID]:
        """Settle ``run_id``'s uses once its print has settled: ``stages`` is how each of
        its LAN starts ended, in the order its result lists them
        (``print_runs.result.lan``, as the follow's progress reads them). A start that
        ended ``done`` takes its grams off its spools (never below 0, and not from a
        spool whose weight is not kept); every other start takes nothing. Each row is
        settled once, under its row lock, so two readers of one print take it off once.
        The spools whose weight changed."""
        with self._pool.connection() as conn, conn.transaction():
            pending = conn.execute(
                "SELECT subtask_name, slot_id, spool_id, grams FROM native_spool_uses"
                " WHERE run_id = %s AND settled_at IS NULL FOR UPDATE",
                (run_id,),
            ).fetchall()
            if not pending:
                return set()
            row = conn.execute(
                "SELECT result->'lan' AS lan FROM print_runs WHERE id = %s", (run_id,)
            ).fetchone()
            starts = row["lan"] if row is not None and isinstance(row["lan"], list) else []
            done = {
                start.get("subtask_name")
                for start, stage in zip(starts, stages, strict=False)
                if isinstance(start, dict) and stage == "done"
            }
            taken: dict[UUID, float] = {}
            for use in pending:
                applied = use["subtask_name"] in done
                conn.execute(
                    "UPDATE native_spool_uses SET settled_at = now(), applied = %s"
                    " WHERE run_id = %s AND subtask_name = %s AND slot_id = %s",
                    (applied, run_id, use["subtask_name"], use["slot_id"]),
                )
                if applied:
                    taken[use["spool_id"]] = taken.get(use["spool_id"], 0.0) + use["grams"]
            changed: set[UUID] = set()
            for spool_id, grams in taken.items():
                updated = conn.execute(
                    "UPDATE native_spools SET remaining_g = GREATEST(remaining_g - %s, 0),"
                    " updated_at = now() WHERE id = %s AND remaining_g IS NOT NULL RETURNING id",
                    (grams, spool_id),
                ).fetchone()
                if updated is not None:
                    changed.add(spool_id)
        return changed
