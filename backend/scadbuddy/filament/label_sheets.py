"""Saved label sheet grids (#7216, plan `docs/superpowers/plans/2026-11-25-label-outputs.md`
R-7216-11 to R-7216-13): named sheet layouts in the table `label_sheets`, which only the API
reads and writes. A label choice names a sheet by its id (`template: "saved"`), and the PDF
reads the row at each request, so an edit applies to the next print.

The bounds are Bambuddy's (`services/label_renderer.py` at v1.3.1,
https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/label_renderer.py#L506-L561),
except the smallest label, which is SB's QR floor (R-7157-1)."""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scadbuddy.filament.label_templates import (
    MAX_LENGTH_MM,
    MIN_HEIGHT_MM,
    MIN_WIDTH_MM,
    PAGE_MM,
    LabelSheet,
    LabelTemplate,
    LabelTemplateChoice,
    PageSize,
)

Conn = Connection[DictRow]

#: The most saved sheets an install keeps (R-7216-11).
MAX_SAVED_SHEETS: Final = 100
#: The longest name, after trim and NFC.
NAME_MAX: Final = 100
#: Bambuddy's grid bounds.
MAX_COLS: Final = 20
MAX_ROWS: Final = 40
#: How far past the page a grid may reach, as Bambuddy's.
FIT_SLACK_MM: Final = 0.01

#: The transaction lock that a create counts and inserts under.
_LOCK: Final = "scadbuddy:filament:label_sheets"
_LAYOUT: Final = (
    "page",
    "width_mm",
    "height_mm",
    "cols",
    "rows",
    "top_mm",
    "left_mm",
    "col_gap_mm",
    "row_gap_mm",
)
_COLUMNS: Final = ", ".join(("id", "name", *_LAYOUT, "created_at", "updated_at"))


def name_key(name: str) -> str:
    """The key two names match by: NFC, then casefold."""
    return unicodedata.normalize("NFC", name).casefold()


def sort_key(name: str) -> tuple[str, str]:
    """The list order: the name with its marks removed, then its `name_key`."""
    plain = "".join(
        ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch)
    )
    return plain.casefold(), name_key(name)


class SheetLayout(BaseModel):
    """A sheet's page, its label size and its grid, in mm (R-7216-11)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    page: PageSize
    width_mm: float = Field(ge=MIN_WIDTH_MM, le=MAX_LENGTH_MM)
    height_mm: float = Field(ge=MIN_HEIGHT_MM, le=MAX_LENGTH_MM)
    cols: int = Field(ge=1, le=MAX_COLS)
    rows: int = Field(ge=1, le=MAX_ROWS)
    top_mm: float = Field(ge=0, le=MAX_LENGTH_MM)
    left_mm: float = Field(ge=0, le=MAX_LENGTH_MM)
    col_gap_mm: float = Field(default=0.0, ge=0, le=MAX_LENGTH_MM)
    row_gap_mm: float = Field(default=0.0, ge=0, le=MAX_LENGTH_MM)

    @model_validator(mode="after")
    def _fits_its_page(self) -> SheetLayout:
        page_w, page_h = PAGE_MM[self.page]
        wide = self.left_mm + self.cols * self.width_mm + (self.cols - 1) * self.col_gap_mm
        tall = self.top_mm + self.rows * self.height_mm + (self.rows - 1) * self.row_gap_mm
        if wide > page_w + FIT_SLACK_MM:
            raise ValueError(
                f"the grid is {wide:g} mm wide, but the {self.page} page is {page_w:g} mm wide"
            )
        if tall > page_h + FIT_SLACK_MM:
            raise ValueError(
                f"the grid is {tall:g} mm tall, but the {self.page} page is {page_h:g} mm tall"
            )
        return self


class LabelSheetIn(BaseModel):
    """A sheet to save: its name (trimmed, NFC, 1 to `NAME_MAX` characters) and layout."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_MAX)
    layout: SheetLayout

    @field_validator("name", mode="before")
    @classmethod
    def _trim_and_compose(cls, value: Any) -> Any:
        return unicodedata.normalize("NFC", value).strip() if isinstance(value, str) else value


class SavedLabelSheet(BaseModel):
    id: UUID
    name: str
    layout: SheetLayout
    created_at: datetime
    updated_at: datetime


class LabelSheetNameTakenError(ValueError):
    """Another sheet has the name, case and composition aside."""

    def __init__(self, name: str) -> None:
        super().__init__(f"a label sheet named {name!r} exists: choose another name")


