"""`printers`: the registry's rows (#3823, plan 2026-10-16-printer-identity-native-path,
Task 2).

The access code is sealed under the KEK (`core/secrets.py` `seal_secret`) with the AAD
``printer:<id>:<serial>``, so a sealed value copied onto another row does not open there.
`open_access_code` is the only read of the sealed columns; nothing else selects them,
and no exception here carries the code.

Every write that changes which row holds a serial or a Bambuddy id runs under one
transaction-scoped advisory lock, so two concurrent `ensure_bambuddy` calls (two
``/printers/now`` reads) never race into a unique violation.

A row's id is the printer's identity on every surface (spec §6.1), so a row is never
just deleted when another row turns out to be the same printer (a synthetic row whose
serial is learned, a Bambuddy link taken over): `_merge` moves everything that names the
gone row onto the kept one in the same transaction. Its list is `PRINTER_REFERENCES`;
a table that names a printer adds itself there (`tests/printers/test_store.py` checks
that every foreign key to ``printers`` is listed), in one of two forms:

- a `PrinterReference` (table, column), for a plain column or one row per printer;
- a `PrinterReference` with ``repoint``, a `RepointHook` ``hook(conn, merged, into)``,
  for a table whose key holds more than the printer and whose conflicts only it can
  settle. It runs on the merge's connection, inside its transaction, while both rows
  still exist; raising undoes the merge. ``tray_spool_links`` (#4180, primary key
  ``(printer_id, ams_id, tray_id)``) is registered this way with
  ``spools.store.repoint_tray_links``, in which ``into``'s link wins (#4190). A bare
  hook is accepted too, for a reference with no column to name: Task 8's print-run
  activity facet is one (`_repoint_run_activity`).

A table that names a printer also carries ``REFERENCES printers (id) ON DELETE
CASCADE`` (``tray_spool_links``: 20261019T0304Z), so a delete takes its rows with it
and the test above sees it; ``print_runs.printer`` is ``ON DELETE SET NULL`` instead
(20261020T0327Z), since a run's history outlives its printer."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.core.events import EventBus, PrinterEvent, emit
from scadbuddy.core.secrets import Envelope, Kek, open_secret, seal_secret
from scadbuddy.printers.models import (
    BambuddyView,
    LanLink,
    PrinterCreate,
    PrinterPatch,
    PrinterRow,
    is_synthetic_serial,
    synthetic_serial,
    synthetic_vp_serial,
)

Pool = ConnectionPool[Connection[DictRow]]

_COLUMNS = (
    "id, serial, name, model, bambuddy_printer_id, bambuddy_unlinked_id, lan_host, lan_cert_sha256,"
    " access_code_sealed IS NOT NULL AS has_access_code, preferred_path, bambuddy_active,"
    " kind, bambuddy_vp_id, forwards_to, vp_mode, bambuddy_view, lan_sync_error,"
    " lan_move_pending, created_at, updated_at"
)
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('scadbuddy:printers', 0))"


class PrinterNotFoundError(LookupError):
    def __init__(self, printer_id: str) -> None:
        super().__init__(f"no printer {printer_id}")
        self.printer_id = printer_id


class SerialTakenError(Exception):
    """Another row already holds the serial."""

    def __init__(self, existing_id: str) -> None:
        super().__init__(f"printer {existing_id} already has this serial")
        self.existing_id = existing_id


class BambuddyLinkTakenError(Exception):
    """Another row (or this one, to another Bambuddy printer) already holds the link."""

    def __init__(self, existing_id: str, bambuddy_printer_id: int) -> None:
        super().__init__(
            f"printer {existing_id} is already linked to Bambuddy printer {bambuddy_printer_id}"
        )
        self.existing_id = existing_id
        self.bambuddy_printer_id = bambuddy_printer_id


class VirtualPrinterRowError(Exception):
    """A Bambuddy virtual printer's row takes no Bambuddy printer link (#4747)."""

    def __init__(self, printer_id: str) -> None:
        super().__init__(f"printer {printer_id} is a Bambuddy virtual printer")
        self.printer_id = printer_id


