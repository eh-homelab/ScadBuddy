"""Locations (#6261, epic #6260, spec §5.2): the tree of places a spool sits in (room,
shelf, bin) and the AMS trays, which are places too. A spool's move writes a ``moved``
ledger event (`filament/ledger.py`) in the transaction that changes its ``location_id``;
a move through `LocationStore.move`/`move_on` clears its ``tray_fingerprint`` (a printer
merge, which keeps the tray, keeps it): a spool in a tray place holds that tray only while
the tray shows what it showed when the spool was linked (`filament/inventory.py`).

"Removing" a place archives it (``archived_at``), which frees its name and its tray.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.trays import EXTERNAL, unit_label
from scadbuddy.codes.parse import format_code
from scadbuddy.filament import ledger
from scadbuddy.filament import models as m
from scadbuddy.filament.bb_sync_models import BbLinked, forget_sync
from scadbuddy.filament.catalog import CatalogRefError
from scadbuddy.filament.dry_box import (
    BoxFullError,
    check_not_many,
    keep_home_on,
    occupant_on,
)

Conn = Connection[DictRow]

_COLUMNS = ", ".join(m.Location.model_fields)
#: Held while a place gets a new parent, so two moves in opposite directions cannot both
#: pass the cycle check and then make a cycle.
_TREE_LOCK = "filament_locations_tree"
#: The prefix of a printer's place lock (`LocationStore.lock_printer_on`).
_PRINTER_LOCK = "filament_printer_places:"


class LocationExistsError(ValueError):
    """An unarchived place of that name is under the parent, or an unarchived place is
    already that tray or that AMS unit (a printer, an ``ams_id`` and no tray)."""

    def __init__(self) -> None:
        super().__init__("a place with that name, that tray or that AMS unit already exists")


class LocationCycleError(ValueError):
    """The new parent is the place itself or one of its descendants."""

    def __init__(self) -> None:
        super().__init__("a place cannot be its own parent or sit under one of its own")


class NoAmsKindError(ValueError):
    """No unarchived location kind is flagged ``ams``."""

    def __init__(self) -> None:
        super().__init__("no location kind is marked as an AMS")


class NoPrinterKindError(ValueError):
    """No unarchived location kind is flagged ``printer``."""

    def __init__(self) -> None:
        super().__init__("no location kind is marked as a printer")


class MoveTargetError(ValueError):
    """The place a spool would move to does not exist, is archived or is an AMS unit's
    place (a printer, an ``ams_id`` and no tray). A printer's place with no ``ams_id``
    takes spools."""

    def __init__(self) -> None:
        super().__init__(
            "the place to move to does not exist, is archived or is an AMS unit's place"
        )


#: A guard for every UPDATE that hangs a place under ``%s``: never one of that place's
#: own ancestors (itself included), or a person's edits (the printer's place hung under
#: its unit) and the rehang would make a cycle (review of #7038).
_NOT_ABOVE = (
    " AND l.id NOT IN (WITH RECURSIVE up AS ("
    " SELECT id, parent_id FROM filament_locations WHERE id = %s"
    " UNION SELECT a.id, a.parent_id FROM filament_locations a"
    " JOIN up ON a.id = up.parent_id) SELECT id FROM up)"
)


#: The ``data.reason`` of the ``moved`` event a printer's delete books for each spool in
#: one of its places (`release_printer_spools_on`).
PRINTER_DELETED = "printer_deleted"


def release_printer_spools_on(conn: Conn, printer_id: UUID, *, actor: str) -> int:
    """Move each spool in one of ``printer_id``'s places to no place, on ``conn``'s
    transaction, just before the printer's row is deleted. Its places go with the row
    (`filament_locations.printer_id` is ON DELETE CASCADE), and a spool still in one would
    fail the delete on `filament_spools.location_id`. Each move is `LocationStore.move_on`,
    a ``moved`` event with ``data.reason`` `PRINTER_DELETED`. Capture makes a spool for a
    tagged tray at once (#7143), so most printers hold one. Locks the spools in id order.
    Returns how many moved."""
    spools = conn.execute(
        "SELECT s.id FROM filament_spools s JOIN filament_locations l ON l.id = s.location_id"
        " WHERE l.printer_id = %s ORDER BY s.id FOR UPDATE OF s",
        (printer_id,),
    ).fetchall()
    for spool in spools:
        LocationStore.move_on(conn, spool["id"], None, actor=actor, reason=PRINTER_DELETED)
    return len(spools)


class ChildPlaceClashError(LocationExistsError):
    """A place under one of a printer's places cannot move to the place above the printer
    (#8177): an unarchived place of its name is there already, or ``other_id``, another
    place that moves there too, has its name."""

    def __init__(self, place_id: UUID, name: str, other_id: UUID | None = None) -> None:
        super().__init__()
        self.place_id = place_id
        self.name = name
        self.other_id = other_id


def lock_printer_places_on(conn: Conn, printer_id: UUID) -> None:
    """Take the tree lock, then lock every place of ``printer_id`` in id order, to the end
    of ``conn``'s transaction (#8177). Taken after the spools (`release_printer_spools_on`),
    as `LocationStore.move` locks the spool before the place. While they are held, no other
    transaction can make a spool, a stock row or a child place point at one of them: its
    foreign key check waits. A write that passed that check before the lock was taken has
    committed when the lock is given. So the spools must be read again after this lock (a
    spool can move in after the first `release_printer_spools_on`); the references and
    child places read after it are what the cascade meets."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_TREE_LOCK,))
    conn.execute(
        "SELECT id FROM filament_locations WHERE printer_id = %s ORDER BY id FOR UPDATE",
        (printer_id,),
    )


def release_printer_child_places_on(conn: Conn, printer_id: UUID) -> int:
    """Move each place that is not ``printer_id``'s but sits under one of its places, on
    ``conn``'s transaction, just before the printer's row is deleted (#8177): its parent
    goes with the row, and `filament_locations.parent_id` has no ON DELETE rule. Each goes
    to the place above the printer: the nearest place above the printer's unarchived root
    that is not the printer's (the walk up skips the printer's own places). Each goes to
    the top level when the printer has no unarchived root, when nothing is above it, or
    when that place is archived (an archived place is out of the tree, as an archived
    parent of a printer's unit counts as none, `LocationStore.ensure_ams_unit_on`). That
    place is locked ``FOR SHARE``, so it stays as read. Each move is
    `LocationStore.update_on`, the write path of a person's own move. Locks the places in
    id order. `ChildPlaceClashError` when a place of that name is there already, or when
    two of the places that move have one name; the caller's transaction then writes
    nothing. Returns how many moved."""
    above = conn.execute(
        "WITH RECURSIVE up AS ("
        " SELECT parent_id AS id FROM filament_locations"
        " WHERE printer_id = %s AND printer_root AND archived_at IS NULL"
        " UNION SELECT l.parent_id FROM filament_locations l JOIN up ON l.id = up.id"
        " WHERE l.printer_id = %s)"
        " SELECT l.id FROM up JOIN filament_locations l ON l.id = up.id"
        " WHERE l.printer_id IS DISTINCT FROM %s",
        (printer_id, printer_id, printer_id),
    ).fetchone()
    parent: UUID | None = None
    if above is not None:
        live = conn.execute(
            "SELECT archived_at IS NULL AS live FROM filament_locations WHERE id = %s FOR SHARE",
            (above["id"],),
        ).fetchone()
        if live is not None and live["live"]:
            parent = above["id"]
    children = conn.execute(
        "SELECT c.id, c.name, c.printer_id IS NULL AND c.archived_at IS NULL AS named"
        " FROM filament_locations c JOIN filament_locations p ON p.id = c.parent_id"
        " WHERE p.printer_id = %s AND c.printer_id IS DISTINCT FROM %s"
        " ORDER BY c.id FOR UPDATE OF c",
        (printer_id, printer_id),
    ).fetchall()
    # Only these rows are in `filament_locations_name`.
    first: dict[str, UUID] = {}
    for child in children:
        if not child["named"]:
            continue
        other = first.setdefault(child["name"].upper(), child["id"])
        if other != child["id"]:
            raise ChildPlaceClashError(child["id"], child["name"], other)
    for child in children:
        try:
            LocationStore.update_on(conn, child["id"], m.LocationPatch(parent_id=parent))
        except LocationExistsError:
            raise ChildPlaceClashError(child["id"], child["name"]) from None
    return len(children)


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

    @property
    def pool(self) -> ConnectionPool[Conn]:
        return self._pool

    def get(self, location_id: UUID) -> m.Location | None:
        with self._pool.connection() as conn:
            return self._get(conn, location_id)

    def ams_units(self, printer_id: UUID) -> dict[int, m.Location]:
        """The printer's unarchived AMS unit places (no tray), by unit (#7153)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM filament_locations WHERE printer_id = %s"
                " AND ams_id IS NOT NULL AND tray_id IS NULL AND archived_at IS NULL",
                (printer_id,),
            ).fetchall()
        return {row["ams_id"]: m.Location.model_validate(row) for row in rows}

    def name_ams_unit(self, printer_id: UUID, ams_id: int, name: str | None) -> m.Location:
        """The printer's AMS unit place, made if missing (`ensure_ams_unit_on`), named
        ``name``, or ``<printer> <unit>`` again for ``None`` (#7153, R-7153-8).
        A printer's places are outside the name index (`filament_locations_name` is
        ``WHERE printer_id IS NULL``), so no name clashes. `NoAmsKindError`."""
        with self._pool.connection() as conn, conn.transaction():
            unit = self.ensure_ams_unit_on(conn, printer_id, ams_id)
            if name is None:
                printer = conn.execute(
                    "SELECT name FROM printers WHERE id = %s", (printer_id,)
                ).fetchone()
                if printer is None:
                    raise CatalogRefError()
                name = f"{printer['name']} {unit_label(ams_id)}"
            row = conn.execute(
                "UPDATE filament_locations SET name = %s, updated_at = now() WHERE id = %s"
                f" RETURNING {_COLUMNS}",
                (name, unit),
            ).fetchone()
        assert row is not None
        return m.Location.model_validate(row)

    def get_many(self, location_ids: Sequence[UUID]) -> dict[UUID, m.Location]:
        """Each place among ``location_ids`` that exists, by id, in one query (the labels
        PDF, #7157)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM filament_locations WHERE id = ANY(%s)",
                (list(location_ids),),
            ).fetchall()
        return {row["id"]: m.Location.model_validate(row) for row in rows}

    def detail(self, location_id: UUID) -> m.LocationDetail | None:
        """A place with its code, kind, path from the root (this place excluded, archived
        ancestors too), unarchived children and unarchived spools: four queries. An
        archived place still resolves."""
        with self._pool.connection() as conn:
            place = self._get(conn, location_id)
            if place is None:
                return None
            kind = conn.execute(
                "SELECT name FROM filament_location_kinds WHERE id = %s", (place.kind_id,)
            ).fetchone()
            assert kind is not None
            ancestors = conn.execute(
                "WITH RECURSIVE up AS (SELECT id, name, number, parent_id, 1 AS depth"
                " FROM filament_locations WHERE id = %s"
                " UNION ALL SELECT l.id, l.name, l.number, l.parent_id, up.depth + 1"
                " FROM filament_locations l JOIN up ON l.id = up.parent_id)"
                # A cycle (a bad row) ends the walk instead of looping until the timeout.
                " CYCLE id SET looped USING trail"
                " SELECT id, name, number FROM up WHERE id <> %s AND NOT looped"
                " ORDER BY depth DESC",
                (place.parent_id, location_id),
            ).fetchall()
            children = conn.execute(
                "SELECT id, name, number FROM filament_locations"
                " WHERE parent_id = %s AND archived_at IS NULL"
                " ORDER BY sort_order, upper(name), id",
                (location_id,),
            ).fetchall()
            spools = conn.execute(
                "SELECT s.id, s.number, s.grams_left, COALESCE(s.rgba, t.rgba) AS colour_rgba,"
                " mt.name AS material FROM filament_spools s"
                " LEFT JOIN filament_spool_types t ON t.id = s.type_id"
                " LEFT JOIN filament_materials mt ON mt.id = COALESCE(s.material_id, t.material_id)"
                " WHERE s.location_id = %s AND s.archived_at IS NULL ORDER BY s.number",
                (location_id,),
            ).fetchall()

        def step(row: dict[str, Any]) -> m.PathStep:
            return m.PathStep(id=row["id"], name=row["name"], code=format_code("L", row["number"]))

        return m.LocationDetail.model_validate(
            place.model_dump()
            | {
                "code": format_code("L", place.number),
                "kind_name": kind["name"],
                "path": [step(r) for r in ancestors],
                "children": [step(r) for r in children],
                "spools": [m.PlacedSpool(**r, code=format_code("S", r["number"])) for r in spools],
            }
        )

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
        the tree lock and the place's row held. `BoxHasManyError` when the place ends as a
        dry box (a dry-storage kind, capacity 1) that holds two spools or more (#7167)."""
        with self._pool.connection() as conn, conn.transaction():
            return self.update_on(conn, location_id, patch)

    @staticmethod
    def update_on(conn: Conn, location_id: UUID, patch: m.LocationPatch) -> m.Location | None:
        """`update` on ``conn``, in the caller's transaction (a printer's delete,
        `release_printer_child_places_on`). `LocationExistsError` or `CatalogRefError`
        leaves that transaction aborted: the caller rolls it back. `BoxHasManyError` is
        raised as in `update`."""
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
            if "parent_id" in patch.model_fields_set:
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_TREE_LOCK,))
            if LocationStore._lock(conn, location_id) is None:
                return None
            if patch.parent_id is not None and "parent_id" in patch.model_fields_set:
                LocationStore._no_cycle(conn, location_id, patch.parent_id)
            row = conn.execute(
                f"UPDATE filament_locations SET {', '.join(sets)} WHERE id = %s"
                f" RETURNING {_COLUMNS}",
                [*values, location_id],
            ).fetchone()
            check_not_many(conn, location_id)
            if patch.archived:
                forget_sync(conn, "location", location_id)
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
        with self._pool.connection() as conn:
            return self.ensure_ams_on(conn, printer_id, ams_id, tray_id, label=label)

    @staticmethod
    def ensure_ams_on(
        conn: Conn, printer_id: UUID, ams_id: int, tray_id: int, *, label: str
    ) -> m.Location:
        """`ensure_ams` on ``conn``, in the caller's transaction (`filament/capture.py`).
        The tray's place hangs under its unit's place (`ensure_ams_unit_on`), also when it
        was made before the unit's place existed; an external holder hangs under the printer's
        place (`ensure_printer_on`), or under none while no kind is flagged ``printer``."""
        select = (
            f"SELECT {_COLUMNS} FROM filament_locations WHERE printer_id = %s AND ams_id = %s"
            " AND tray_id = %s AND archived_at IS NULL"
        )
        key = (printer_id, ams_id, tray_id)
        LocationStore.lock_printer_on(conn, printer_id)
        try:
            # The external holder's parent is the printer's place.
            found = conn.execute(select, key).fetchone()
            root = None
            if ams_id == EXTERNAL and not (
                found is not None
                and found["parent_id"] is not None
                and conn.execute(
                    "SELECT 1 FROM filament_locations WHERE id = %s AND archived_at IS NULL",
                    (found["parent_id"],),
                ).fetchone()
            ):
                # Only a holder with no live parent needs the printer's place: making it
                # also hangs that holder under it, so the holder is read again.
                root = LocationStore._printer_parent(conn, printer_id)
                found = conn.execute(select, key).fetchone()
            if found is not None and found["parent_id"] is not None:
                if ams_id != EXTERNAL:
                    LocationStore.hang_orphans_on(conn, printer_id)
                return m.Location.model_validate(found)
            try:
                unit = (
                    root
                    if ams_id == EXTERNAL
                    else LocationStore.ensure_ams_unit_on(conn, printer_id, ams_id)
                )
            except NoAmsKindError:
                if found is None:
                    raise
                return m.Location.model_validate(found)  # the tray never needs its unit's place
            if found is None:
                kind = conn.execute(
                    "SELECT id FROM filament_location_kinds WHERE ams AND archived_at IS NULL"
                    " ORDER BY sort_order, upper(name), id LIMIT 1"
                ).fetchone()
                if kind is None:
                    raise NoAmsKindError()
                found = conn.execute(
                    "INSERT INTO filament_locations"
                    " (name, kind_id, parent_id, printer_id, ams_id, tray_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (printer_id, ams_id, tray_id)"
                    " WHERE printer_id IS NOT NULL AND archived_at IS NULL DO NOTHING"
                    f" RETURNING {_COLUMNS}",
                    (label, kind["id"], unit, *key),
                ).fetchone()
            if found is None:
                found = conn.execute(select, key).fetchone()
            if unit is not None and found is not None and found["parent_id"] is None:
                found = (
                    conn.execute(
                        "UPDATE filament_locations l SET parent_id = %s, updated_at = now()"
                        f" WHERE l.id = %s{_NOT_ABOVE} RETURNING {_COLUMNS}",
                        (unit, found["id"], unit),
                    ).fetchone()
                    or found
                )
        except errors.UniqueViolation:
            raise LocationExistsError() from None
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        assert found is not None
        return m.Location.model_validate(found)

    @staticmethod
    def ensure_ams_unit_on(conn: Conn, printer_id: UUID, ams_id: int) -> UUID:
        """The id of the unarchived place of that printer's AMS unit (no tray), made as
        ``<printer> <unit>`` (``H2C AMS-A``, ``H2C HT-A``, as the tray places are named) of
        the first ``ams`` kind if there is none (#6268). It hangs under the printer's place
        (`ensure_printer_on`), also when it has no parent or its parent is archived; with
        no ``printer`` kind it hangs under none. Idempotent and safe when two calls
        race. `NoAmsKindError`, or `CatalogRefError` for an unknown printer."""
        select = (
            "SELECT id, parent_id, NOT EXISTS (SELECT 1 FROM filament_locations p"
            " WHERE p.id = l.parent_id AND p.archived_at IS NULL) AS orphan"
            " FROM filament_locations l WHERE printer_id = %s AND ams_id = %s"
            " AND tray_id IS NULL AND archived_at IS NULL"
        )
        key = (printer_id, ams_id)
        LocationStore.lock_printer_on(conn, printer_id)
        try:
            found = conn.execute(select, key).fetchone()
            if found is None:
                kind = conn.execute(
                    "SELECT id FROM filament_location_kinds WHERE ams AND archived_at IS NULL"
                    " ORDER BY sort_order, upper(name), id LIMIT 1"
                ).fetchone()
                if kind is None:
                    raise NoAmsKindError()
                printer = conn.execute(
                    "SELECT name FROM printers WHERE id = %s", (printer_id,)
                ).fetchone()
                if printer is None:
                    raise CatalogRefError()
                found = conn.execute(
                    "INSERT INTO filament_locations (name, kind_id, parent_id, printer_id, ams_id)"
                    " VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (printer_id, ams_id) WHERE tray_id IS NULL"
                    " AND printer_id IS NOT NULL AND ams_id IS NOT NULL"
                    " AND archived_at IS NULL DO NOTHING"
                    " RETURNING id, parent_id, false AS orphan",
                    (
                        f"{printer['name']} {unit_label(ams_id)}",
                        kind["id"],
                        LocationStore._printer_parent(conn, printer_id),
                        *key,
                    ),
                ).fetchone()
            if found is None:
                found = conn.execute(select, key).fetchone()
            if found is not None and found["orphan"]:
                parent = LocationStore._printer_parent(conn, printer_id)
                if parent is not None:
                    found = (
                        conn.execute(
                            "UPDATE filament_locations l SET parent_id = %s, updated_at = now()"
                            f" WHERE l.id = %s{_NOT_ABOVE}"
                            " RETURNING id, parent_id, false AS orphan",
                            (parent, found["id"], parent),
                        ).fetchone()
                        or found
                    )
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        assert found is not None
        unit: UUID = found["id"]
        return unit

    @staticmethod
    def lock_printer_on(conn: Conn, printer_id: UUID) -> None:
        """Take the printer's place lock, held to the end of ``conn``'s transaction
        (#7081). Every `ensure_*` takes it before it reads or writes a place of the
        printer, so a labels request (`filament/labels.py`, units first, then slots) and a
        capture (`filament/capture.py`, each slot's unit just before the slot) make a
        printer's places one at a time and never wait on each other's new rows in opposite
        orders. An advisory lock, not a row lock on the printer's place: it needs no
        ``printer`` kind, so a capture with none still runs. Taking it again in the same
        transaction is free."""
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{_PRINTER_LOCK}{printer_id}",)
        )

    def ensure_printer(self, printer_id: UUID) -> UUID:
        """The id of the printer's own place; `ensure_printer_on` on a pool connection."""
        with self._pool.connection() as conn:
            return self.ensure_printer_on(conn, printer_id)

    @staticmethod
    def ensure_printer_on(conn: Conn, printer_id: UUID) -> UUID:
        """The id of the printer's own place (its unarchived row with ``printer_root``),
        made named as the printer, of the first unarchived kind flagged ``printer`` if
        there is none (#6955). A person's place beside the printer is never the root. Every
        AMS unit place and the external holder's tray of the printer with no parent, or an
        archived one, is moved under it. Idempotent and safe when two calls race.
        `NoPrinterKindError`, or `CatalogRefError` for an unknown printer."""
        select = (
            "SELECT id FROM filament_locations WHERE printer_id = %s AND printer_root"
            " AND archived_at IS NULL"
        )
        LocationStore.lock_printer_on(conn, printer_id)
        try:
            found = conn.execute(select, (printer_id,)).fetchone()
            if found is None:
                kind = conn.execute(
                    "SELECT id FROM filament_location_kinds WHERE printer AND archived_at IS NULL"
                    " ORDER BY sort_order, upper(name), id LIMIT 1"
                ).fetchone()
                if kind is None:
                    raise NoPrinterKindError()
                printer = conn.execute(
                    "SELECT name FROM printers WHERE id = %s", (printer_id,)
                ).fetchone()
                if printer is None:
                    raise CatalogRefError()
                found = conn.execute(
                    "INSERT INTO filament_locations (name, kind_id, printer_id, printer_root)"
                    " VALUES (%s, %s, %s, true)"
                    " ON CONFLICT (printer_id) WHERE printer_root AND archived_at IS NULL"
                    " DO NOTHING RETURNING id",
                    (printer["name"], kind["id"], printer_id),
                ).fetchone()
            if found is None:
                found = conn.execute(select, (printer_id,)).fetchone()
            assert found is not None
            conn.execute(
                "UPDATE filament_locations l SET parent_id = %s, updated_at = now()"
                " WHERE l.printer_id = %s AND l.archived_at IS NULL AND NOT l.printer_root"
                " AND ((l.ams_id IS NOT NULL AND l.tray_id IS NULL)"
                " OR (l.ams_id = %s AND l.tray_id IS NOT NULL))"
                " AND NOT EXISTS (SELECT 1 FROM filament_locations p"
                " WHERE p.id = l.parent_id AND p.archived_at IS NULL)" + _NOT_ABOVE,
                (found["id"], printer_id, EXTERNAL, found["id"]),
            )
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        place: UUID = found["id"]
        return place

    @staticmethod
    def hang_orphans_on(conn: Conn, printer_id: UUID) -> None:
        """Make the printer's place (`ensure_printer_on`) when one of its AMS unit places
        or its external holder has no parent or an archived one, so an install made before
        printer places gets its root and its units hung under it on the next capture
        (`filament/capture.py` `reconcile`). One read when nothing is orphaned; with no
        ``printer`` kind it does nothing (`_printer_parent`)."""
        orphan = conn.execute(
            "SELECT 1 FROM filament_locations l WHERE l.printer_id = %s"
            " AND l.archived_at IS NULL AND NOT l.printer_root"
            " AND ((l.ams_id IS NOT NULL AND l.tray_id IS NULL)"
            " OR (l.ams_id = %s AND l.tray_id IS NOT NULL))"
            " AND NOT EXISTS (SELECT 1 FROM filament_locations p"
            " WHERE p.id = l.parent_id AND p.archived_at IS NULL) LIMIT 1",
            (printer_id, EXTERNAL),
        ).fetchone()
        if orphan is not None:
            LocationStore._printer_parent(conn, printer_id)

    @staticmethod
    def _printer_parent(conn: Conn, printer_id: UUID) -> UUID | None:
        """The printer's place for a unit or the external holder to hang under; ``None``
        while no kind is flagged ``printer``, so a capture never fails for it."""
        try:
            return LocationStore.ensure_printer_on(conn, printer_id)
        except NoPrinterKindError:
            return None

    def move(self, spool_id: UUID, location_id: UUID | None, *, actor: str) -> m.SpoolEvent | None:
        """Put a spool in a place (``None``: in none) and write its ``moved`` event,
        ``data={"from": old, "to": new}``. ``None`` when it is there already: nothing is
        written. `LookupError` for no such spool, `MoveTargetError` for a place that does
        not exist, is archived or is an AMS unit's place (a printer, an ``ams_id`` and no
        tray: a unit holds trays, not spools). A printer's place with no ``ams_id`` (a shelf
        beside it) takes spools.
        Locks the spool, then the place."""
        with self._pool.connection() as conn, conn.transaction():
            return self.move_on(conn, spool_id, location_id, actor=actor)

    @staticmethod
    def move_on(
        conn: Conn,
        spool_id: UUID,
        location_id: UUID | None,
        *,
        actor: str,
        at: datetime | None = None,
        reason: str | None = None,
    ) -> m.SpoolEvent | None:
        """`move` on ``conn``, in the caller's transaction (`filament/inventory.py`
        `FilamentInventory.link`). A move through `move`/`move_on` clears the spool's
        ``tray_fingerprint``: what a tray showed when the spool was placed there says
        nothing of its new place. A printer merge (`repoint_printer_locations`) keeps it,
        since the spool stays in the same tray. ``at`` is when it happened (a report's
        time, `filament/capture.py`); with none the ledger books it now. A ``reason`` is
        the event's ``data.reason`` (a tray reconcile's, #6713)."""
        spool = conn.execute(
            "SELECT location_id FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,)
        ).fetchone()
        if spool is None:
            raise LookupError(f"no spool {spool_id}")
        before: UUID | None = spool["location_id"]
        if before == location_id:
            return None
        if location_id is not None:
            # Every target is locked FOR NO KEY UPDATE, the lock `occupant_on` takes too, so
            # none is upgraded (two upgrades of one SHARE lock deadlock, #7167).
            target = conn.execute(
                "SELECT printer_id, ams_id, tray_id FROM filament_locations"
                " WHERE id = %s AND archived_at IS NULL FOR NO KEY UPDATE",
                (location_id,),
            ).fetchone()
            if target is None or (
                target["printer_id"] is not None
                and target["ams_id"] is not None
                and target["tray_id"] is None
            ):
                raise MoveTargetError()
            held = occupant_on(conn, location_id, but=spool_id)
            if held is not None:
                raise BoxFullError(held)
        conn.execute(
            "UPDATE filament_spools SET location_id = %s, tray_fingerprint = NULL,"
            " updated_at = now() WHERE id = %s",
            (location_id, spool_id),
        )
        keep_home_on(conn, spool_id, location_id)
        return ledger.record(
            conn,
            spool_id,
            "moved",
            actor=actor,
            at=at,
            data={"from": before, "to": location_id}
            | ({} if reason is None else {"reason": reason}),
        )

    @staticmethod
    def link_bb_on(
        conn: Conn, bb_id: int, name: str, identifier: str | None
    ) -> tuple[UUID, BbLinked] | None:
        """The place Bambuddy's location ``bb_id`` is, on ``conn`` (#6265 Task 9, the pull):
        the one linked to it (``found``), else the unlinked, unarchived root place of the
        same upper case name that is no printer's place (F18), which takes the link
        (``linked``), else a new root place of kind ``Shelf`` with ``name`` and
        ``identifier`` (``made``). ``None`` when another root place holds the name, or no
        unarchived kind is named ``Shelf``. A link is bookkeeping: it does not move
        ``updated_at``."""
        found = conn.execute(
            "SELECT id FROM filament_locations WHERE bb_location_id = %s", (bb_id,)
        ).fetchone()
        if found is not None:
            return found["id"], "found"
        linked = conn.execute(
            "UPDATE filament_locations SET bb_location_id = %s WHERE id = (SELECT id"
            " FROM filament_locations WHERE upper(name) = upper(%s) AND parent_id IS NULL"
            " AND printer_id IS NULL AND archived_at IS NULL AND bb_location_id IS NULL"
            " ORDER BY id LIMIT 1) RETURNING id",
            (bb_id, name),
        ).fetchone()
        if linked is not None:
            return linked["id"], "linked"
        if conn.execute(
            "SELECT 1 FROM filament_locations WHERE upper(name) = upper(%s) AND parent_id IS NULL"
            " AND printer_id IS NULL AND archived_at IS NULL",
            (name,),
        ).fetchone():
            return None
        kind = conn.execute(
            "SELECT id FROM filament_location_kinds WHERE upper(name) = 'SHELF'"
            " AND archived_at IS NULL"
        ).fetchone()
        if kind is None:
            return None
        made = conn.execute(
            "INSERT INTO filament_locations (name, kind_id, identifier, bb_location_id)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (name, kind["id"], identifier, bb_id),
        ).fetchone()
        assert made is not None
        return made["id"], "made"

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
    ``merged`` has moves as it is, and so does its printer's own place when ``into`` has none
    (two of them clash, as two trays do), which then takes ``into``'s printer name. Where
    both printers have an unarchived place for one tray, ``into``'s stands: each spool in
    ``merged``'s moves there with a ``moved`` event, its places move under it, and
    ``merged``'s is archived (not deleted: the ledger's ``moved`` events name it). Archived
    places move as they are.

    Locks the spools before the places, as `LocationStore.move` does."""
    gone = conn.execute(
        "SELECT m.id, k.id AS kept FROM filament_locations m LEFT JOIN filament_locations k"
        " ON k.printer_id = %s AND k.archived_at IS NULL AND ((m.printer_root AND k.printer_root)"
        " OR (NOT m.printer_root AND NOT k.printer_root AND k.ams_id = m.ams_id"
        " AND k.tray_id IS NOT DISTINCT FROM m.tray_id))"
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
            "UPDATE filament_location_sensors SET location_id = %s, updated_at = now()"
            " WHERE location_id = %s",
            (kept, old),
        )
        conn.execute(
            "UPDATE filament_locations SET parent_id = %s, updated_at = now() WHERE parent_id = %s",
            (kept, old),
        )
        conn.execute(
            "UPDATE filament_locations SET archived_at = now(), updated_at = now() WHERE id = %s",
            (old,),
        )
    # A root only ``merged`` had moves to ``into`` and is named as ``into`` (#7081); a
    # place with a printer is outside the name index, so the name cannot clash.
    conn.execute(
        "UPDATE filament_locations l SET name = p.name, updated_at = now() FROM printers p"
        " WHERE p.id = %s AND l.printer_id = %s AND l.printer_root AND l.archived_at IS NULL",
        (into, merged),
    )
    conn.execute(
        "UPDATE filament_locations SET printer_id = %s, updated_at = now() WHERE printer_id = %s",
        (into, merged),
    )
