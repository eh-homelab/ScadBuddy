"""Spool types (#6261, epic #6260): a type is a product, what a maker sells under one
code. This module holds the store over `filament_spool_types` and its two child tables,
and the rule that gives a spool its values: its own override, else its type's, else its
subtype's, else its material's (`effective`).

A type with a ``cost_per_kg`` keeps a row of the price book (`filaments`, §5.1) in step,
in the transaction that writes the type; ``filament_id`` is that row.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast, get_args
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import (
    CatalogExistsError,
    CatalogRefError,
    check_temps,
)
from scadbuddy.filament.catalog import (
    TypeTempError as TypeTempError,
)
from scadbuddy.filament.colour import normalise_rgba
from scadbuddy.orders.models import FilamentEffect, FilamentInput, FilamentPatch
from scadbuddy.orders.store import OrderStore

Conn = Connection[DictRow]
Row = Mapping[str, Any]
_Types = list[m.SpoolType]
_Presets = list[m.TypePreset]
_KValues = list[m.TypeKValue]

#: The fields whose change rewrites a type's price book row.
_PRICED = frozenset({"cost_per_kg", "material_id", "subtype_id", "brand_id", "rgba"})
_TYPE_COLUMNS = ", ".join(m.SpoolType.model_fields)
_PRESET_COLUMNS = ", ".join(m.TypePreset.model_fields)
_K_COLUMNS = ", ".join(m.TypeKValue.model_fields)

#: Every field a spool may override, in the order `EffectiveValues` declares them.
OVERRIDABLE: tuple[str, ...] = tuple(m.EffectiveValues.model_fields)

#: What a subtype and a material give a spool: their print defaults, nothing else (a
#: subtype's own ``material_id`` names its parent, not the spool's material).
_INHERITED: frozenset[str] = frozenset(
    {"density_g_cm3", "nozzle_temp_min", "nozzle_temp_max", "bed_temp_min", "bed_temp_max"}
)


class TypeRowsDuplicateError(ValueError):
    """Two rows of one list name the same printer and nozzle."""

    def __init__(self) -> None:
        super().__init__("two rows name the same printer and nozzle")


def effective(
    spool_row: Row | None,
    type_row: Row | None,
    subtype_row: Row | None,
    material_row: Row | None,
) -> m.EffectiveValues:
    """For each overridable field, the first value that is not null of the spool, its
    type, the type's subtype and the type's material. A missing level is ``None``."""
    values: dict[str, Any] = {}
    for field in OVERRIDABLE:
        levels: tuple[Row | None, ...] = (spool_row, type_row)
        if field in _INHERITED:
            levels = (*levels, subtype_row, material_row)
        values[field] = next(
            (row[field] for row in levels if row is not None and row.get(field) is not None),
            None,
        )
    return m.EffectiveValues.model_validate(values)


def overridden(spool_row: Row) -> list[str]:
    """The overridable fields the spool sets itself."""
    return [field for field in OVERRIDABLE if spool_row.get(field) is not None]


class SpoolTypeStore:
    """The type tables over a pool whose connections autocommit and return dict rows
    (the settings store's). Synchronous: callers run it in a thread."""

    def __init__(self, pool: ConnectionPool[Conn], orders: OrderStore | None = None) -> None:
        self._pool = pool
        self._orders = orders or OrderStore(pool)

    def list(self, *, include_archived: bool = False) -> _Types:
        where = "" if include_archived else " WHERE archived_at IS NULL"
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_TYPE_COLUMNS} FROM filament_spool_types{where} ORDER BY upper(code), id"
            ).fetchall()
        return [m.SpoolType.model_validate(row) for row in rows]

    def get(self, type_id: UUID) -> m.SpoolType | None:
        with self._pool.connection() as conn:
            row = self._row(conn, type_id)
        return None if row is None else m.SpoolType.model_validate(row)

    def presets(self, type_id: UUID) -> _Presets:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_PRESET_COLUMNS} FROM filament_spool_type_presets WHERE type_id = %s"
                " ORDER BY printer_model, nozzle_diameter",
                (type_id,),
            ).fetchall()
        return [m.TypePreset.model_validate(row) for row in rows]

    def k_values(self, type_id: UUID) -> _KValues:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_K_COLUMNS} FROM filament_spool_type_k_values WHERE type_id = %s"
                " ORDER BY printer_id, extruder, nozzle_diameter",
                (type_id,),
            ).fetchall()
        return [m.TypeKValue.model_validate(row) for row in rows]

    def create(self, body: m.SpoolTypeCreate) -> m.SpoolType:
        data = body.model_dump()
        check_temps(data)
        names = list(data)
        try:
            with self._orders.transaction() as conn:
                row = conn.execute(
                    f"INSERT INTO filament_spool_types ({', '.join(names)})"
                    f" VALUES ({', '.join('%s' for _ in names)}) RETURNING {_TYPE_COLUMNS}",
                    [data[name] for name in names],
                ).fetchone()
                assert row is not None
                row = self._link_price_book(conn, row, changed=True)
        except errors.UniqueViolation:
            raise CatalogExistsError() from None
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        return m.SpoolType.model_validate(row)

    def update(self, type_id: UUID, patch: m.SpoolTypePatch) -> m.SpoolType | None:
        """The fields ``patch`` sends (``archived`` included); ``None`` for no such type.
        The temperature ranges are checked as they stand after the patch."""
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
            with self._orders.transaction() as conn:
                current = self._row(conn, type_id, lock=True)
                if current is None:
                    return None
                check_temps(current | {n: getattr(patch, n) for n in columns})
                row = conn.execute(
                    f"UPDATE filament_spool_types SET {', '.join(sets)} WHERE id = %s"
                    f" RETURNING {_TYPE_COLUMNS}",
                    [*values, type_id],
                ).fetchone()
                assert row is not None
                row = self._link_price_book(conn, row, changed=bool(set(columns) & _PRICED))
        except errors.UniqueViolation:
            raise CatalogExistsError() from None
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        return m.SpoolType.model_validate(row)

    def archive(self, type_id: UUID, archived: bool) -> m.SpoolType | None:
        """Take a type off the lists (``True``), freeing its code, or put it back."""
        return self.update(type_id, m.SpoolTypePatch(archived=archived))

    def set_presets(self, type_id: UUID, rows: _Presets) -> _Presets | None:
        """The type's presets, replaced by ``rows``; ``None`` for no such type."""
        try:
            with self._orders.transaction() as conn:
                if self._row(conn, type_id, lock=True) is None:
                    return None
                conn.execute(
                    "DELETE FROM filament_spool_type_presets WHERE type_id = %s", (type_id,)
                )
                for row in rows:
                    conn.execute(
                        "INSERT INTO filament_spool_type_presets (type_id, printer_model,"
                        " nozzle_diameter, slicer_filament, slicer_filament_name)"
                        " VALUES (%s, %s, %s, %s, %s)",
                        (
                            type_id,
                            row.printer_model,
                            row.nozzle_diameter,
                            row.slicer_filament,
                            row.slicer_filament_name,
                        ),
                    )
        except errors.UniqueViolation:
            raise TypeRowsDuplicateError() from None
        return self.presets(type_id)

    def set_k_values(self, type_id: UUID, rows: _KValues) -> _KValues | None:
        """The type's pressure-advance values, replaced by ``rows``; ``None`` for no such
        type."""
        try:
            with self._orders.transaction() as conn:
                if self._row(conn, type_id, lock=True) is None:
                    return None
                conn.execute(
                    "DELETE FROM filament_spool_type_k_values WHERE type_id = %s", (type_id,)
                )
                for row in rows:
                    conn.execute(
                        "INSERT INTO filament_spool_type_k_values (type_id, printer_id, extruder,"
                        " nozzle_diameter, nozzle_type, k_value, name, cali_idx, setting_id)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            type_id,
                            row.printer_id,
                            row.extruder,
                            row.nozzle_diameter,
                            row.nozzle_type,
                            row.k_value,
                            row.name,
                            row.cali_idx,
                            row.setting_id,
                        ),
                    )
        except errors.UniqueViolation:
            raise TypeRowsDuplicateError() from None
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        return self.k_values(type_id)

    def match(
        self,
        *,
        code: str | None = None,
        brand: str | None,
        material: str,
        subtype: str | None,
        rgba: str,
        label_weight_g: int,
        conn: Conn | None = None,
    ) -> m.SpoolType | None:
        """The unarchived type a product description names. ``code`` alone decides when
        given (case-insensitive); else brand, material and subtype are catalog names (any
        case; none matches a type with none), ``rgba`` is compared with its alpha (six
        digits are opaque) and ``label_weight_g`` exactly. On ``conn`` when given (a caller
        in its own transaction, `filament/bambuddy_pull.py`), else on a pooled one."""
        if conn is not None:
            return self._match(conn, code, brand, material, subtype, rgba, label_weight_g)
        with self._pool.connection() as pooled:
            return self._match(pooled, code, brand, material, subtype, rgba, label_weight_g)

    @staticmethod
    def _match(
        conn: Conn,
        code: str | None,
        brand: str | None,
        material: str,
        subtype: str | None,
        rgba: str,
        label_weight_g: int,
    ) -> m.SpoolType | None:
        if code is not None:
            row = conn.execute(
                f"SELECT {_TYPE_COLUMNS} FROM filament_spool_types"
                " WHERE upper(code) = upper(%s) AND archived_at IS NULL",
                (code,),
            ).fetchone()
        else:
            columns = ", ".join(f"t.{c}" for c in m.SpoolType.model_fields)
            row = conn.execute(
                f"SELECT {columns} FROM filament_spool_types t"
                " JOIN filament_materials mat ON mat.id = t.material_id"
                " LEFT JOIN filament_subtypes sub ON sub.id = t.subtype_id"
                " LEFT JOIN filament_brands b ON b.id = t.brand_id"
                " WHERE t.archived_at IS NULL AND upper(mat.name) = upper(%s)"
                " AND upper(coalesce(sub.name, '')) = upper(coalesce(%s, ''))"
                " AND upper(coalesce(b.name, '')) = upper(coalesce(%s, ''))"
                " AND t.rgba = %s AND t.label_weight_g = %s"
                " ORDER BY t.created_at, t.id LIMIT 1",
                (material, subtype, brand, normalise_rgba(rgba), label_weight_g),
            ).fetchone()
        return None if row is None else m.SpoolType.model_validate(row)

    # -- internals --------------------------------------------------------------------

    @staticmethod
    def _row(conn: Conn, type_id: UUID, *, lock: bool = False) -> dict[str, Any] | None:
        return conn.execute(
            f"SELECT {_TYPE_COLUMNS} FROM filament_spool_types WHERE id = %s"
            f"{' FOR UPDATE' if lock else ''}",
            (type_id,),
        ).fetchone()

    def _link_price_book(self, conn: Conn, row: dict[str, Any], *, changed: bool) -> dict[str, Any]:
        """Write the type's price book row in the same transaction, and ``filament_id``.

        Its identity is the type's material, subtype and brand names, the ``#RRGGBB`` of
        its ``rgba`` and its alpha byte: #6209 landed in #6211 before this work, so the
        book keeps ``colour`` ``#RRGGBB`` beside ``alpha``, ``extra_colours`` and
        ``effect``, and an opaque and a translucent type of one hue get two rows. The row
        is found by that identity, else inserted with the type's alpha, colour stops and
        effect: a book row's identity and look are never rewritten, since another type,
        or a person, may hold it. A row found keeps its label and takes the type's cost.
        Nothing is written when ``changed`` is false (a patch that touched neither cost
        nor identity, an archive), when the type has no ``cost_per_kg`` (it keeps its
        ``filament_id``), and the book row of an archived type stays live, for the quotes
        that price with it."""
        if not changed or row["cost_per_kg"] is None:
            return row
        names = conn.execute(
            "SELECT (SELECT name FROM filament_materials WHERE id = %s) AS material,"
            " (SELECT name FROM filament_subtypes WHERE id = %s) AS subtype,"
            " (SELECT name FROM filament_brands WHERE id = %s) AS brand,"
            " (SELECT name FROM filament_effects WHERE id = %s) AS effect",
            (row["material_id"], row["subtype_id"], row["brand_id"], row["effect_id"]),
        ).fetchone()
        assert names is not None
        colour, alpha = row["rgba"][:7], int(row["rgba"][7:9], 16)
        cost = f"{row['cost_per_kg']:.4f}"
        target = self._orders.existing_filament(
            conn, names["material"], names["subtype"], names["brand"], colour, alpha
        )
        if target is None:
            target = self._orders.insert_filament(
                FilamentInput(
                    material=names["material"],
                    subtype=names["subtype"],
                    brand=names["brand"],
                    colour_name=row["colour_name"],
                    colour=colour,
                    cost_per_kg=cost,
                    alpha=alpha,
                    extra_colours=(row["extra_colours"] or [])[:8],
                    effect=_book_effect(names["effect"]),
                ),
                conn=conn,
            ).id
        else:
            book = self._orders.filament(target, conn=conn)
            assert book is not None
            if Decimal(book.cost_per_kg) != row["cost_per_kg"]:
                self._orders.update_filament(target, FilamentPatch(cost_per_kg=cost), conn=conn)
        if target == row["filament_id"]:
            return row
        linked = conn.execute(
            "UPDATE filament_spool_types SET filament_id = %s WHERE id = %s"
            f" RETURNING {_TYPE_COLUMNS}",
            (target, row["id"]),
        ).fetchone()
        assert linked is not None
        return linked