class LanLinkChangedError(Exception):
    """The row's LAN link changed while the Bambuddy sync probed: the user's own
    link wins (#4747 item 2, "never overwrites a LAN link that already exists")."""

    def __init__(self, printer_id: str) -> None:
        super().__init__(f"printer {printer_id}'s LAN link changed meanwhile")
        self.printer_id = printer_id


#: `link_lan`'s ``expect_host`` when any current link may be replaced (the user's own
#: link through ``PUT /printers/{id}/lan``).
ANY_HOST = object()


class KekMissingError(Exception):
    def __init__(self) -> None:
        super().__init__("LAN printing needs SCADBUDDY_SECRET_KEY_FILE")


class NoAccessCodeError(LookupError):
    def __init__(self, printer_id: str) -> None:
        super().__init__(f"printer {printer_id} has no LAN access code")
        self.printer_id = printer_id


class SerialUnknownError(ValueError):
    """A LAN link needs the printer's real serial: its MQTT topics are named by it."""

    def __init__(self, printer_id: str) -> None:
        super().__init__(f"printer {printer_id}'s serial is not known, so it cannot be linked")
        self.printer_id = printer_id


#: ``hook(conn, merged, into)``: repoint one table's references from ``merged`` to
#: ``into`` inside the merge's transaction (module docstring).
RepointHook = Callable[[Connection[DictRow], str, str], None]


@dataclass(frozen=True)
class PrinterReference:
    """A column naming a printer by its registry id. ``unique``: one row per printer
    (the gone row's is dropped when the kept row has one). ``repoint``: the table's own
    hook, called instead of the generic update."""

    table: str
    column: str
    unique: bool = False
    repoint: RepointHook | None = None


def _repoint_tray_links(conn: Connection[DictRow], merged: str, into: str) -> None:
    # Imported here: `spools` is a feature of its own, and the registry needs it only
    # while merging.
    from scadbuddy.spools.store import repoint_tray_links

    repoint_tray_links(conn, uuid.UUID(merged), uuid.UUID(into))


def _repoint_run_activity(conn: Connection[DictRow], merged: str, into: str) -> None:
    # A print run's activity row names its printer in the ``printer`` facet (#3823 Task
    # 8), a JSON value rather than a column, so no foreign key keeps it.
    conn.execute(
        "UPDATE activity SET facets = jsonb_set(facets, '{printer}', to_jsonb(%s::text))"
        " WHERE kind = 'print_run' AND facets->>'printer' = %s",
        (into, merged),
    )


def _forget_run_activity(conn: Connection[DictRow], printer_id: str) -> None:
    conn.execute(
        "UPDATE activity SET facets = facets - 'printer'"
        " WHERE kind = 'print_run' AND facets->>'printer' = %s",
        (printer_id,),
    )


#: Everything a merge repoints (I7 of the #4186 review). The extension point for every
#: later table keyed by ``printers.id``.
PRINTER_REFERENCES: tuple[PrinterReference | RepointHook, ...] = (
    PrinterReference("printer_reports", "printer_id", unique=True),
    PrinterReference("tray_spool_links", "printer_id", repoint=_repoint_tray_links),
    # #3823 Task 8: the printer a run printed on, and its activity row's facet.
    PrinterReference("print_runs", "printer"),
    # #4747: the printer a Bambuddy virtual printer forwards to.
    PrinterReference("printers", "forwards_to"),
    _repoint_run_activity,
)


def access_code_aad(printer_id: str, serial: str) -> str:
    return f"printer:{printer_id}:{serial}"


def printer_uuid(printer_id: str) -> uuid.UUID | None:
    """``printer_id`` as the column's uuid; ``None`` for anything that is not one, which
    then names no printer."""
    try:
        return uuid.UUID(printer_id)
    except (ValueError, TypeError, AttributeError):
        return None


#: What `PrinterStore.upsert_virtual` did.
VirtualOutcome = Literal["already_linked", "linked", "created", "conflict"]


def adopts_for_virtual(holder: PrinterRow, *, may_adopt: bool) -> bool:
    """Whether a virtual printer takes over ``holder``, the row holding its serial
    (#4747, security review of the sync): only a physical row with no Bambuddy link
    now or before (a LAN-only or hand-made row), and only when the caller allows it
    (``may_adopt``: the VP is not in ``proxy`` mode, whose serial is its target's, and
    no Bambuddy printer reports that serial). Anything else keeps its identity."""
    return (
        may_adopt
        and holder.kind == "physical"
        and holder.bambuddy_printer_id is None
        and holder.bambuddy_unlinked_id is None
    )


