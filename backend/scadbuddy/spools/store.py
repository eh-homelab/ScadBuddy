"""``physical_spools``, ``spool_provider_records``, ``spool_provider_refreshes`` and
``tray_spool_links`` (plan 2026-10-18 D1, migration ``20261018T1744Z_physical_spools``).

A provider's refresh (:meth:`SpoolStore.replace_records`) rewrites that provider's rows
in one transaction; a person's manual link (:meth:`SpoolStore.link`) is the only other
writer of them. Every write takes one advisory lock, so two writers never race to make
the same physical spool. :meth:`SpoolStore.overlay`, which the print dialog reads, only
reads: the mapping from tray to spool is resolved at the time of the read, and a manual
link whose tray changed simply no longer matches.

How a record finds its spool, in order: a manual link a person made (kept across
refreshes); else the record's ``tray_uuid``, else its ``tag_uid``, against
``physical_spools`` (a spool is made for a key never seen before). A key two of one
provider's records carry links neither of them, since it cannot say which is in the
tray (Bambuddy's own recording, ``tests/bambuddy/recordings/inventory-spools.json``,
carries one placeholder ``tag_uid`` on seven spools). Two records of one provider never
share a spool (``UNIQUE (physical_spool_id, provider)``): the first claim wins, manual
links before RFID ones, and the rest stay unlinked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.spools.models import (
    LinkedBy,
    Overlay,
    ProviderRefresh,
    SeenTray,
    SpoolSnapshot,
    TraySpool,
    clean_key,
)

#: An overlay older than this is left out with a note: three missed refreshes at the
#: default five-minute interval (plan 2026-10-18 D1).
SPOOL_OVERLAY_STALE = timedelta(minutes=30)

_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('scadbuddy:spools', 0))"

Conn = Connection[DictRow]


class SpoolStore:
    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    async def _call[T](self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(fn, *args, **kwargs)

    # -- the refresh ----------------------------------------------------------------

    async def replace_records(
        self, provider: str, snapshots: Sequence[SpoolSnapshot], at: datetime
    ) -> None:
        """``provider``'s whole inventory as of ``at``: every record upserted and linked,
        those it no longer lists removed, and its refresh recorded."""
        await self._call(self._replace_records, provider, list(snapshots), at)

    async def failed(self, provider: str, at: datetime, failure: str) -> None:
        """A refresh of ``provider`` failed with ``failure`` (a type, never a message):
        its rows stay, and the overlay's note says when it last answered."""
        await self._call(self._failed, provider, at, failure)

    async def seen(self, trays: Sequence[SeenTray]) -> None:
        """What a printer's report says of each RFID tray's spool: the spool is made, or
        its material, colour and keys brought up to date."""
        await self._call(self._seen, list(trays))

    async def refreshes(self) -> list[ProviderRefresh]:
        return await self._call(self._refreshes)

    # -- the dialog's read ------------------------------------------------------------

    async def overlay(
        self,
        printer_id: str,
        trays: Sequence[SeenTray],
        *,
        now: datetime,
        providers: Iterable[str],
    ) -> Overlay:
        """Each tray's spool and the fresh records of it, and every one of ``providers``
        whose records are stale or missing. Reads only."""
        return await self._call(self._overlay, printer_id, list(trays), now, list(providers))

    # -- a person's links ---------------------------------------------------------------

    async def link(
        self,
        printer_id: str,
        tray: SeenTray,
        *,
        physical_spool_id: UUID | None = None,
        provider: str | None = None,
        record_id: str | None = None,
    ) -> TraySpool:
        """Say which spool is in ``tray``: a physical spool, or a provider's record. A
        record moves to the tray's spool (another record of its provider there is
        unlinked), and a spool moves to this tray from any other; nothing is duplicated.
        An RFID tray's spool is its keys', so no tray link is written for it.
        `LookupError` for a spool or record that does not exist, `ValueError` for a
        request that names neither, or a spool other than the one an RFID tray names."""
        if physical_spool_id is None and (provider is None or record_id is None):
            raise ValueError("name a physical spool, or a provider and its record")
        return await self._call(
            self._link, printer_id, tray, physical_spool_id, provider, record_id
        )

    async def unlink(self, printer_id: str, ams_id: int, tray_id: int) -> None:
        """Forget which spool is in the tray; the spool and its records stay."""
        await self._call(self._unlink, printer_id, ams_id, tray_id)

    # -- sync ---------------------------------------------------------------------------

    def _replace_records(self, provider: str, snapshots: list[SpoolSnapshot], at: datetime) -> None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(_LOCK)
            manual: dict[str, UUID] = {
                row["record_id"]: row["physical_spool_id"]
                for row in conn.execute(
                    "SELECT record_id, physical_spool_id FROM spool_provider_records"
                    " WHERE provider = %s AND linked_by = 'manual'",
                    (provider,),
                )
            }
            ambiguous = _ambiguous(snapshots)
            claimed: set[UUID] = set()
            links: dict[str, tuple[UUID, LinkedBy]] = {}
            for snapshot in snapshots:
                spool = manual.get(snapshot.record_id)
                if spool is not None and spool not in claimed:
                    claimed.add(spool)
                    links[snapshot.record_id] = (spool, "manual")
            for snapshot in snapshots:
                if snapshot.record_id in links:
                    continue
                tray_uuid, tag_uid = _keys(snapshot.tray_uuid, snapshot.tag_uid, ambiguous)
                if tray_uuid is None and tag_uid is None:
                    continue
                spool = _spool_for(
                    conn,
                    tray_uuid,
                    tag_uid,
                    material=snapshot.material,
                    colour=snapshot.colour,
                    tray_info_idx=None,
                    seen_at=None,
                )
                if spool not in claimed:
                    claimed.add(spool)
                    links[snapshot.record_id] = (spool, "rfid")
            conn.execute("DELETE FROM spool_provider_records WHERE provider = %s", (provider,))
            for snapshot in snapshots:
                spool, linked_by = links.get(snapshot.record_id, (None, None))
                conn.execute(
                    "INSERT INTO spool_provider_records"
                    " (provider, record_id, physical_spool_id, linked_by, snapshot, refreshed_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (provider, record_id) DO NOTHING",
                    (
                        provider,
                        snapshot.record_id,
                        spool,
                        linked_by,
                        Jsonb(snapshot.model_dump(mode="json")),
                        at,
                    ),
                )
            conn.execute(
                "INSERT INTO spool_provider_refreshes (provider, refreshed_at) VALUES (%s, %s)"
                " ON CONFLICT (provider) DO UPDATE SET refreshed_at = EXCLUDED.refreshed_at",
                (provider, at),
            )

    def _failed(self, provider: str, at: datetime, failure: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO spool_provider_refreshes (provider, failed_at, failure)"
                " VALUES (%s, %s, %s) ON CONFLICT (provider) DO UPDATE"
                " SET failed_at = EXCLUDED.failed_at, failure = EXCLUDED.failure",
                (provider, at, failure),
            )

    def _seen(self, trays: list[SeenTray]) -> None:
        ambiguous = _ambiguous(trays)
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(_LOCK)
            now = conn.execute("SELECT now() AS now").fetchone()
            assert now is not None
            for tray in trays:
                tray_uuid, tag_uid = _keys(tray.tray_uuid, tray.tag_uid, ambiguous)
                if tray_uuid is None and tag_uid is None:
                    continue
                _spool_for(
                    conn,
                    tray_uuid,
                    tag_uid,
                    material=tray.material,
                    colour=tray.colour,
                    tray_info_idx=tray.tray_info_idx,
                    seen_at=now["now"],
                )

    def _refreshes(self) -> list[ProviderRefresh]:
        with self._pool.connection() as conn:
            return [
                ProviderRefresh.model_validate(row)
                for row in conn.execute(
                    "SELECT provider, refreshed_at, failed_at, failure"
                    " FROM spool_provider_refreshes ORDER BY provider"
                )
            ]

    def _overlay(
        self, printer_id: str, trays: list[SeenTray], now: datetime, providers: list[str]
    ) -> Overlay:
        with self._pool.connection() as conn:
            refreshed = {
                row["provider"]: row["refreshed_at"]
                for row in conn.execute(
                    "SELECT provider, refreshed_at FROM spool_provider_refreshes"
                    " WHERE provider = ANY(%s)",
                    (providers,),
                )
            }
            stale = {
                name: refreshed.get(name)
                for name in providers
                if refreshed.get(name) is None or now - refreshed[name] > SPOOL_OVERLAY_STALE
            }
            fresh = [name for name in providers if name not in stale]
            found = [_resolve(conn, printer_id, tray) for tray in trays]
            spools = [spool for spool, _ in found if spool is not None]
            records = _records(conn, spools, fresh)
        return Overlay(
            trays=[
                TraySpool(
                    ams_id=tray.ams_id,
                    tray_id=tray.tray_id,
                    physical_spool_id=spool,
                    linked_by=linked_by,
                    records=records.get(spool, {}) if spool is not None else {},
                )
                for tray, (spool, linked_by) in zip(trays, found, strict=True)
            ],
            stale=stale,
        )

    def _link(
        self,
        printer_id: str,
        tray: SeenTray,
        physical_spool_id: UUID | None,
        provider: str | None,
        record_id: str | None,
    ) -> TraySpool:
        tray_uuid, tag_uid = clean_key(tray.tray_uuid), clean_key(tray.tag_uid)
        tagged = tray_uuid is not None or tag_uid is not None
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(_LOCK)
            record: dict[str, Any] | None = None
            if provider is not None and record_id is not None:
                record = conn.execute(
                    "SELECT physical_spool_id FROM spool_provider_records"
                    " WHERE provider = %s AND record_id = %s FOR UPDATE",
                    (provider, record_id),
                ).fetchone()
                if record is None:
                    raise LookupError(f"{provider} has no spool record {record_id}")
            if physical_spool_id is not None and not _exists(conn, physical_spool_id):
                raise LookupError(f"no physical spool {physical_spool_id}")
            if tagged:
                spool = _spool_for(
                    conn,
                    tray_uuid,
                    tag_uid,
                    material=tray.material,
                    colour=tray.colour,
                    tray_info_idx=tray.tray_info_idx,
                    seen_at=None,
                )
                if physical_spool_id is not None and physical_spool_id != spool:
                    raise ValueError("the tray's RFID tag names another spool")
            elif physical_spool_id is not None:
                spool = physical_spool_id
            elif record is not None and record["physical_spool_id"] is not None:
                spool = record["physical_spool_id"]
            else:
                current, _ = _resolve(conn, printer_id, tray)
                spool = current if current is not None else _new_spool(conn, tray)
            if provider is not None and record_id is not None:
                conn.execute(
                    "UPDATE spool_provider_records SET physical_spool_id = NULL, linked_by = NULL"
                    " WHERE provider = %s AND physical_spool_id = %s AND record_id <> %s",
                    (provider, spool, record_id),
                )
                conn.execute(
                    "UPDATE spool_provider_records"
                    " SET physical_spool_id = %s, linked_by = 'manual'"
                    " WHERE provider = %s AND record_id = %s",
                    (spool, provider, record_id),
                )
            if not tagged:
                conn.execute(
                    "DELETE FROM tray_spool_links WHERE physical_spool_id = %s"
                    " AND NOT (printer_id = %s AND ams_id = %s AND tray_id = %s)",
                    (spool, printer_id, tray.ams_id, tray.tray_id),
                )
                conn.execute(
                    "INSERT INTO tray_spool_links"
                    " (printer_id, ams_id, tray_id, physical_spool_id, fingerprint)"
                    " VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (printer_id, ams_id, tray_id) DO UPDATE"
                    " SET physical_spool_id = EXCLUDED.physical_spool_id,"
                    " fingerprint = EXCLUDED.fingerprint, linked_at = now()",
                    (printer_id, tray.ams_id, tray.tray_id, spool, tray.fingerprint),
                )
            records = _records(conn, [spool], None)
        return TraySpool(
            ams_id=tray.ams_id,
            tray_id=tray.tray_id,
            physical_spool_id=spool,
            linked_by="rfid" if tagged else "manual",
            records=records.get(spool, {}),
        )

    def _unlink(self, printer_id: str, ams_id: int, tray_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM tray_spool_links WHERE printer_id = %s AND ams_id = %s"
                " AND tray_id = %s",
                (printer_id, ams_id, tray_id),
            )


