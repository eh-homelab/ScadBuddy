"""Locations (#6261, epic #6260, spec §5.2): the tree of places a spool sits in (room,
shelf, bin) and the AMS trays, which are places too. A spool's move writes a ``moved``
ledger event (`filament/ledger.py`) in the transaction that changes its ``location_id``;
a move through `LocationStore.move`/`move_on` clears its ``tray_fingerprint`` (a printer
merge, which keeps the tray, keeps it): a spool in a tray place holds that tray only while
the tray shows what it showed when the spool was linked (`filament/inventory.py`).

"Removing" a place archives it (``archived_at``), which frees its name and its tray.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.filament import ledger
from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import CatalogRefError

Conn = Connection[DictRow]

_COLUMNS = ", ".join(m.Location.model_fields)
#: Held while a place gets a new parent, so two moves in opposite directions cannot both
#: pass the cycle check and then make a cycle.
_TREE_LOCK = "filament_locations_tree"


class LocationExistsError(ValueError):
    """An unarchived place of that name is under the parent, or an unarchived place is
    already that tray."""

    def __init__(self) -> None:
        super().__init__("a place with that name or that tray already exists")


class LocationCycleError(ValueError):
    """The new parent is the place itself or one of its descendants."""

    def __init__(self) -> None:
        super().__init__("a place cannot be its own parent or sit under one of its own")


class NoAmsKindError(ValueError):
    """No unarchived location kind is flagged ``ams``."""

    def __init__(self) -> None:
        super().__init__("no location kind is marked as an AMS")


class MoveTargetError(ValueError):
    """The place a spool would move to does not exist or is archived."""

    def __init__(self) -> None:
        super().__init__("the place to move to does not exist or is archived")


class LocationStore:
    """The location tables over a pool whose connections autocommit and return dict rows
    (the settings store's). Synchronous: callers run it in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    def tree(self, *, include_archived: bool = False) -> list[m.LocationNode]:
        """The places nested under their parents, each level by sort order then name, with
        the number of unarchived spools in each. Archived places are left out unless
        ``include_archived``; a place whose parent is left out is then a root."""
        where = "" if include_archived else " WHERE archived_at IS NULL"
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM filament_locations{where}"
                " ORDER BY sort_order, upper(name), id"
            ).fetchall()
            counts = {
                row["location_id"]: row["n"]
                for row in conn.execute(
                    "SELECT location_id, count(*) AS n FROM filament_spools"
                    " WHERE location_id IS NOT NULL AND archived_at IS NULL"
                    " GROUP BY location_id"
                ).fetchall()
            }
        nodes = {
            row["id"]: m.LocationNode.model_validate(
                row | {"spool_count": counts.get(row["id"], 0)}
            )
            for row in rows
        }
        roots: list[m.LocationNode] = []
        for node in nodes.values():
            parent = nodes.get(node.parent_id) if node.parent_id is not None else None
            (roots if parent is None else parent.children).append(node)
        return roots

    def get(self, location_id: UUID) -> m.Location | None:
        with self._pool.connection() as conn:
            return self._get(conn, location_id)

    def create(self, body: m.LocationCreate) -> m.Location:
        data = body.model_dump()
        names = list(data)
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    f"INSERT INTO filament_locations ({', '.join(names)})"
                    f" VALUES ({', '.join('%s' for _ in names)}) RETURNING {_COLUMNS}",
                    [data[name] for name in names],
                ).fetchone()
        except errors.UniqueViolation:
            raise LocationExistsError() from None
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        assert row is not None
        return m.Location.model_validate(row)

    def update(self, location_id: UUID, patch: m.LocationPatch) -> m.Location | None:
        """The fields ``patch`` sends (``archived`` included); ``None`` for no such place.
        A new parent that is the place or one of its descendants is `LocationCycleError`;
        the ancestors of the new parent are walked in the transaction that writes it, with
        the tree lock and the place's row held."""
        sent = [n for n in type(patch).model_fields if n in patch.model_fields_set]
        columns = [n for n in sent if n != "archived"]
        sets = [f"{name} = %s" for name in columns]
        values: list[Any] = [getattr(patch, name) for name in columns]
        if patch.archived is not None:
            sets.append(
                "archived_at = COALESCE(archived_at, now())"
                if patch.archived
                else "archived_at = NULL"
            )
        sets.append("updated_at = now()")
        try:
            with self._pool.connection() as conn, conn.transaction():
                if "parent_id" in patch.model_fields_set:
                    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_TREE_LOCK,))
                if self._lock(conn, location_id) is None:
                    return None
                if patch.parent_id is not None and "parent_id" in patch.model_fields_set:
                    self._no_cycle(conn, location_id, patch.parent_id)
                row = conn.execute(
                    f"UPDATE filament_locations SET {', '.join(sets)} WHERE id = %s"
                    f" RETURNING {_COLUMNS}",
                    [*values, location_id],
                ).fetchone()
        except errors.UniqueViolation:
            raise LocationExistsError() from None
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        return None if row is None else m.Location.model_validate(row)

    def ensure_ams(self, printer_id: UUID, ams_id: int, tray_id: int, *, label: str) -> m.Location:
        """The unarchived place of that printer's tray, made with ``label`` as its name
        if there is none. Its kind is the first (by sort order) unarchived kind flagged
        ``ams``; `NoAmsKindError` when there is none. Idempotent, and safe when two calls
        race: the loser of the insert reads the winner's row. A tray is found by its printer,
        AMS unit and tray alone; its ``label`` is only a name and may repeat. An unknown printer is
        `CatalogRefError`."""
        select = (
            f"SELECT {_COLUMNS} FROM filament_locations WHERE printer_id = %s AND ams_id = %s"
            " AND tray_id = %s AND archived_at IS NULL"
        )
        key = (printer_id, ams_id, tray_id)
        try:
            with self._pool.connection() as conn:
                found = conn.execute(select, key).fetchone()
                if found is None:
                    kind = conn.execute(
                        "SELECT id FROM filament_location_kinds WHERE ams AND archived_at IS NULL"
                        " ORDER BY sort_order, upper(name), id LIMIT 1"
                    ).fetchone()
                    if kind is None:
                        raise NoAmsKindError()
                    found = conn.execute(
                        "INSERT INTO filament_locations"
                        " (name, kind_id, printer_id, ams_id, tray_id) VALUES (%s, %s, %s, %s, %s)"
                        " ON CONFLICT (printer_id, ams_id, tray_id)"
                        " WHERE printer_id IS NOT NULL AND archived_at IS NULL DO NOTHING"
                        f" RETURNING {_COLUMNS}",
                        (label, kind["id"], *key),
                    ).fetchone()
                if found is None:
                    found = conn.execute(select, key).fetchone()
        except errors.UniqueViolation:
            raise LocationExistsError() from None
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        assert found is not None
        return m.Location.model_validate(found)

    def move(self, spool_id: UUID, location_id: UUID | None, *, actor: str) -> m.SpoolEvent | None:
        """Put a spool in a place (``None``: in none) and write its ``moved`` event,
        ``data={"from": old, "to": new}``. ``None`` when it is there already: nothing is
        written. `LookupError` for no such spool, `MoveTargetError` for a place that does
        not exist or is archived. Locks the spool, then the place."""
        with self._pool.connection() as conn, conn.transaction():
            return self.move_on(conn, spool_id, location_id, actor=actor)

    @staticmethod
    def move_on(
        conn: Conn, spool_id: UUID, location_id: UUID | None, *, actor: str
    ) -> m.SpoolEvent | None:
        """`move` on ``conn``, in the caller's transaction (`filament/inventory.py`
        `FilamentInventory.link`). A move through `move`/`move_on` clears the spool's
        ``tray_fingerprint``: what a tray showed when the spool was placed there says
        nothing of its new place. A printer merge (`repoint_printer_locations`) keeps it,
        since the spool stays in the same tray."""
        spool = conn.execute(
            "SELECT location_id FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,)
        ).fetchone()
        if spool is None:
            raise LookupError(f"no spool {spool_id}")
        before: UUID | None = spool["location_id"]
        if before == location_id:
            return None
        if location_id is not None:
            target = conn.execute(
                "SELECT 1 FROM filament_locations WHERE id = %s AND archived_at IS NULL FOR SHARE",
                (location_id,),
            ).fetchone()
            if target is None:
                raise MoveTargetError()
        conn.execute(
            "UPDATE filament_spools SET location_id = %s, tray_fingerprint = NULL,"
            " updated_at = now() WHERE id = %s",
            (location_id, spool_id),
        )
        return ledger.record(
            conn,
            spool_id,
            "moved",
            actor=actor,
            data={"from": before, "to": location_id},
        )

    # -- internals --------------------------------------------------------------------

    @staticmethod
    def _get(conn: Conn, location_id: UUID) -> m.Location | None:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM filament_locations WHERE id = %s", (location_id,)
        ).fetchone()
        return None if row is None else m.Location.model_validate(row)

    @staticmethod
    def _lock(conn: Conn, location_id: UUID) -> dict[str, Any] | None:
        return conn.execute(
            "SELECT id FROM filament_locations WHERE id = %s FOR UPDATE", (location_id,)
        ).fetchone()

    @staticmethod
    def _no_cycle(conn: Conn, location_id: UUID, parent_id: UUID) -> None:
        """The ancestors of ``parent_id``, itself included, do not hold ``location_id``."""
        hit = conn.execute(
            "WITH RECURSIVE up AS ("
            " SELECT id, parent_id FROM filament_locations WHERE id = %s"
            " UNION"
            " SELECT l.id, l.parent_id FROM filament_locations l JOIN up ON l.id = up.parent_id"
            ") SELECT 1 FROM up WHERE id = %s",
            (parent_id, location_id),
        ).fetchone()
        if hit is not None:
            raise LocationCycleError()


