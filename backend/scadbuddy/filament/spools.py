"""Spools and their tags (#6261, epic #6260, spec §5.3-5.4): the store over
`filament_spools`, `filament_tags` and `filament_tag_bindings`. Every change writes its
ledger event (`filament/ledger.py`) in the same transaction.

A spool reads back with its effective values (`types.effective`): its own override, else
its type's, else the subtype's and material's print defaults. A spool with prints in its
ledger is never deleted (`SpoolInUseError`); it is archived.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.filament import ledger
from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import CatalogRefError
from scadbuddy.filament.types import TypeTempError, effective, overridden
from scadbuddy.spools.models import clean_tag_uid, clean_tray_uuid

Conn = Connection[DictRow]
Row = Mapping[str, Any]
_Spools = list[m.Spool]
_Events = list[m.SpoolEvent]

#: The columns of a spool row: every field of `Spool` but the two it is given here.
_SPOOL_FIELDS = tuple(f for f in m.Spool.model_fields if f not in {"effective", "overridden"})
_SPOOL_COLUMNS = ", ".join(_SPOOL_FIELDS)
_TYPE_COLUMNS = ", ".join(m.SpoolType.model_fields)
_TAG_COLUMNS = ", ".join(m.Tag.model_fields)
_BINDING_COLUMNS = ", ".join(m.TagBinding.model_fields)
#: What a create or a patch may write to a column of its own.
_WRITABLE = frozenset(m.SpoolPatch.model_fields) - {"archived"}


class SpoolInUseError(ValueError):
    """The spool has prints in its ledger: archive it instead."""

    def __init__(self) -> None:
        super().__init__("the spool has prints in its ledger; archive it instead")


class SpoolTypelessError(ValueError):
    """A spool would name neither a type nor a material."""

    def __init__(self) -> None:
        super().__init__("a spool needs a type_id or a material_id")


class TagUnnamedError(ValueError):
    """Neither key of the tag is left once cleaned (none, or all zeros)."""

    def __init__(self) -> None:
        super().__init__("the tag has no tag_uid or tray_uuid once cleaned")


class TagKeysClashError(ValueError):
    """The tag's UID and its tray UUID name two different tags."""

    def __init__(self) -> None:
        super().__init__("the tag_uid and the tray_uuid name two different tags")


def _check_temps(values: m.EffectiveValues) -> None:
    for kind in ("nozzle", "bed"):
        low, high = getattr(values, f"{kind}_temp_min"), getattr(values, f"{kind}_temp_max")
        if low is not None and high is not None and low > high:
            raise TypeTempError(f"{kind}_temp_min is above {kind}_temp_max")


def _by_id(conn: Conn, table: str, columns: str, ids: Iterable[UUID | None]) -> dict[UUID, Row]:
    wanted = list({i for i in ids if i is not None})
    if not wanted:
        return {}
    rows = conn.execute(f"SELECT {columns} FROM {table} WHERE id = ANY(%s)", (wanted,)).fetchall()
    return {row["id"]: row for row in rows}


def _hydrate(conn: Conn, rows: list[dict[str, Any]]) -> list[m.Spool]:
    """The spools of ``rows`` with their effective values. A spool's subtype and
    material are its own if it sets them, else its type's."""
    types = _by_id(conn, "filament_spool_types", _TYPE_COLUMNS, (r["type_id"] for r in rows))

    def level(row: Row, field: str) -> UUID | None:
        kind = types.get(row["type_id"])
        own: UUID | None = row[field]
        return own if own is not None or kind is None else kind[field]

    def pick(found: dict[UUID, Row], key: UUID | None) -> Row | None:
        return None if key is None else found.get(key)

    subtypes = _by_id(conn, "filament_subtypes", "*", (level(r, "subtype_id") for r in rows))
    materials = _by_id(conn, "filament_materials", "*", (level(r, "material_id") for r in rows))
    spools = []
    for row in rows:
        values = effective(
            row,
            pick(types, row["type_id"]),
            pick(subtypes, level(row, "subtype_id")),
            pick(materials, level(row, "material_id")),
        )
        spools.append(
            m.Spool.model_validate(row | {"effective": values, "overridden": overridden(row)})
        )
    return spools


