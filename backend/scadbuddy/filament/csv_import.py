"""The spools' CSV import (#6262 Task 15, plan F24, F25): rows a browser parsed from a
file (no file bytes cross the wire), previewed with ``dry_run`` and then written.

A row with a ``number`` changes that spool, only in the cells that differ from what the
spool shows now (its effective value, or the name behind it); an empty cell changes
nothing. A row with no ``number`` makes a spool. Catalog names resolve by ``upper(name)``
and are added when missing, as the Bambuddy pull adds them (`bambuddy_pull._catalog_id`);
an insert type is the exception, since a new one needs an empty weight no CSV cell holds,
so a missing one is a row error. ``type`` is a spool type's code and must exist.
``location`` must name exactly one unarchived place, and a spool goes there through
`LocationStore.move_on` (#6261: a move is the only way to change a place).

A dry run reads and writes nothing (it adds no catalog row and takes no spool number). A
write runs every row in one transaction, each in a savepoint, and refuses as a whole
(`ImportRefusedError`, nothing written) when any row is an error.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from scadbuddy.filament import models as m
from scadbuddy.filament.bambuddy_pull import _catalog_id
from scadbuddy.filament.component import Filament
from scadbuddy.filament.locations import LocationStore, MoveTargetError
from scadbuddy.filament.spools import FilamentSpoolStore, SpoolTypelessError
from scadbuddy.filament.types import TypeTempError
from scadbuddy.filament.types import effective as work_out_effective

Conn = Connection[DictRow]

#: One cell as the browser read it; empty or blank reads as no value.
Cell = Annotated[str | None, Field(max_length=200)]
NoteCell = Annotated[str | None, Field(max_length=2000)]

#: The columns an export writes that are not a spool's own values: what the ledger and
#: the settings work out. An import ignores them (F24c), and the preview says so.
DERIVED_COLUMNS: Final = (
    "grams_left",
    "last_weighed_g",
    "last_weighed_at",
    "last_dried_at",
    "last_used_at",
    "low_stock",
    "archived_at",
)
#: How many rows one request may hold.
MAX_ROWS: Final = 5000


class CsvRow(BaseModel):
    """One row of a spools CSV, as text cells under the export's headers. Catalog
    columns hold names, ``type`` a spool type's code, ``location`` a place's name and
    ``extra_colours`` colours split by spaces, commas or semicolons. A derived column
    (`DERIVED_COLUMNS`) is dropped; any other unknown column is refused."""

    model_config = ConfigDict(extra="forbid")

    number: Cell = None
    type: Cell = None
    material: Cell = None
    subtype: Cell = None
    brand: Cell = None
    category: Cell = None
    effect: Cell = None
    colour_name: Cell = None
    rgba: Cell = None
    extra_colours: Cell = None
    diameter_mm: Cell = None
    density_g_cm3: Cell = None
    nozzle_temp_min: Cell = None
    nozzle_temp_max: Cell = None
    bed_temp_min: Cell = None
    bed_temp_max: Cell = None
    label_weight_g: Cell = None
    insert_type: Cell = None
    low_stock_pct: Cell = None
    cost_per_kg: Cell = None
    weight_locked: Cell = None
    location: Cell = None
    note: NoteCell = None

    @model_validator(mode="before")
    @classmethod
    def _cells(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        cells = {k: v for k, v in data.items() if k not in DERIVED_COLUMNS}
        return {k: None if isinstance(v, str) and not v.strip() else v for k, v in cells.items()}


#: The CSV's own columns, in the export's order (F25: the frontend's
#: ``SPOOL_CSV_COLUMNS`` names the same keys).
COLUMNS: Final = list(CsvRow.model_fields)


class SpoolCsvImport(BaseModel):
    """`POST /filament/spools/import`'s body."""

    model_config = ConfigDict(extra="forbid")

    rows: list[CsvRow] = Field(max_length=MAX_ROWS)
    #: True: answer what a write would do and write nothing.
    dry_run: bool


