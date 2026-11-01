"""ScadBuddy's own spool inventory (#4643, tier B of #3818): ``native_spools``, the
records the native spool provider (`spools/spool_providers.py`) offers the overlay.

Unlike ``spool_provider_records``, which is a cache each provider's refresh rewrites,
these are the records themselves: a person keeps them (``/api/v1/native-spools``,
`api/native_spools.py`), and nothing else writes them yet. A record's RFID keys are
cleaned as the overlay cleans a tray's (`spools.models.clean_tray_uuid`,
`clean_tag_uid`), so a record a person types from a tray's report finds that tray's
spool; one unarchived record holds a key (the migration's partial unique indexes).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from scadbuddy.spools.models import SpoolSnapshot, clean_tag_uid, clean_tray_uuid

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