class TooManyLabelSheetsError(ValueError):
    def __init__(self) -> None:
        super().__init__(
            f"at most {MAX_SAVED_SHEETS} saved label sheets: delete one before you save another"
        )


class LabelSheetGoneError(LookupError):
    """A choice names a saved sheet that was deleted."""

    def __init__(self) -> None:
        super().__init__("The saved sheet was deleted. Choose another template.")


def _sheet(row: DictRow) -> SavedLabelSheet:
    return SavedLabelSheet(
        id=row["id"],
        name=row["name"],
        layout=SheetLayout(**{n: row[n] for n in _LAYOUT}),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _values(body: LabelSheetIn) -> list[Any]:
    return [body.name, name_key(body.name), *(getattr(body.layout, n) for n in _LAYOUT)]


class LabelSheetStore:
    """The saved sheets over a pool whose connections autocommit and return dict rows.
    Synchronous: callers run it in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    def list(self) -> list[SavedLabelSheet]:
        """Every saved sheet, by `sort_key`, so an accented name lists beside its plain
        letters ("Étiquettes" before "Zebra")."""
        with self._pool.connection() as conn:
            rows = conn.execute(f"SELECT {_COLUMNS} FROM label_sheets").fetchall()
        sheets = [_sheet(row) for row in rows]
        return sorted(sheets, key=lambda sheet: (sort_key(sheet.name), str(sheet.id)))

    def get(self, sheet_id: UUID) -> SavedLabelSheet | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM label_sheets WHERE id = %s", (sheet_id,)
            ).fetchone()
        return None if row is None else _sheet(row)

    def create(self, body: LabelSheetIn) -> SavedLabelSheet:
        """Save a new sheet. The count and the insert run under one transaction lock, so
        two saves at once cannot both pass `MAX_SAVED_SHEETS`."""
        columns = ("name", "name_key", *_LAYOUT)
        try:
            with self._pool.connection() as conn, conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_LOCK,))
                count = conn.execute("SELECT count(*) AS n FROM label_sheets").fetchone()
                assert count is not None
                if count["n"] >= MAX_SAVED_SHEETS:
                    raise TooManyLabelSheetsError()
                row = conn.execute(
                    f"INSERT INTO label_sheets ({', '.join(columns)})"
                    f" VALUES ({', '.join('%s' for _ in columns)}) RETURNING {_COLUMNS}",
                    _values(body),
                ).fetchone()
        except errors.UniqueViolation:
            raise LabelSheetNameTakenError(body.name) from None
        assert row is not None
        return _sheet(row)

    def replace(self, sheet_id: UUID, body: LabelSheetIn) -> SavedLabelSheet | None:
        """Replace a sheet's name and layout; ``None`` when there is no such sheet."""
        sets = ", ".join(f"{n} = %s" for n in ("name", "name_key", *_LAYOUT))
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    f"UPDATE label_sheets SET {sets}, updated_at = now() WHERE id = %s"
                    f" RETURNING {_COLUMNS}",
                    [*_values(body), sheet_id],
                ).fetchone()
        except errors.UniqueViolation:
            raise LabelSheetNameTakenError(body.name) from None
        return None if row is None else _sheet(row)

    def delete(self, sheet_id: UUID) -> bool:
        with self._pool.connection() as conn:
            return conn.execute("DELETE FROM label_sheets WHERE id = %s", (sheet_id,)).rowcount > 0


def to_template(sheet: SavedLabelSheet) -> LabelTemplate:
    """The template a saved sheet prints as."""
    layout = sheet.layout
    return LabelTemplate(
        id="saved",
        name=sheet.name,
        width_mm=layout.width_mm,
        height_mm=layout.height_mm,
        sheet=LabelSheet(
            page=layout.page,
            cols=layout.cols,
            rows=layout.rows,
            top_mm=layout.top_mm,
            left_mm=layout.left_mm,
            col_gap_mm=layout.col_gap_mm,
            row_gap_mm=layout.row_gap_mm,
        ),
    )


def resolve(choice: LabelTemplateChoice, sheets: LabelSheetStore) -> LabelTemplate:
    """The template a choice names. A saved sheet is read from its row now, so an edit
    applies to the next print; a deleted one is `LabelSheetGoneError`."""
    if choice.template != "saved":
        return choice.resolved()
    assert choice.sheet_id is not None
    sheet = sheets.get(choice.sheet_id)
    if sheet is None:
        raise LabelSheetGoneError()
    return to_template(sheet)