class ImportRowResult(BaseModel):
    """What one row does (or did). ``line`` is the row's line in a file with one header
    line (the first row is line 2). ``number`` is the spool's, or ``None`` for a spool a
    dry run would make. ``changes`` maps a column to ``[before, after]``; a catalog name
    not in the catalog yet carries a third item, ``{"new": true}``."""

    line: int
    action: Literal["create", "update", "error"]
    number: int | None
    changes: dict[str, list[Any]]
    error: str | None


class ImportPreview(BaseModel):
    rows: list[ImportRowResult]


class ImportRefusedError(ValueError):
    """A write whose rows hold an error: nothing was written."""

    def __init__(self, preview: ImportPreview) -> None:
        self.preview = preview
        super().__init__(
            "; ".join(f"line {r.line}: {r.error}" for r in preview.rows if r.action == "error")
        )


class _RowError(Exception):
    """A row that cannot be imported, with what to tell the person."""


#: Catalog columns: the spool column each sets and the table it names a row of.
_CATALOGS: Final = {
    "material": ("material_id", "filament_materials"),
    "subtype": ("subtype_id", "filament_subtypes"),
    "brand": ("brand_id", "filament_brands"),
    "category": ("category_id", "filament_categories"),
    "effect": ("effect_id", "filament_effects"),
}
#: Columns that are a spool column of the same name and a value of its type.
_PLAIN: Final = (
    "colour_name",
    "rgba",
    "extra_colours",
    "diameter_mm",
    "density_g_cm3",
    "nozzle_temp_min",
    "nozzle_temp_max",
    "bed_temp_min",
    "bed_temp_max",
    "label_weight_g",
    "low_stock_pct",
    "cost_per_kg",
    "weight_locked",
    "note",
)
#: Plain columns the spool holds as its own, not as an override of its type's.
_OWN: Final = frozenset({"weight_locked", "note"})
_SPLIT = re.compile(r"[\s,;]+")
_NUMBER = re.compile(r"#?(\d{1,9})")


def import_rows(
    store: Filament, rows: Sequence[CsvRow], *, dry_run: bool, actor: str
) -> ImportPreview:
    """What ``rows`` do; with ``dry_run`` false, done, in one transaction. A write with
    any row in error is `ImportRefusedError`, and nothing is written."""
    with store.spools.transaction() as conn:
        return import_on(conn, store, rows, dry_run=dry_run, actor=actor)


def import_on(
    conn: Conn, store: Filament, rows: Sequence[CsvRow], *, dry_run: bool, actor: str
) -> ImportPreview:
    """`import_rows` on ``conn``, in the caller's transaction (the route's, which holds
    its Idempotency-Key's ledger row too)."""
    results = []
    for index, row in enumerate(rows):
        line = index + 2
        try:
            if dry_run:
                results.append(_plan(conn, row, line, write=None))
            else:
                with conn.transaction():
                    results.append(_plan(conn, row, line, write=(store, actor)))
        except _RowError as exc:
            results.append(
                ImportRowResult(line=line, action="error", number=None, changes={}, error=str(exc))
            )
    preview = ImportPreview(rows=results)
    if not dry_run and any(r.action == "error" for r in results):
        raise ImportRefusedError(preview)
    return preview