def _same_view(old: BambuddyView | None, new: BambuddyView) -> bool:
    """Whether Bambuddy reports what it did at the last sync (``seen_at`` aside)."""
    if old is None:
        return False
    return old.model_dump(exclude={"seen_at"}) == new.model_dump(exclude={"seen_at"})


def _row(data: dict[str, Any]) -> PrinterRow:
    fields = {key: value for key, value in data.items() if key != "declined"}
    forwards_to = data.get("forwards_to")
    return PrinterRow.model_validate(
        {
            **fields,
            "id": str(data["id"]),
            "forwards_to": None if forwards_to is None else str(forwards_to),
        }
    )


class PrinterStore:
    def __init__(
        self,
        pool: Pool,
        kek: Kek | None,
        *,
        events: EventBus | None = None,
        previous_kek: Kek | None = None,
    ) -> None:
        self.pool = pool
        self.kek = kek
        #: The KEK before a rotation (`SCADBUDDY_SECRET_KEY_PREVIOUS_FILE`), tried after
        #: ``kek`` where a value sealed before the rotation may still be opened (#4888).
        self.previous_kek = previous_kek
        #: Where a merge announces the two rows it touched (``printer.changed``).
        self.events = events

    # ── reads ─────────────────────────────────────────────────────────────────────

    def _one(self, conn: Connection[DictRow], where: str, value: object) -> PrinterRow | None:
        if where == "id":
            value = printer_uuid(str(value))
            if value is None:
                return None
        found = conn.execute(f"SELECT {_COLUMNS} FROM printers WHERE {where} = %s", (value,))
        data = found.fetchone()
        return None if data is None else _row(data)

    def get(self, printer_id: str) -> PrinterRow | None:
        with self.pool.connection() as conn:
            return self._one(conn, "id", printer_id)

    def by_serial(self, serial: str) -> PrinterRow | None:
        with self.pool.connection() as conn:
            return self._one(conn, "serial", serial)

    def by_bambuddy_id(self, bambuddy_printer_id: int) -> PrinterRow | None:
        with self.pool.connection() as conn:
            return self._one(conn, "bambuddy_printer_id", bambuddy_printer_id)

    def list(self) -> list[PrinterRow]:
        with self.pool.connection() as conn:
            rows = conn.execute(f"SELECT {_COLUMNS} FROM printers ORDER BY name, id").fetchall()
        return [_row(data) for data in rows]

    def require(self, printer_id: str) -> PrinterRow:
        row = self.get(printer_id)
        if row is None:
            raise PrinterNotFoundError(printer_id)
        return row

    def resolve_bambuddy_id(self, printer_id: str | int | None) -> int | None:
        """The Bambuddy integer behind a registry id, an integer passed through, or
        ``None``: the one way the tables still keyed by the Bambuddy printer id are
        reached from a registry id (plan, "Deferred to 3b")."""
        if printer_id is None or isinstance(printer_id, int):
            return printer_id
        row = self.get(printer_id)
        return None if row is None else row.bambuddy_printer_id

    # ── writes ────────────────────────────────────────────────────────────────────

    def create(self, body: PrinterCreate) -> PrinterRow:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(_LOCK)
            return self._insert(
                conn,
                serial=body.serial,
                name=body.name,
                model=body.model,
                preferred_path=body.preferred_path,
                bambuddy_printer_id=None,
            )

    def _insert(
        self,
        conn: Connection[DictRow],
        *,
        serial: str,
        name: str,
        model: str | None,
        preferred_path: str,
        bambuddy_printer_id: int | None,
    ) -> PrinterRow:
        existing = self._one(conn, "serial", serial)
        if existing is not None:
            raise SerialTakenError(existing.id)
        data = conn.execute(
            f"INSERT INTO printers (id, serial, name, model, preferred_path, bambuddy_printer_id)"
            f" VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
            (str(uuid.uuid4()), serial, name, model, preferred_path, bambuddy_printer_id),
        ).fetchone()
        assert data is not None
        return _row(data)

    def _update(
        self, conn: Connection[DictRow], printer_id: str, sets: str, args: tuple[Any, ...]
    ) -> PrinterRow:
        key = printer_uuid(printer_id)
        if key is None:
            raise PrinterNotFoundError(printer_id)
        data = conn.execute(
            f"UPDATE printers SET {sets}, updated_at = now() WHERE id = %s RETURNING {_COLUMNS}",
            (*args, key),
        ).fetchone()
        if data is None:
            raise PrinterNotFoundError(printer_id)
        return _row(data)

    def _merge(self, conn: Connection[DictRow], gone: str, kept: str) -> None:
        """Make ``kept`` the printer ``gone`` was: every `PRINTER_REFERENCES` row naming
        ``gone`` names ``kept`` (a unique one is dropped when ``kept`` has its own), then
        ``gone`` is deleted. In the caller's transaction, under its lock."""
        for ref in PRINTER_REFERENCES:
            if not isinstance(ref, PrinterReference):
                ref(conn, gone, kept)
                continue
            if ref.repoint is not None:
                ref.repoint(conn, gone, kept)
                continue
            if ref.unique:
                conn.execute(
                    f"DELETE FROM {ref.table} WHERE {ref.column} = %s AND EXISTS"
                    f" (SELECT 1 FROM {ref.table} WHERE {ref.column} = %s)",
                    (gone, kept),
                )
            conn.execute(
                f"UPDATE {ref.table} SET {ref.column} = %s WHERE {ref.column} = %s", (kept, gone)
            )
        conn.execute("DELETE FROM printers WHERE id = %s", (gone,))

    def _announce(self, *printer_ids: str) -> None:
        for printer_id in printer_ids:
            emit(self.events, PrinterEvent(kind="printer.changed", printer_id=printer_id))

    def patch(self, printer_id: str, body: PrinterPatch) -> PrinterRow:
        with self.pool.connection() as conn:
            current = self._one(conn, "id", printer_id)
            if current is None:
                raise PrinterNotFoundError(printer_id)
            return self._update(
                conn,
                printer_id,
                "name = %s, preferred_path = %s",
                (body.name or current.name, body.preferred_path or current.preferred_path),
            )

    def link_bambuddy(self, printer_id: str, bambuddy_printer_id: int) -> PrinterRow:
        """Link ``printer_id`` to a Bambuddy printer. A row `ensure_bambuddy` made for
        that Bambuddy printer while its serial was withheld (a synthetic serial, no LAN
        link) is merged into this one (`_merge`); any other holder refuses."""
        merged: str | None = None
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(_LOCK)
            row = self._one(conn, "id", printer_id)
            if row is None:
                raise PrinterNotFoundError(printer_id)
            if row.bambuddy_printer_id == bambuddy_printer_id:
                return row
            if row.kind != "physical":
                raise VirtualPrinterRowError(row.id)
            if row.bambuddy_printer_id is not None:
                raise BambuddyLinkTakenError(row.id, row.bambuddy_printer_id)
            holder = self._one(conn, "bambuddy_printer_id", bambuddy_printer_id)
            if holder is not None:
                if not (is_synthetic_serial(holder.serial) and holder.lan_host is None):
                    raise BambuddyLinkTakenError(holder.id, bambuddy_printer_id)
                self._merge(conn, holder.id, row.id)
                merged = holder.id
            linked = self._update(
                conn,
                row.id,
                "bambuddy_printer_id = %s, bambuddy_unlinked_id = NULL",
                (bambuddy_printer_id,),
            )
        if merged is not None:
            self._announce(merged, linked.id)
        return linked

    def mark_bambuddy_active(self, active: Iterable[int]) -> None:
        """Record which linked Bambuddy printers Bambuddy lists as active now, so a strip
        read while it is down shows the same ones. Writes only the rows that change."""
        ids = sorted(set(active))
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE printers SET bambuddy_active = (bambuddy_printer_id = ANY(%s)),"
                " updated_at = now() WHERE kind = 'physical'"
                " AND bambuddy_printer_id IS NOT NULL"
                " AND bambuddy_active IS DISTINCT FROM (bambuddy_printer_id = ANY(%s))",
                (ids, ids),
            )

    def mark_virtual_listed(self, listed: Iterable[int]) -> tuple[str, ...]:
        """Record which virtual printers Bambuddy lists now on their rows' ``bambuddy_active``
        (#4889): a VP's row is kept when Bambuddy drops the VP, marked no longer listed.
        Writes only the rows that change, and returns their ids. Each of this and
        `mark_bambuddy_active` writes ``bambuddy_active`` on its own kind of row only
        (#4921), so neither can overwrite the other's whatever links a row holds."""
        ids = sorted(set(listed))
        with self.pool.connection() as conn:
            changed = conn.execute(
                "UPDATE printers SET bambuddy_active = (bambuddy_vp_id = ANY(%s)),"
                " updated_at = now() WHERE kind = 'bambuddy_virtual'"
                " AND bambuddy_vp_id IS NOT NULL"
                " AND bambuddy_active IS DISTINCT FROM (bambuddy_vp_id = ANY(%s))"
                " RETURNING id",
                (ids, ids),
            ).fetchall()
        return tuple(str(row["id"]) for row in changed)

    def unlink_bambuddy(self, printer_id: str) -> PrinterRow:
        """Take the Bambuddy link off, and remember which it was, so the next strip read
        (`ensure_bambuddy`) gives that Bambuddy printer its own synthetic row rather than
        link this one again by its serial. Under the lock `ensure_bambuddy` takes."""
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(_LOCK)
            return self._update(
                conn,
                printer_id,
                "bambuddy_unlinked_id = COALESCE(bambuddy_printer_id, bambuddy_unlinked_id),"
                " bambuddy_printer_id = NULL",
                (),
            )

    def link_lan(
        self,
        printer_id: str,
        body: LanLink,
        *,
        cert_sha256: str,
        expect_host: object = ANY_HOST,
    ) -> PrinterRow:
        """Store the LAN link. ``expect_host`` (the Bambuddy sync's): the ``lan_host``
        the row must still have, ``None`` for none, read under the row's lock; another
        is `LanLinkChangedError` and nothing is written."""
        if self.kek is None:
            raise KekMissingError()
        with self.pool.connection() as conn, conn.transaction():
            key = printer_uuid(printer_id)
            if key is not None:
                conn.execute("SELECT 1 FROM printers WHERE id = %s FOR UPDATE", (key,))
            row = self._one(conn, "id", printer_id)
            if row is None:
                raise PrinterNotFoundError(printer_id)
            if expect_host is not ANY_HOST and row.lan_host != expect_host:
                raise LanLinkChangedError(printer_id)
            if is_synthetic_serial(row.serial):
                raise SerialUnknownError(printer_id)
            sealed = seal_secret(self.kek, body.access_code, access_code_aad(row.id, row.serial))
            return self._update(
                conn,
                printer_id,
                "lan_host = %s, lan_cert_sha256 = %s, access_code_sealed = %s,"
                " access_code_dek_sealed = %s, access_code_kek_id = %s, lan_sync_error = NULL,"
                " lan_move_pending = false",
                (
                    body.host,
                    cert_sha256,
                    sealed.secret_sealed,
                    sealed.dek_sealed,
                    sealed.kek_id,
                ),
            )

    def unlink_lan(self, printer_id: str) -> PrinterRow:
        with self.pool.connection() as conn, conn.transaction():
            row = self._one(conn, "id", printer_id)
            if row is None:
                raise PrinterNotFoundError(printer_id)
            conn.execute("DELETE FROM printer_reports WHERE printer_id = %s", (row.id,))
            return self._update(
                conn,
                printer_id,
                "lan_host = NULL, lan_cert_sha256 = NULL, access_code_sealed = NULL,"
                " access_code_dek_sealed = NULL, access_code_kek_id = NULL,"
                " lan_move_pending = false",
                (),
            )

    def open_access_code(self, printer_id: str) -> str:
        """The LAN access code, opened under the KEK. Raises `SealError` when the
        sealed value is not this row's (or another key sealed it)."""
        if self.kek is None:
            raise KekMissingError()
        key = printer_uuid(printer_id)
        if key is None:
            raise PrinterNotFoundError(printer_id)
        with self.pool.connection() as conn:
            data = conn.execute(
                "SELECT id, serial, access_code_sealed, access_code_dek_sealed,"
                " access_code_kek_id FROM printers WHERE id = %s",
                (key,),
            ).fetchone()
        if data is None:
            raise PrinterNotFoundError(printer_id)
        if data["access_code_sealed"] is None:
            raise NoAccessCodeError(printer_id)
        envelope = Envelope(
            secret_sealed=bytes(data["access_code_sealed"]),
            dek_sealed=bytes(data["access_code_dek_sealed"]),
            kek_id=data["access_code_kek_id"],
        )
        return open_secret(self.kek, envelope, access_code_aad(str(data["id"]), data["serial"]))

    def ensure_bambuddy(
        self,
        bambuddy_printer_id: int,
        *,
        serial: str | None,
        name: str,
        model: str | None,
        listed: set[int] | frozenset[int] | None = None,
    ) -> PrinterRow:
        """`ensure_bambuddy_written`'s row, for the callers that need only the row."""
        row, _ = self.ensure_bambuddy_written(
            bambuddy_printer_id, serial=serial, name=name, model=model, listed=listed
        )
        return row

    def ensure_bambuddy_written(
        self,
        bambuddy_printer_id: int,
        *,
        serial: str | None,
        name: str,
        model: str | None,
        listed: set[int] | frozenset[int] | None = None,
    ) -> tuple[PrinterRow, bool]:
        """The row for a Bambuddy printer, and whether this call wrote anything, decided
        under the lock (#4861: an adoption another caller made meanwhile is not this
        one's). The row is made when there is none: the row linking that
        id; else the row holding ``serial`` (which it links, if that row has no Bambuddy
        link, or links to one Bambuddy no longer lists: a re-added printer); else a new
        row, ``preferred_path`` ``bambuddy``. Without a serial (Bambuddy withheld it) the
        new row's serial is `synthetic_serial`, so an unlinked Bambuddy printer still
        gets exactly one row; once the serial is known a synthetic row adopts it, or is
        merged into the row that already holds it (`_merge`).

        ``listed`` is every id Bambuddy lists now; ``None`` treats every link as live.
        The known case (a row links this id and has nothing to adopt) is read without
        the lock, so a strip read waits on no writer."""
        with self.pool.connection() as conn:
            # One read: the linking row, and whether the serial's row is one the user
            # unlinked from this Bambuddy printer (then there is nothing to adopt).
            data = conn.execute(
                f"SELECT {_COLUMNS}, EXISTS (SELECT 1 FROM printers AS h WHERE h.serial = %s"
                " AND h.bambuddy_unlinked_id = printers.bambuddy_printer_id) AS declined"
                " FROM printers WHERE bambuddy_printer_id = %s",
                (serial, bambuddy_printer_id),
            ).fetchone()
        linked = None if data is None else _row(data)
        if linked is not None and (data is not None and data["declined"]):
            return linked, False
        if linked is not None and not self._adopts(linked, serial):
            return linked, False
        merged: tuple[str, str] | None = None
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(_LOCK)
            row, merged, wrote = self._ensure_locked(
                conn, bambuddy_printer_id, serial=serial, name=name, model=model, listed=listed
            )
        if merged is not None:
            self._announce(*merged)
        return row, wrote

    @staticmethod
    def _adopts(linked: PrinterRow, serial: str | None) -> bool:
        """Whether the row linking a Bambuddy printer has its serial still to learn."""
        return serial is not None and linked.serial != serial and is_synthetic_serial(linked.serial)

    def _ensure_locked(
        self,
        conn: Connection[DictRow],
        bambuddy_printer_id: int,
        *,
        serial: str | None,
        name: str,
        model: str | None,
        listed: set[int] | frozenset[int] | None,
    ) -> tuple[PrinterRow, tuple[str, str] | None, bool]:
        """The row, the merge it made (gone, kept), and whether it wrote."""

        def live(other: int | None) -> bool:
            return other is not None and (listed is None or other in listed)

        linked = self._one(conn, "bambuddy_printer_id", bambuddy_printer_id)
        if linked is not None and not self._adopts(linked, serial):
            return linked, None, False
        if serial is not None:
            holder = self._one(conn, "serial", serial)
            if holder is not None and holder.bambuddy_unlinked_id == bambuddy_printer_id:
                # The user unlinked this Bambuddy printer from the serial's row: it is
                # never linked again by serial. The Bambuddy printer keeps (or gets) its
                # synthetic row, as one whose serial is withheld does.
                if linked is not None:
                    return linked, None, False
                serial = None
        if serial is not None:
            holder = self._one(conn, "serial", serial)
            if holder is None and linked is not None:  # the synthetic row learns its serial
                return self._update(conn, linked.id, "serial = %s", (serial,)), None, True
            if (
                holder is not None
                and holder.kind == "physical"
                and not live(holder.bambuddy_printer_id)
            ):
                merged = None
                if linked is not None:  # the synthetic row made meanwhile is this printer
                    self._merge(conn, linked.id, holder.id)
                    merged = (linked.id, holder.id)
                row = self._update(
                    conn, holder.id, "bambuddy_printer_id = %s", (bambuddy_printer_id,)
                )
                return row, merged, True
            if linked is not None:  # the serial's row is another live Bambuddy printer's
                return linked, None, False
            if holder is None:
                return (
                    self._insert(
                        conn,
                        serial=serial,
                        name=name,
                        model=model,
                        preferred_path="bambuddy",
                        bambuddy_printer_id=bambuddy_printer_id,
                    ),
                    None,
                    True,
                )
            # Two live Bambuddy printers report one serial: this one gets a row of its
            # own, as one with no serial would (`link_bambuddy` refuses the pair, 409).
        own = self._one(conn, "serial", synthetic_serial(bambuddy_printer_id))
        if own is not None:  # its link was taken off; it is still this printer's
            return (
                self._update(conn, own.id, "bambuddy_printer_id = %s", (bambuddy_printer_id,)),
                None,
                True,
            )
        return (
            self._insert(
                conn,
                serial=synthetic_serial(bambuddy_printer_id),
                name=name,
                model=model,
                preferred_path="bambuddy",
                bambuddy_printer_id=bambuddy_printer_id,
            ),
            None,
            True,
        )

    # ── the Bambuddy sync (#4439, #4747) ──────────────────────────────────────────

    def create_linked(
        self, *, serial: str, name: str, model: str | None, bambuddy_printer_id: int
    ) -> PrinterRow:
        """A new row for a Bambuddy printer by its serial, linked, ``preferred_path``
        ``auto`` (#4439). Refuses (`SerialTakenError`, `BambuddyLinkTakenError`) when
        the strip's `ensure_bambuddy` or another sync made one meanwhile."""
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(_LOCK)
            holder = self._one(conn, "bambuddy_printer_id", bambuddy_printer_id)
            if holder is not None:
                raise BambuddyLinkTakenError(holder.id, bambuddy_printer_id)
            return self._insert(
                conn,
                serial=serial,
                name=name,
                model=model,
                preferred_path="auto",
                bambuddy_printer_id=bambuddy_printer_id,
            )

    def record_bambuddy_view(
        self, printer_id: str, view: BambuddyView, *, model: str | None = None
    ) -> tuple[PrinterRow, bool]:
        """Store what Bambuddy reports for the row, and say whether that wrote anything
        (judged under the row's lock, so a change by someone else is not this one). Its
        name follows Bambuddy's only while it is still the name Bambuddy last gave (the
        user has not renamed it); its model is filled only when it has none (#4439
        "Refresh model/name only if the user hasn't renamed it")."""
        with self.pool.connection() as conn, conn.transaction():
            # Under the row's lock, as `link_lan`: a rename landing between the read and
            # the write would otherwise be reverted.
            key = printer_uuid(printer_id)
            if key is not None:
                conn.execute("SELECT 1 FROM printers WHERE id = %s FOR UPDATE", (key,))
            row = self._one(conn, "id", printer_id)
            if row is None:
                raise PrinterNotFoundError(printer_id)
            name = row.name
            if row.bambuddy_view is not None and row.name == row.bambuddy_view.name:
                name = view.name
            if (
                _same_view(row.bambuddy_view, view)
                and name == row.name
                and (row.model is not None or model is None)
            ):
                return row, False  # a sync that changes nothing writes nothing
            return (
                self._update(
                    conn,
                    printer_id,
                    "bambuddy_view = %s, name = %s, model = COALESCE(model, %s)",
                    (Jsonb(view.model_dump(mode="json")), name, model),
                ),
                True,
            )

    def set_lan_sync_error(
        self, printer_id: str, error: str | None, *, move_pending: bool | None = None
    ) -> PrinterRow:
        """Record why the sync could not fill or move the LAN link; ``move_pending``
        (`PrinterRow.lan_move_pending`) is left as it is when ``None``."""
        with self.pool.connection() as conn:
            if move_pending is None:
                return self._update(conn, printer_id, "lan_sync_error = %s", (error,))
            return self._update(
                conn,
                printer_id,
                "lan_sync_error = %s, lan_move_pending = %s",
                (error, move_pending),
            )

    def upsert_virtual(
        self,
        vp_id: int,
        *,
        serial: str | None,
        name: str,
        model: str | None,
        mode: str,
        forwards_to: str | None,
        view: BambuddyView,
        may_adopt: bool = True,
    ) -> tuple[PrinterRow, VirtualOutcome, bool]:
        """The row for Bambuddy virtual printer ``vp_id``, what happened to it, and
        whether anything was written:
        ``already_linked`` (the row with that VP id, its mode, target and view brought
        up to date, its name only if the user has not renamed it), ``linked`` (a
        physical row holding the VP's serial, with no Bambuddy link of either kind,
        adopted when `adopts_for_virtual` allows; its LAN link stays), ``created`` (a
        new row) or ``conflict`` (another row holds the serial and keeps it: a new row
        under `synthetic_vp_serial`, never an overwrite)."""
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(_LOCK)
            fields = "vp_mode = %s, forwards_to = %s, bambuddy_view = %s"
            args: tuple[Any, ...] = (
                mode,
                None if forwards_to is None else uuid.UUID(forwards_to),
                Jsonb(view.model_dump(mode="json")),
            )
            row = self._one(conn, "bambuddy_vp_id", vp_id)
            if row is not None:
                renamed = row.bambuddy_view is None or row.name != row.bambuddy_view.name
                new_name = row.name if renamed else name
                new_model = model or row.model
                if (
                    _same_view(row.bambuddy_view, view)
                    and row.vp_mode == mode
                    and row.forwards_to == forwards_to
                    and row.name == new_name
                    and row.model == new_model
                ):
                    return row, "already_linked", False
                return (
                    self._update(
                        conn,
                        row.id,
                        f"{fields}, name = %s, model = %s",
                        (*args, new_name, new_model),
                    ),
                    "already_linked",
                    True,
                )
            holder = None if serial is None else self._one(conn, "serial", serial)
            if holder is not None and adopts_for_virtual(holder, may_adopt=may_adopt):
                return (
                    self._update(
                        conn,
                        holder.id,
                        f"{fields}, kind = 'bambuddy_virtual', bambuddy_vp_id = %s,"
                        " model = COALESCE(model, %s)",
                        (*args, vp_id, model),
                    ),
                    "linked",
                    True,
                )
            if serial is None or holder is not None:
                serial = synthetic_vp_serial(vp_id)
                if self._one(conn, "serial", serial) is not None:
                    raise SerialTakenError(serial)
            data = conn.execute(
                "INSERT INTO printers (id, serial, name, model, preferred_path, kind,"
                " bambuddy_vp_id, vp_mode, forwards_to, bambuddy_view)"
                " VALUES (%s, %s, %s, %s, 'bambuddy', 'bambuddy_virtual', %s, %s, %s, %s)"
                f" RETURNING {_COLUMNS}",
                (str(uuid.uuid4()), serial, name, model, vp_id, *args),
            ).fetchone()
            assert data is not None
            return _row(data), "created" if holder is None else "conflict", True

    def delete(self, printer_id: str) -> None:
        key = printer_uuid(printer_id)
        if key is None:
            raise PrinterNotFoundError(printer_id)
        with self.pool.connection() as conn:
            gone = conn.execute("DELETE FROM printers WHERE id = %s", (key,))
            if gone.rowcount == 0:
                raise PrinterNotFoundError(printer_id)
            # `print_runs.printer` goes null with the row (ON DELETE SET NULL); its runs'
            # activity facets follow in the same transaction, so the Printer filter never
            # offers a printer that is gone (#4442 M3).
            _forget_run_activity(conn, str(key))
