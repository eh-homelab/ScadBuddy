"""The filament catalogs (#6261, epic #6260): materials, subtypes, brands, categories,
effects, insert materials and types, named colours and location kinds. One store serves
all nine, keyed by kind; a `CatalogSpec` names a kind's table and models.

"Removing" a row archives it (``archived_at``), which frees its name; an archived row
stays readable by id and by the rows that point at it. Uniqueness is the tables' own
partial indexes, so a clash surfaces as `CatalogExistsError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.filament import models as m

Conn = Connection[DictRow]


@dataclass(frozen=True)
class CatalogSpec:
    table: str
    create: type[BaseModel]
    patch: type[BaseModel]
    out: type[BaseModel]


CATALOGS: dict[str, CatalogSpec] = {
    "materials": CatalogSpec("filament_materials", m.MaterialCreate, m.MaterialPatch, m.Material),
    "subtypes": CatalogSpec("filament_subtypes", m.SubtypeCreate, m.SubtypePatch, m.Subtype),
    "brands": CatalogSpec("filament_brands", m.BrandCreate, m.BrandPatch, m.Brand),
    "categories": CatalogSpec("filament_categories", m.CategoryCreate, m.CategoryPatch, m.Category),
    "effects": CatalogSpec("filament_effects", m.EffectCreate, m.EffectPatch, m.Effect),
    "insert-materials": CatalogSpec(
        "filament_insert_materials",
        m.InsertMaterialCreate,
        m.InsertMaterialPatch,
        m.InsertMaterial,
    ),
    "insert-types": CatalogSpec(
        "filament_insert_types", m.InsertTypeCreate, m.InsertTypePatch, m.InsertType
    ),
    "colours": CatalogSpec("filament_colours", m.ColourCreate, m.ColourPatch, m.Colour),
    "location-kinds": CatalogSpec(
        "filament_location_kinds",
        m.LocationKindCreate,
        m.LocationKindPatch,
        m.LocationKind,
    ),
}


class CatalogExistsError(ValueError):
    """An unarchived row of the catalog already has that name."""

    def __init__(self) -> None:
        super().__init__("a row of this catalog already has that name")


class CatalogRefError(ValueError):
    """A row the new one points at does not exist."""

    def __init__(self) -> None:
        super().__init__("a row this one points at does not exist")


class UnknownCatalogError(KeyError):
    """No catalog of that kind."""


def _spec(kind: str) -> CatalogSpec:
    try:
        return CATALOGS[kind]
    except KeyError:
        raise UnknownCatalogError(kind) from None


def _columns(spec: CatalogSpec) -> str:
    # Column names come from the declared models, never from a request.
    return ", ".join(spec.out.model_fields)


class CatalogStore:
    """The catalog tables over a pool whose connections autocommit and return dict rows
    (the settings store's). Synchronous: callers run it in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    def list(self, kind: str, *, include_archived: bool = False) -> list[BaseModel]:
        spec = _spec(kind)
        where = "" if include_archived else " WHERE archived_at IS NULL"
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_columns(spec)} FROM {spec.table}{where}"
                " ORDER BY sort_order, upper(name), id"
            ).fetchall()
        return [spec.out.model_validate(row) for row in rows]

    def get(self, kind: str, row_id: UUID) -> BaseModel | None:
        spec = _spec(kind)
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_columns(spec)} FROM {spec.table} WHERE id = %s", (row_id,)
            ).fetchone()
        return None if row is None else spec.out.model_validate(row)

    def create(self, kind: str, body: BaseModel) -> BaseModel:
        spec = _spec(kind)
        data = body.model_dump()
        names = list(data)
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    f"INSERT INTO {spec.table} ({', '.join(names)})"
                    f" VALUES ({', '.join('%s' for _ in names)}) RETURNING {_columns(spec)}",
                    [data[name] for name in names],
                ).fetchone()
        except errors.UniqueViolation:
            raise CatalogExistsError() from None
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        assert row is not None
        return spec.out.model_validate(row)

    def update(self, kind: str, row_id: UUID, patch: BaseModel) -> BaseModel | None:
        """The fields ``patch`` sends (``archived`` included); ``None`` for no such row."""
        spec = _spec(kind)
        sent = [n for n in type(patch).model_fields if n in patch.model_fields_set]
        columns = [n for n in sent if n != "archived"]
        sets = [f"{name} = %s" for name in columns]
        values: list[Any] = [getattr(patch, name) for name in columns]
        archived = getattr(patch, "archived", None) if "archived" in sent else None
        if archived is not None:
            sets.append(
                "archived_at = COALESCE(archived_at, now())" if archived else "archived_at = NULL"
            )
        sets.append("updated_at = now()")
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    f"UPDATE {spec.table} SET {', '.join(sets)} WHERE id = %s"
                    f" RETURNING {_columns(spec)}",
                    [*values, row_id],
                ).fetchone()
        except errors.UniqueViolation:
            raise CatalogExistsError() from None
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        return None if row is None else spec.out.model_validate(row)

    def archive(self, kind: str, row_id: UUID, archived: bool) -> BaseModel | None:
        """Take a row off the lists (``True``) or put it back; ``None`` for no such row."""
        return self.update(kind, row_id, _spec(kind).patch.model_validate({"archived": archived}))