def _plan(
    conn: Conn, row: CsvRow, line: int, *, write: tuple[Filament, str] | None
) -> ImportRowResult:
    plain = _plain(row)
    spool: m.Spool | None = None
    if row.number is not None:
        match = _NUMBER.fullmatch(row.number.strip())
        if match is None:
            raise _RowError(f"number: {row.number!r} is not a spool number")
        number = int(match.group(1))
        found = conn.execute(
            "SELECT id FROM filament_spools WHERE number = %s", (number,)
        ).fetchone()
        if found is None:
            raise _RowError(f"no spool #{number}")
        spool = FilamentSpoolStore.read(conn, found["id"])
        assert spool is not None
    elif row.type is None and row.material is None:
        raise _RowError("a new spool needs a type or a material")

    changes: dict[str, list[Any]] = {}
    values: dict[str, Any] = {}
    effective = spool.effective if spool is not None else m.EffectiveValues()

    if row.type is not None:
        kind = conn.execute(
            "SELECT id, code FROM filament_spool_types"
            " WHERE upper(code) = upper(%s) AND archived_at IS NULL",
            (row.type.strip(),),
        ).fetchone()
        if kind is None:
            raise _RowError(f"no spool type with the code {row.type.strip()!r}")
        if spool is None or spool.type_id != kind["id"]:
            before = None if spool is None else _type_code(conn, spool.type_id)
            changes["type"] = [before, kind["code"]]
            values["type_id"] = kind["id"]

    new: dict[str, str] = {}
    material = effective.material_id
    for column, (field, table) in _CATALOGS.items():
        name = getattr(row, column)
        if name is None:
            continue
        name = name.strip()
        current = None if spool is None else getattr(spool.names, column)
        if current is not None and current.upper() == name.upper():
            continue
        found_id = _find(conn, table, name, material)
        if column == "material":
            material = found_id
        changes[column] = [current, name if found_id is None else _name(conn, table, found_id)]
        if found_id is None:
            changes[column].append({"new": True})
            new[column] = name
        else:
            values[field] = found_id

    if row.insert_type is not None:
        name = row.insert_type.strip()
        current = None if spool is None else spool.names.insert_type
        if current is None or current.upper() != name.upper():
            found_id = _find(conn, "filament_insert_types", name, None)
            if found_id is None:
                raise _RowError(
                    f"no insert type named {name!r}; add it on the Catalog tab with its"
                    " empty weight"
                )
            changes["insert_type"] = [current, _name(conn, "filament_insert_types", found_id)]
            values["insert_type_id"] = found_id

    for field, value in plain.items():
        if spool is None:
            before = None
        else:
            before = getattr(spool if field in _OWN else effective, field)
            if before == value:
                continue
        changes[field] = [before, value]
        values[field] = value

    _check_temps(_after(conn, spool, values))

    location: UUID | None = None
    if row.location is not None:
        location = _place(conn, row.location.strip())
        if spool is None or spool.location_id != location:
            before_place = None if spool is None else spool.names.location
            changes["location"] = [before_place, _name(conn, "filament_locations", location)]
        else:
            location = None

    if write is None:
        return ImportRowResult(
            line=line,
            action="create" if spool is None else "update",
            number=None if spool is None else spool.number,
            changes=changes,
            error=None,
        )
    store, actor = write
    for column, name in new.items():
        field, table = _CATALOGS[column]
        values[field] = _catalog_id(conn, table, name, values.get("material_id", material))
    try:
        if spool is None:
            spool_id = store.spools.create_on(conn, m.SpoolCreate(**values), actor=actor)
        else:
            spool_id = spool.id
            if values and not store.spools.update_on(
                conn, spool_id, m.SpoolPatch(**values), actor=actor
            ):
                raise _RowError(f"no spool #{spool.number}")
    except (TypeTempError, SpoolTypelessError) as exc:
        raise _RowError(str(exc)) from None
    except ValidationError as exc:
        raise _RowError(_readable(exc)) from None
    except errors.ForeignKeyViolation as exc:
        raise _RowError(_missing(exc)) from None
    if location is not None:
        try:
            LocationStore.move_on(conn, spool_id, location, actor=actor)
        except (MoveTargetError, LookupError):
            raise _RowError(
                f"location: {_name(conn, 'filament_locations', location)!r} cannot be moved to"
            ) from None
    made = conn.execute("SELECT number FROM filament_spools WHERE id = %s", (spool_id,)).fetchone()
    assert made is not None
    return ImportRowResult(
        line=line,
        action="create" if spool is None else "update",
        number=made["number"],
        changes=changes,
        error=None,
    )


#: The CSV column behind a spool column that names a catalog row, a type or a place.
_ID_COLUMNS: Final = {
    "type_id": "type",
    "location_id": "location",
    "insert_type_id": "insert_type",
    **{field: column for column, (field, _table) in _CATALOGS.items()},
}


def _readable(exc: ValidationError) -> str:
    """A validation failure as ``column: problem`` lines, without the library's prefixes."""
    parts = []
    for e in exc.errors():
        column = ".".join(_ID_COLUMNS.get(str(p), str(p)) for p in e["loc"])
        problem = str(e["msg"]).removeprefix("Value error, ")
        parts.append(f"{column}: {problem}" if column else problem)
    return "; ".join(parts)