def _ambiguous(items: Sequence[SpoolSnapshot | SeenTray]) -> set[str]:
    """The keys more than one of ``items`` carries."""
    seen: set[str] = set()
    twice: set[str] = set()
    for item in items:
        for key in {clean_key(item.tray_uuid), clean_key(item.tag_uid)} - {None}:
            assert key is not None
            (twice if key in seen else seen).add(key)
    return twice


def _keys(
    tray_uuid: str | None, tag_uid: str | None, ambiguous: set[str]
) -> tuple[str | None, str | None]:
    uuid_key, tag_key = clean_key(tray_uuid), clean_key(tag_uid)
    return (
        None if uuid_key in ambiguous else uuid_key,
        None if tag_key in ambiguous else tag_key,
    )


def _exists(conn: Conn, spool: UUID) -> bool:
    return (
        conn.execute("SELECT 1 FROM physical_spools WHERE id = %s", (spool,)).fetchone() is not None
    )


def _spool_for(
    conn: Conn,
    tray_uuid: str | None,
    tag_uid: str | None,
    *,
    material: str | None,
    colour: str | None,
    tray_info_idx: str | None,
    seen_at: datetime | None,
) -> UUID:
    """The physical spool ``tray_uuid`` (else ``tag_uid``) names, made when none does.
    A missing key is filled in; what a tray reports (``seen_at`` set) also updates its
    material and colour, and its tag (each read of a spool may be either of its two
    tags)."""
    row = None
    if tray_uuid is not None:
        row = conn.execute(
            "SELECT id FROM physical_spools WHERE tray_uuid = %s", (tray_uuid,)
        ).fetchone()
    if row is None and tag_uid is not None:
        row = conn.execute(
            "SELECT id FROM physical_spools WHERE tag_uid = %s", (tag_uid,)
        ).fetchone()
    if row is None:
        made = conn.execute(
            "INSERT INTO physical_spools"
            " (tray_uuid, tag_uid, material, colour, tray_info_idx, last_seen_at)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (tray_uuid, tag_uid, material, colour, tray_info_idx, seen_at),
        ).fetchone()
        assert made is not None
        spool: UUID = made["id"]
        return spool
    spool = row["id"]
    if tag_uid is not None and seen_at is not None:
        # The tag moves to the spool it was read on.
        conn.execute(
            "UPDATE physical_spools SET tag_uid = NULL WHERE tag_uid = %s AND id <> %s",
            (tag_uid, spool),
        )
    conn.execute(
        "UPDATE physical_spools SET"
        " tray_uuid = COALESCE(tray_uuid, %(tray_uuid)s),"
        " tag_uid = CASE WHEN %(seen)s THEN COALESCE(%(tag_uid)s, tag_uid)"
        "   ELSE COALESCE(tag_uid, %(tag_uid)s) END,"
        " material = CASE WHEN %(seen)s THEN COALESCE(%(material)s, material)"
        "   ELSE COALESCE(material, %(material)s) END,"
        " colour = CASE WHEN %(seen)s THEN COALESCE(%(colour)s, colour)"
        "   ELSE COALESCE(colour, %(colour)s) END,"
        " tray_info_idx = COALESCE(%(tray_info_idx)s, tray_info_idx),"
        " last_seen_at = COALESCE(%(seen_at)s, last_seen_at)"
        " WHERE id = %(id)s",
        {
            "id": spool,
            # Found by its tag when it has a tray_uuid no spool has: free to fill in.
            "tray_uuid": tray_uuid,
            "tag_uid": tag_uid
            if tag_uid is not None and not _taken(conn, tag_uid, spool)
            else None,
            "material": material,
            "colour": colour,
            "tray_info_idx": tray_info_idx,
            "seen": seen_at is not None,
            "seen_at": seen_at,
        },
    )
    return spool


