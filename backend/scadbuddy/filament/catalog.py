"""The filament catalogs (#6261, epic #6260): materials, subtypes, brands, categories,
effects, insert materials and types, named colours and location kinds. One store serves
all nine, keyed by kind; a `CatalogSpec` names a kind's table and models.

"Removing" a row archives it (``archived_at``); an archived row stays readable by id and
by the rows that point at it. A material's or subtype's temperature range is never
backwards (`TypeTempError`), as a type's is.

Names (#6706). The materials, subtypes, brands, categories and effects catalogs keep every
row's name and aliases in a names table of their own (`NameTable`), keyed by
`catalog_key`: the name in upper case with every non-alphanumeric dropped, so `Dual
Color` and `dual-color` are one key. Its primary key keeps a key on one row (a subtype's
within its material), archived rows included. A trigger claims a row's name in the
writer's transaction (`20261120T0602Z_filament_catalog_names.sql`); this store writes the
aliases (`set_aliases`). A clash is `CatalogKeyClashError`, which names the holder, and
a name or alias with no letter or digit is `CatalogKeyEmptyError`. Lookups
by name resolve through the key (`find_by_key`, `resolve_or_add`). The other catalogs keep
their partial indexes on ``upper(name)``, which an archive frees; a clash there is
`CatalogExistsError`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.filament import models as m
from scadbuddy.filament.bb_sync_models import forget_sync

Conn = Connection[DictRow]


#: The material scope of a subtype for every material (R-6706-10).
NIL = UUID(int=0)


def catalog_key(name: str) -> str:
    """A name's key: ``+`` read as ``PLUS`` (R-6706-17: `PLA+` is `PLA Plus`, not `PLA`),
    then its letters and digits, upper case (R-6706-1). The same function as `key_sql`; a
    character Python would upper-case to two (``ß``) stays as it is, as Postgres keeps
    it. Stored keys come only from `key_sql` (R-6706-14)."""
    return "".join(_upper(char) for char in name.replace("+", "PLUS") if char.isalnum())


def _upper(char: str) -> str:
    upper = char.upper()
    return upper if len(upper) == 1 else char


def key_sql(expr: str) -> str:
    """`catalog_key` in SQL, of the SQL expression ``expr``."""
    return f"upper(regexp_replace(replace({expr}, '+', 'PLUS'), '[^[:alnum:]]', '', 'g'))"


@dataclass(frozen=True)
class NameTable:
    """A catalog's names table (#6706): ``owner`` is its column of the catalog row, ``noun``
    the row's name in a clash (and its ``<noun>_id``, ``<noun>_name``), ``scope`` the
    catalog column whose value scopes a key (a subtype's ``material_id``)."""

    table: str
    owner: str
    noun: str
    scope: str | None = None


@dataclass(frozen=True)
class CatalogSpec:
    table: str
    create: type[BaseModel]
    patch: type[BaseModel]
    out: type[BaseModel]
    names: NameTable | None = None


CATALOGS: dict[str, CatalogSpec] = {
    "materials": CatalogSpec(
        "filament_materials",
        m.MaterialCreate,
        m.MaterialPatch,
        m.Material,
        NameTable("filament_material_names", "material_id", "material"),
    ),
    "subtypes": CatalogSpec(
        "filament_subtypes",
        m.SubtypeCreate,
        m.SubtypePatch,
        m.Subtype,
        NameTable("filament_subtype_names", "subtype_id", "subtype", scope="material_id"),
    ),
    "brands": CatalogSpec(
        "filament_brands",
        m.BrandCreate,
        m.BrandPatch,
        m.Brand,
        NameTable("filament_brand_names", "brand_id", "brand"),
    ),
    "categories": CatalogSpec(
        "filament_categories",
        m.CategoryCreate,
        m.CategoryPatch,
        m.Category,
        NameTable("filament_category_names", "category_id", "category"),
    ),
    "effects": CatalogSpec(
        "filament_effects",
        m.EffectCreate,
        m.EffectPatch,
        m.Effect,
        NameTable("filament_effect_names", "effect_id", "effect"),
    ),
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


#: The text columns a catalog search (`CatalogStore.list` ``q``) reads, where a kind has them.
_SEARCHED = ("name", "manufacturer", "material_name", "rgba")

#: The kind of each catalog table.
KIND_OF_TABLE = {spec.table: kind for kind, spec in CATALOGS.items()}


class CatalogExistsError(ValueError):
    """An unarchived row of the catalog already has that name."""

    def __init__(self) -> None:
        super().__init__("a row of this catalog already has that name")


class CatalogKeyClashError(CatalogExistsError):
    """A name or alias whose key another name or alias of the catalog holds (#6706)."""

    def __init__(self, noun: str, holder_id: UUID, holder_name: str, name: str) -> None:
        ValueError.__init__(
            self, f"the {noun} {holder_name!r} already has a name or alias that matches {name!r}"
        )
        self.noun = noun
        self.holder_id = holder_id
        self.holder_name = holder_name


class CatalogKeyEmptyError(ValueError):
    """A name or alias with no letter or digit: it has no key, so it is refused."""

    def __init__(self, name: str, what: str = "name") -> None:
        super().__init__(f"the {what} {name!r} has no letter or digit")


class AliasKeyEmptyError(CatalogKeyEmptyError):
    """An alias with no key."""

    def __init__(self, alias: str) -> None:
        super().__init__(alias, "alias")


class NoAliasesError(KeyError):
    """A catalog that has no aliases."""


class CatalogRefError(ValueError):
    """A row the new one points at does not exist."""

    def __init__(self) -> None:
        super().__init__("a row this one points at does not exist")


class TypeTempError(ValueError):
    """A temperature range whose minimum is above its maximum (a type's, a spool's, a
    material's or a subtype's)."""


#: The catalogs whose rows have print temperatures.
_TEMPS = frozenset({"materials", "subtypes"})
_TEMP_COLUMNS = ("nozzle_temp_min", "nozzle_temp_max", "bed_temp_min", "bed_temp_max")


def check_temps(values: Mapping[str, Any]) -> None:
    """`TypeTempError` when a nozzle or bed minimum is above its maximum."""
    for kind in ("nozzle", "bed"):
        low, high = values.get(f"{kind}_temp_min"), values.get(f"{kind}_temp_max")
        if low is not None and high is not None and low > high:
            raise TypeTempError(f"{kind}_temp_min is above {kind}_temp_max")


class UnknownCatalogError(KeyError):
    """No catalog of that kind."""


def _spec(kind: str) -> CatalogSpec:
    try:
        return CATALOGS[kind]
    except KeyError:
        raise UnknownCatalogError(kind) from None


def _columns(spec: CatalogSpec) -> str:
    # Column names come from the declared models, never from a request.
    columns = [name for name in spec.out.model_fields if name != "aliases"]
    if spec.names is not None:
        n = spec.names
        columns.append(
            f"ARRAY(SELECT n.name FROM {n.table} n WHERE n.{n.owner} = {spec.table}.id"
            " AND NOT n.canonical ORDER BY upper(n.name), n.name) AS aliases"
        )
    return ", ".join(columns)


def _names(kind: str) -> tuple[CatalogSpec, NameTable]:
    spec = _spec(kind)
    if spec.names is None:
        raise NoAliasesError(kind)
    return spec, spec.names


def _scope_of(names: NameTable, material: UUID | None) -> UUID | None:
    """The ``material_scope`` of a key in ``names``; ``None`` for an unscoped table."""
    return None if names.scope is None else (material or NIL)


def _holder(
    conn: Conn,
    names: NameTable,
    table: str,
    name: str,
    scope: UUID | None,
    *,
    other_than: UUID | None = None,
) -> tuple[UUID, str] | None:
    """The row that holds ``name``'s key in its scope (but ``other_than``), and its
    canonical name."""
    where = "" if scope is None else " AND n.material_scope = %s"
    params: list[Any] = [name] if scope is None else [name, scope]
    if other_than is not None:
        where += f" AND n.{names.owner} <> %s"
        params.append(other_than)
    row = conn.execute(
        f"SELECT c.id, c.name FROM {names.table} n JOIN {table} c ON c.id = n.{names.owner}"
        f" WHERE n.key = {key_sql('%s')}{where}",
        params,
    ).fetchone()
    return None if row is None else (row["id"], row["name"])


def _no_key(exc: errors.CheckViolation, spec: CatalogSpec) -> bool:
    """Whether the name trigger refused a name with no key."""
    message = exc.diag.message_primary or ""
    return spec.names is not None and message.endswith("has no letter or digit")


def _is_names(exc: errors.UniqueViolation, spec: CatalogSpec) -> bool:
    return spec.names is not None and exc.diag.table_name == spec.names.table


def _clash(
    conn: Conn,
    spec: CatalogSpec,
    names: Sequence[str],
    material: UUID | None,
    *,
    other_than: UUID | None = None,
) -> CatalogExistsError:
    """The `CatalogKeyClashError` naming who holds the key of the first of ``names`` that
    another row holds, read after the refused write (a moved subtype brings its name and
    every alias into the new scope, R-6706-16); the plain error when nobody does by then."""
    assert spec.names is not None
    scope = _scope_of(spec.names, material)
    for name in names:
        held = _holder(conn, spec.names, spec.table, name, scope, other_than=other_than)
        if held is not None:
            return CatalogKeyClashError(spec.names.noun, held[0], held[1], name)
    return CatalogExistsError()


def has_key(conn: Conn, name: str) -> bool:
    """Whether ``name`` has a key (a letter or digit), as the name trigger reads it."""
    row = conn.execute(f"SELECT {key_sql('%s')} <> '' AS has", (name,)).fetchone()
    assert row is not None
    return bool(row["has"])


def lookup_key(
    conn: Conn, kind: str, name: str, *, material: UUID | None = None
) -> tuple[bool, UUID | None]:
    """In one query: whether ``name`` has a key (`has_key`), and the row `find_by_key`
    finds for it (none for a name with no key)."""
    _, names = _names(kind)
    where, scope = "n.key = k.key", list[Any]()
    if names.scope is not None:
        where += " AND n.material_scope IN (%s, %s) ORDER BY n.material_scope = %s"
        scope = [material or NIL, NIL, NIL]
    # The subquery's placeholders come before the key's in the text.
    row = conn.execute(
        f"SELECT k.key <> '' AS keyed, (SELECT n.{names.owner} FROM {names.table} n"
        f" WHERE {where} LIMIT 1) AS id FROM (SELECT {key_sql('%s')} AS key) k",
        [*scope, name],
    ).fetchone()
    assert row is not None
    return bool(row["keyed"]), row["id"]


def find_by_key(conn: Conn, kind: str, name: str, *, material: UUID | None = None) -> UUID | None:
    """The row of a catalog with names whose name or alias has ``name``'s key, archived
    or not. A subtype is ``material``'s own, else one for every material."""
    return lookup_key(conn, kind, name, material=material)[1]


def resolve_or_add(
    conn: Conn, kind: str, name: str, *, sort_order: int, material: UUID | None = None
) -> UUID:
    """`find_by_key`, else a new row named ``name`` (a subtype of ``material``), whose
    key the trigger claims in the same transaction. A row another writer adds first is
    found on the next pass."""
    spec, names = _names(kind)
    for _ in range(3):
        found = find_by_key(conn, kind, name, material=material)
        if found is not None:
            return found
        columns, values = ["name", "sort_order"], [name, sort_order]
        if names.scope is not None:
            columns.append(names.scope)
            values.append(material)
        try:
            with conn.transaction():
                made = conn.execute(
                    f"INSERT INTO {spec.table} ({', '.join(columns)})"
                    f" VALUES ({', '.join('%s' for _ in columns)}) RETURNING id",
                    values,
                ).fetchone()
        except errors.UniqueViolation as exc:
            if not _is_names(exc, spec):
                raise
            continue
        except errors.CheckViolation as exc:
            if not _no_key(exc, spec):
                raise
            raise CatalogKeyEmptyError(name) from None
        assert made is not None
        new: UUID = made["id"]
        return new
    raise CatalogExistsError()


class CatalogStore:
    """The catalog tables over a pool whose connections autocommit and return dict rows
    (the settings store's). Synchronous: callers run it in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    def list(
        self, kind: str, *, include_archived: bool = False, q: str | None = None
    ) -> list[BaseModel]:
        """A catalog in sort order. ``q``: every word must match some text of the row, in
        any case (its name; a colour's maker, material and ``#RRGGBBAA`` too, #6907)."""
        spec = _spec(kind)
        where: list[str] = [] if include_archived else ["archived_at IS NULL"]
        params: list[Any] = []
        texts = [n for n in _SEARCHED if n in spec.out.model_fields]
        for word in (q or "").split():
            where.append("(" + " OR ".join(f"{n} ILIKE %s" for n in texts) + ")")
            escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{escaped}%"] * len(texts))
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_columns(spec)} FROM {spec.table}{clause}"
                " ORDER BY sort_order, upper(name), id",
                params,
            ).fetchall()
        return [spec.out.model_validate(row) for row in rows]

    def get(self, kind: str, row_id: UUID) -> BaseModel | None:
        with self._pool.connection() as conn:
            return self._read(conn, _spec(kind), row_id)

    def resolve(self, kind: str, name: str, material: UUID | None = None) -> BaseModel | None:
        """The row `find_by_key` finds, or ``None``."""
        with self._pool.connection() as conn:
            found = find_by_key(conn, kind, name, material=material)
            return None if found is None else self._read(conn, _spec(kind), found)

    @staticmethod
    def _read(conn: Conn, spec: CatalogSpec, row_id: UUID) -> BaseModel | None:
        row = conn.execute(
            f"SELECT {_columns(spec)} FROM {spec.table} WHERE id = %s", (row_id,)
        ).fetchone()
        return None if row is None else spec.out.model_validate(row)

    def create(self, kind: str, body: BaseModel) -> BaseModel:
        spec = _spec(kind)
        data = body.model_dump()
        if kind in _TEMPS:
            check_temps(data)
        names = list(data)
        with self._pool.connection() as conn:
            try:
                with conn.transaction():
                    row = conn.execute(
                        f"INSERT INTO {spec.table} ({', '.join(names)})"
                        f" VALUES ({', '.join('%s' for _ in names)}) RETURNING id",
                        [data[name] for name in names],
                    ).fetchone()
                    assert row is not None
                    made = self._read(conn, spec, row["id"])
            except errors.UniqueViolation as exc:
                if not _is_names(exc, spec):
                    raise CatalogExistsError() from None
                raise _clash(conn, spec, [data["name"]], data.get("material_id")) from None
            except errors.CheckViolation as exc:
                if not _no_key(exc, spec):
                    raise
                raise CatalogKeyEmptyError(data["name"]) from None
            except errors.ForeignKeyViolation:
                raise CatalogRefError() from None
        assert made is not None
        return made

    def update(self, kind: str, row_id: UUID, patch: BaseModel) -> BaseModel | None:
        """The fields ``patch`` sends (``archived`` included); ``None`` for no such row.
        `TypeTempError` when a material's or subtype's temperature range would be
        backwards; `CatalogKeyClashError` when the new name (or a subtype's new material)
        meets a key another row holds."""
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
        with self._pool.connection() as conn:
            try:
                with conn.transaction():
                    if kind in _TEMPS and set(columns) & set(_TEMP_COLUMNS):
                        current = conn.execute(
                            f"SELECT {', '.join(_TEMP_COLUMNS)} FROM {spec.table}"
                            " WHERE id = %s FOR UPDATE",
                            (row_id,),
                        ).fetchone()
                        if current is not None:
                            check_temps(current | dict(zip(columns, values, strict=True)))
                    row = conn.execute(
                        f"UPDATE {spec.table} SET {', '.join(sets)} WHERE id = %s RETURNING id",
                        [*values, row_id],
                    ).fetchone()
                    if row is not None and archived and kind == "insert-types":
                        forget_sync(conn, "insert_type", row_id)
                    changed = None if row is None else self._read(conn, spec, row_id)
            except errors.UniqueViolation as exc:
                if not _is_names(exc, spec):
                    raise CatalogExistsError() from None
                was = self._read(conn, spec, row_id)
                name = str(getattr(patch, "name", None) or getattr(was, "name", ""))
                material = (
                    getattr(patch, "material_id", None)
                    if "material_id" in sent
                    else getattr(was, "material_id", None)
                )
                aliases = list(getattr(was, "aliases", None) or [])
                raise _clash(conn, spec, [name, *aliases], material, other_than=row_id) from None
            except errors.CheckViolation as exc:
                if not _no_key(exc, spec):
                    raise
                raise CatalogKeyEmptyError(str(getattr(patch, "name", ""))) from None
            except errors.ForeignKeyViolation:
                raise CatalogRefError() from None
        return changed

    def set_aliases(self, kind: str, row_id: UUID, aliases: Sequence[str]) -> BaseModel | None:
        """Replace a row's aliases (trimmed); ``None`` for no such row. All or none:
        `CatalogKeyClashError` when an alias's key is taken (by another row, the row's own
        name, or an earlier alias of the list), `AliasKeyEmptyError` when it has none."""
        spec, names = _names(kind)
        scope_column = names.scope or "NULL::uuid"
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                f"SELECT {scope_column} AS material FROM {spec.table} WHERE id = %s FOR UPDATE",
                (row_id,),
            ).fetchone()
            if row is None:
                return None
            scope = _scope_of(names, row["material"])
            conn.execute(
                f"DELETE FROM {names.table} WHERE {names.owner} = %s AND NOT canonical", (row_id,)
            )
            scope_insert = "" if scope is None else ", material_scope"
            for alias in (one.strip() for one in aliases):
                # Refused before its INSERT, so an alias with no key is never a clash.
                if not has_key(conn, alias):
                    raise AliasKeyEmptyError(alias)
                values: list[Any] = [alias, row_id, alias] + ([] if scope is None else [scope])
                try:
                    with conn.transaction():
                        conn.execute(
                            f"INSERT INTO {names.table} (key, {names.owner}, name, canonical"
                            f"{scope_insert}) VALUES ({key_sql('%s')}, %s, %s, false"
                            f"{'' if scope is None else ', %s'})",
                            values,
                        )
                except errors.UniqueViolation:
                    held = _holder(conn, names, spec.table, alias, scope)
                    assert held is not None
                    raise CatalogKeyClashError(names.noun, held[0], held[1], alias) from None
            return self._read(conn, spec, row_id)

    def archive(self, kind: str, row_id: UUID, archived: bool) -> BaseModel | None:
        """Take a row off the lists (``True``) or put it back; ``None`` for no such row."""
        return self.update(kind, row_id, _spec(kind).patch.model_validate({"archived": archived}))