class FilamentSpoolStore:
    """The spool and tag tables over a pool whose connections autocommit and return dict
    rows (the settings store's). Synchronous: callers run it in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    def list(self, *, include_archived: bool = False, needs_type: bool | None = None) -> _Spools:
        """By number. ``needs_type`` keeps only the spools with no type (``True``) or only
        those with one (``False``)."""
        where = [] if include_archived else ["archived_at IS NULL"]
        if needs_type is not None:
            where.append("type_id IS NULL" if needs_type else "type_id IS NOT NULL")
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_SPOOL_COLUMNS} FROM filament_spools{clause} ORDER BY number"
            ).fetchall()
            return _hydrate(conn, rows)

    def get(self, spool_id: UUID) -> m.Spool | None:
        with self._pool.connection() as conn:
            return self._spool(conn, spool_id)

    def create(self, body: m.SpoolCreate, *, actor: str) -> m.Spool:
        """A new spool and its ``created`` event; ``grams_left``, when sent, is that
        event's reading. The temperature ranges are checked on the effective values."""
        data = body.model_dump(exclude={"grams_left"})
        names = list(data)
        try:
            with self._pool.connection() as conn, conn.transaction():
                row = conn.execute(
                    f"INSERT INTO filament_spools ({', '.join(names)})"
                    f" VALUES ({', '.join('%s' for _ in names)}) RETURNING id",
                    [data[name] for name in names],
                ).fetchone()
                assert row is not None
                spool_id: UUID = row["id"]
                self._check(conn, spool_id)
                ledger.record(
                    conn,
                    spool_id,
                    "created",
                    source="person" if body.grams_left is not None else None,
                    absolute_g=body.grams_left,
                    actor=actor,
                )
                made = self._spool(conn, spool_id)
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        assert made is not None
        return made

    def update(self, spool_id: UUID, patch: m.SpoolPatch, *, actor: str) -> m.Spool | None:
        """The fields ``patch`` sends; ``None`` for no such spool. One ``edited`` event
        holds every field that changed, as ``[before, after]``; ``archived`` writes an
        ``archived`` or ``restored`` event instead. A patch that would leave neither a type
        nor a material is `SpoolTypelessError`, and the temperature ranges are checked on
        the effective values after it."""
        sent = [n for n in _WRITABLE if n in patch.model_fields_set]
        try:
            with self._pool.connection() as conn, conn.transaction():
                current = conn.execute(
                    f"SELECT {_SPOOL_COLUMNS} FROM filament_spools WHERE id = %s FOR UPDATE",
                    (spool_id,),
                ).fetchone()
                if current is None:
                    return None
                after = {n: getattr(patch, n) for n in sent}
                merged = current | after
                if merged["type_id"] is None and merged["material_id"] is None:
                    raise SpoolTypelessError()
                changes = {
                    n: [current[n], after[n]]
                    for n in m.SpoolPatch.model_fields
                    if n in after and current[n] != after[n]
                }
                if changes:
                    columns = list(changes)
                    conn.execute(
                        f"UPDATE filament_spools SET {', '.join(f'{c} = %s' for c in columns)},"
                        " updated_at = now() WHERE id = %s",
                        [*(after[c] for c in columns), spool_id],
                    )
                    self._check(conn, spool_id)
                    ledger.record(conn, spool_id, "edited", actor=actor, data={"changes": changes})
                if patch.archived is not None:
                    self._archive(conn, current, patch.archived, actor)
                return self._spool(conn, spool_id)
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None

    def archive(self, spool_id: UUID, archived: bool, *, actor: str) -> m.Spool | None:
        """Take a spool off the lists (``True``) or put it back; ``None`` for no such
        spool. Nothing is written when it is already so."""
        return self.update(spool_id, m.SpoolPatch(archived=archived), actor=actor)

    def delete(self, spool_id: UUID) -> bool:
        """Remove a spool that never printed, with its events, tag bindings and Bambuddy
        link; ``False`` for no such spool. `SpoolInUseError` when its ledger holds a
        ``print_started`` or ``print_ended`` event."""
        with self._pool.connection() as conn, conn.transaction():
            found = conn.execute(
                "SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,)
            ).fetchone()
            if found is None:
                return False
            used = conn.execute(
                "SELECT 1 FROM filament_spool_events WHERE spool_id = %s"
                " AND kind IN ('print_started', 'print_ended') LIMIT 1",
                (spool_id,),
            ).fetchone()
            if used is not None:
                raise SpoolInUseError()
            for table in ("filament_spool_events", "filament_tag_bindings", "filament_bb_links"):
                conn.execute(f"DELETE FROM {table} WHERE spool_id = %s", (spool_id,))
            conn.execute("DELETE FROM filament_spools WHERE id = %s", (spool_id,))
        return True

    def bind_tag(
        self,
        spool_id: UUID,
        *,
        tag_uid: str | None = None,
        tray_uuid: str | None = None,
        kind: m.TagKind,
        override: bool = False,
        actor: str,
    ) -> m.TagBinding | None:
        """Put a tag on a spool; ``None`` for no such spool. The tag is found by its
        cleaned UID or tray UUID (`spools.models.clean_tag_uid`, `clean_tray_uuid`), and
        takes a key it lacked; a tag not seen before is made with ``kind``. An open binding
        of the tag to another spool, and of the spool to another tag, is closed with
        ``reason='rebound'`` and a ``tag_unbound`` event on its spool; the new binding
        writes ``tag_bound``. A tag already on the spool is left as it is."""
        uid, tray = clean_tag_uid(tag_uid), clean_tray_uuid(tray_uuid)
        if uid is None and tray is None:
            raise TagUnnamedError()
        with self._pool.connection() as conn, conn.transaction():
            if (
                conn.execute(
                    "SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,)
                ).fetchone()
                is None
            ):
                return None
            tag_id = self._tag(conn, uid, tray, kind)
            held = conn.execute(
                f"SELECT {_BINDING_COLUMNS} FROM filament_tag_bindings"
                " WHERE tag_id = %s AND spool_id = %s AND unbound_at IS NULL",
                (tag_id, spool_id),
            ).fetchone()
            if held is not None:
                return m.TagBinding.model_validate(held)
            closed = conn.execute(
                "UPDATE filament_tag_bindings SET unbound_at = now(), reason = 'rebound'"
                " WHERE unbound_at IS NULL AND (tag_id = %s OR spool_id = %s)"
                " RETURNING tag_id, spool_id",
                (tag_id, spool_id),
            ).fetchall()
            for old in closed:
                ledger.record(
                    conn,
                    old["spool_id"],
                    "tag_unbound",
                    actor=actor,
                    data={"tag_id": old["tag_id"], "reason": "rebound"},
                )
            row = conn.execute(
                "INSERT INTO filament_tag_bindings (tag_id, spool_id, override)"
                f" VALUES (%s, %s, %s) RETURNING {_BINDING_COLUMNS}",
                (tag_id, spool_id, override),
            ).fetchone()
            assert row is not None
            ledger.record(
                conn,
                spool_id,
                "tag_bound",
                actor=actor,
                data={"tag_id": tag_id, "override": override},
            )
        return m.TagBinding.model_validate(row)

    def unbind_tag(self, tag_id: UUID, *, actor: str) -> m.TagBinding | None:
        """Take a tag off its spool, with a ``tag_unbound`` event; ``None`` when the tag
        is on no spool."""
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE filament_tag_bindings SET unbound_at = now(), reason = 'unbound'"
                f" WHERE tag_id = %s AND unbound_at IS NULL RETURNING {_BINDING_COLUMNS}",
                (tag_id,),
            ).fetchone()
            if row is None:
                return None
            ledger.record(
                conn,
                row["spool_id"],
                "tag_unbound",
                actor=actor,
                data={"tag_id": tag_id, "reason": "unbound"},
            )
        return m.TagBinding.model_validate(row)

    def open_binding(self, spool_id: UUID) -> m.TagBinding | None:
        """The spool's tag binding now, if it has one."""
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_BINDING_COLUMNS} FROM filament_tag_bindings"
                " WHERE spool_id = %s AND unbound_at IS NULL",
                (spool_id,),
            ).fetchone()
        return None if row is None else m.TagBinding.model_validate(row)

    def events(self, spool_id: UUID) -> _Events:
        """The spool's ledger, oldest first."""
        with self._pool.connection() as conn:
            return ledger.events(conn, spool_id)

    def add_event(
        self, spool_id: UUID, body: m.SpoolEventAdd, *, actor: str
    ) -> m.SpoolEvent | None:
        """A person's weighing or drying; ``None`` for no such spool."""
        data: dict[str, Any] = {} if body.note is None else {"note": body.note}
        if body.kind == "dried":
            data |= {
                "temp_c": body.temp_c,
                "hours": None if body.hours is None else float(body.hours),
            }
        with self._pool.connection() as conn, conn.transaction():
            if (
                conn.execute(
                    "SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,)
                ).fetchone()
                is None
            ):
                return None
            return ledger.record(
                conn,
                spool_id,
                body.kind,
                source=body.source if body.kind == "weighed" else "person",
                absolute_g=body.grams,
                at=body.at,
                actor=actor,
                data=data,
            )

    # -- internals --------------------------------------------------------------------

    @staticmethod
    def _spool(conn: Conn, spool_id: UUID) -> m.Spool | None:
        row = conn.execute(
            f"SELECT {_SPOOL_COLUMNS} FROM filament_spools WHERE id = %s", (spool_id,)
        ).fetchone()
        return None if row is None else _hydrate(conn, [row])[0]

    def _check(self, conn: Conn, spool_id: UUID) -> None:
        spool = self._spool(conn, spool_id)
        assert spool is not None
        _check_temps(spool.effective)

    @staticmethod
    def _archive(conn: Conn, current: Row, archived: bool, actor: str) -> None:
        if (current["archived_at"] is not None) == archived:
            return
        conn.execute(
            "UPDATE filament_spools SET archived_at = CASE WHEN %s THEN now() END,"
            " updated_at = now() WHERE id = %s",
            (archived, current["id"]),
        )
        ledger.record(conn, current["id"], "archived" if archived else "restored", actor=actor)

    @staticmethod
    def _tag(conn: Conn, uid: str | None, tray: str | None, kind: m.TagKind) -> UUID:
        """The id of the tag either key names, given the key it lacked; else a new tag."""
        found = conn.execute(
            f"SELECT {_TAG_COLUMNS} FROM filament_tags"
            " WHERE tag_uid = %s OR tray_uuid = %s FOR UPDATE",
            (uid, tray),
        ).fetchall()
        if len(found) > 1:
            raise TagKeysClashError()
        if found:
            tag = found[0]
            if (uid is not None and tag["tag_uid"] is None) or (
                tray is not None and tag["tray_uuid"] is None
            ):
                conn.execute(
                    "UPDATE filament_tags SET tag_uid = coalesce(tag_uid, %s),"
                    " tray_uuid = coalesce(tray_uuid, %s) WHERE id = %s",
                    (uid, tray, tag["id"]),
                )
            tag_id: UUID = tag["id"]
            return tag_id
        made = conn.execute(
            "INSERT INTO filament_tags (kind, tag_uid, tray_uuid) VALUES (%s, %s, %s) RETURNING id",
            (kind, uid, tray),
        ).fetchone()
        assert made is not None
        new_id: UUID = made["id"]
        return new_id