def _taken(conn: Conn, tag_uid: str, spool: UUID) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM physical_spools WHERE tag_uid = %s AND id <> %s", (tag_uid, spool)
        ).fetchone()
        is not None
    )


def _new_spool(conn: Conn, tray: SeenTray) -> UUID:
    made = conn.execute(
        "INSERT INTO physical_spools (material, colour, tray_info_idx) VALUES (%s, %s, %s)"
        " RETURNING id",
        (tray.material, tray.colour, tray.tray_info_idx),
    ).fetchone()
    assert made is not None
    spool: UUID = made["id"]
    return spool


def _resolve(conn: Conn, printer_id: str, tray: SeenTray) -> tuple[UUID | None, LinkedBy | None]:
    """The spool in ``tray``: by its keys, else by a manual link whose fingerprint still
    matches. Reads only."""
    tray_uuid, tag_uid = clean_key(tray.tray_uuid), clean_key(tray.tag_uid)
    for column, key in (("tray_uuid", tray_uuid), ("tag_uid", tag_uid)):
        if key is None:
            continue
        query = f"SELECT id FROM physical_spools WHERE {column} = %s"
        row = conn.execute(query.encode(), (key,)).fetchone()
        if row is not None:
            return row["id"], "rfid"
    if tray_uuid is not None or tag_uid is not None:
        # A tagged tray is its tag's spool or none: a link from before it was tagged is
        # not its spool.
        return None, None
    link = conn.execute(
        "SELECT physical_spool_id FROM tray_spool_links"
        " WHERE printer_id = %s AND ams_id = %s AND tray_id = %s AND fingerprint = %s",
        (printer_id, tray.ams_id, tray.tray_id, tray.fingerprint),
    ).fetchone()
    if link is None:
        return None, None
    return link["physical_spool_id"], "manual"


def _records(
    conn: Conn, spools: list[UUID], providers: list[str] | None
) -> dict[UUID, dict[str, SpoolSnapshot]]:
    """Each spool's records, of ``providers`` only when given."""
    if not spools:
        return {}
    found: dict[UUID, dict[str, SpoolSnapshot]] = {}
    for row in conn.execute(
        "SELECT physical_spool_id, provider, snapshot FROM spool_provider_records"
        " WHERE physical_spool_id = ANY(%s) AND (%s::text[] IS NULL OR provider = ANY(%s))",
        (spools, providers, providers),
    ):
        found.setdefault(row["physical_spool_id"], {})[row["provider"]] = (
            SpoolSnapshot.model_validate(row["snapshot"])
        )
    return found