#: Who a merge's ``moved`` events name: no person moved the spools.
MERGE_ACTOR = "system:printer-merge"


def repoint_printer_locations(conn: Conn, merged: UUID, into: UUID) -> None:
    """Make printer ``merged``'s AMS tray places printer ``into``'s, on ``conn`` and in
    the printer merge's transaction (`printers/store.py` `_merge`). A tray only
    ``merged`` has moves as it is. Where both printers have an unarchived place for one
    tray, ``into``'s stands: each spool in ``merged``'s moves there with a ``moved``
    event, its places move under it, and ``merged``'s is archived (not deleted: the
    ledger's ``moved`` events name it). Archived places move as they are.

    Locks the spools before the places, as `LocationStore.move` does."""
    gone = conn.execute(
        "SELECT m.id, k.id AS kept FROM filament_locations m LEFT JOIN filament_locations k"
        " ON k.printer_id = %s AND k.ams_id = m.ams_id AND k.tray_id = m.tray_id"
        " AND k.archived_at IS NULL"
        " WHERE m.printer_id = %s AND m.archived_at IS NULL AND k.id IS NOT NULL",
        (into, merged),
    ).fetchall()
    clash = {row["id"]: row["kept"] for row in gone}
    spools = conn.execute(
        "SELECT id, location_id FROM filament_spools WHERE location_id = ANY(%s)"
        " ORDER BY id FOR UPDATE",
        (list(clash),),
    ).fetchall()
    conn.execute(
        "SELECT id FROM filament_locations WHERE printer_id = ANY(%s) ORDER BY id FOR UPDATE",
        ([merged, into],),
    )
    for spool in spools:
        to = clash[spool["location_id"]]
        conn.execute(
            "UPDATE filament_spools SET location_id = %s, updated_at = now() WHERE id = %s",
            (to, spool["id"]),
        )
        ledger.record(
            conn,
            spool["id"],
            "moved",
            actor=MERGE_ACTOR,
            data={"from": spool["location_id"], "to": to, "reason": "printer_merged"},
        )
    for old, kept in clash.items():
        conn.execute(
            "UPDATE filament_locations SET parent_id = %s, updated_at = now() WHERE parent_id = %s",
            (kept, old),
        )
        conn.execute(
            "UPDATE filament_locations SET archived_at = now(), updated_at = now() WHERE id = %s",
            (old,),
        )
    conn.execute(
        "UPDATE filament_locations SET printer_id = %s, updated_at = now() WHERE printer_id = %s",
        (into, merged),
    )