def _book_effect(name: str | None) -> FilamentEffect | None:
    """The price book's ``effect`` for an effect catalog name (``Dual Color`` is
    ``dual-color``); ``None`` for a name the book has no value for."""
    if name is None:
        return None
    value = name.strip().lower().replace(" ", "-")
    return cast("FilamentEffect", value) if value in get_args(FilamentEffect) else None


#: The K value tables and the column of what each value belongs to.
K_VALUE_TABLES = {"filament_spool_type_k_values": "type_id", "filament_spool_k_values": "spool_id"}


def repoint_k_values(conn: Conn, table: str, merged: UUID, into: UUID) -> None:
    """Make printer ``merged``'s K values in ``table`` printer ``into``'s, on ``conn`` and
    in the printer merge's transaction (`printers/store.py` `_merge`). Where both
    printers have a value for one key (owner, extruder, nozzle), ``into``'s stands and
    ``merged``'s is dropped."""
    owner = K_VALUE_TABLES[table]
    conn.execute(
        f"DELETE FROM {table} m USING {table} k WHERE m.printer_id = %s AND k.printer_id = %s"
        f" AND k.{owner} = m.{owner} AND k.extruder = m.extruder"
        " AND k.nozzle_diameter = m.nozzle_diameter",
        (merged, into),
    )
    conn.execute(f"UPDATE {table} SET printer_id = %s WHERE printer_id = %s", (into, merged))