def _missing(exc: errors.ForeignKeyViolation) -> str:
    """A foreign key failure as the column whose value no longer exists (a catalog row
    archived or deleted while the import ran)."""
    constraint = exc.diag.constraint_name or ""
    for field, column in sorted(_ID_COLUMNS.items(), key=lambda kv: -len(kv[0])):
        if field in constraint:
            return f"{column}: that value no longer exists"
    return "a value refers to something that no longer exists"


def _plain(row: CsvRow) -> dict[str, Any]:
    """The plain cells, typed as a spool patch types them; a bad one is a row error."""
    cells: dict[str, Any] = {}
    for field in _PLAIN:
        cell = getattr(row, field)
        if cell is None:
            continue
        cell = cell.strip()
        cells[field] = [c for c in _SPLIT.split(cell) if c] if field == "extra_colours" else cell
    try:
        patch = m.SpoolPatch.model_validate(cells)
    except ValidationError as exc:
        raise _RowError(_readable(exc)) from None
    return {field: getattr(patch, field) for field in cells}


def _after(conn: Conn, spool: m.Spool | None, values: dict[str, Any]) -> m.EffectiveValues:
    """What the spool shows once ``values`` are written: its own cells over its type's
    (the new type's, when the row sets one), the type's subtype and material, as a read
    of it would work them out (`types.effective`). A catalog name not in the catalog yet
    has no row to inherit from."""
    own: dict[str, Any] = (spool.model_dump() if spool is not None else {}) | values

    def one(table: str, row_id: UUID | None) -> dict[str, Any] | None:
        if row_id is None:
            return None
        return conn.execute(f"SELECT * FROM {table} WHERE id = %s", (row_id,)).fetchone()

    kind = one("filament_spool_types", own.get("type_id"))

    def level(field: str) -> UUID | None:
        if own.get(field) is not None or kind is None:
            return own.get(field)
        return kind[field]  # type: ignore[no-any-return]

    return work_out_effective(
        own,
        kind,
        one("filament_subtypes", level("subtype_id")),
        one("filament_materials", level("material_id")),
    )


def _check_temps(after: m.EffectiveValues) -> None:
    for kind in ("nozzle", "bed"):
        low = getattr(after, f"{kind}_temp_min")
        high = getattr(after, f"{kind}_temp_max")
        if low is not None and high is not None and low > high:
            raise _RowError(f"{kind}_temp_min is above {kind}_temp_max")


def _find(conn: Conn, table: str, name: str, material: UUID | None) -> UUID | None:
    """The unarchived row of ``table`` named ``name`` in any case, as `_catalog_id`
    finds it, or ``None``."""
    if table == "filament_subtypes":
        row = conn.execute(
            "SELECT id FROM filament_subtypes WHERE archived_at IS NULL"
            " AND upper(name) = upper(%s) AND (material_id = %s OR material_id IS NULL)"
            " ORDER BY material_id IS NULL LIMIT 1",
            (name, material),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT id FROM {table} WHERE archived_at IS NULL AND upper(name) = upper(%s)",
            (name,),
        ).fetchone()
    return None if row is None else row["id"]


def _name(conn: Conn, table: str, row_id: UUID) -> str:
    row = conn.execute(f"SELECT name FROM {table} WHERE id = %s", (row_id,)).fetchone()
    assert row is not None
    return str(row["name"])


def _type_code(conn: Conn, type_id: UUID | None) -> str | None:
    if type_id is None:
        return None
    row = conn.execute("SELECT code FROM filament_spool_types WHERE id = %s", (type_id,)).fetchone()
    return None if row is None else str(row["code"])


def _place(conn: Conn, name: str) -> UUID:
    """The one unarchived place named ``name`` in any case (F24a); a row error for none
    or several (names are unique only under one parent)."""
    rows = conn.execute(
        "SELECT id FROM filament_locations WHERE archived_at IS NULL AND upper(name) = upper(%s)",
        (name,),
    ).fetchall()
    if not rows:
        raise _RowError(f"no location named {name!r}")
    if len(rows) > 1:
        raise _RowError(f"{len(rows)} locations are named {name!r}; rename one")
    place: UUID = rows[0]["id"]
    return place
