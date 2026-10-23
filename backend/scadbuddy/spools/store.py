"""``physical_spools``, ``spool_provider_records``, ``spool_record_links``,
``spool_provider_refreshes`` and ``tray_spool_links`` (plan 2026-10-18 D1, migration
``20261018T1744Z_physical_spools``).

``spool_provider_records`` is a cache: each provider's refresh
(:meth:`SpoolStore.replace_records`) rewrites its own rows. Which physical spool each
record is lives apart, in ``spool_record_links``, so a record a refresh leaves out (an
empty answer, an archived spool) keeps its link and is shown again when it comes back.
Every write takes one advisory lock, so two writers never race to make the same physical
spool. :meth:`SpoolStore.overlay`, which the print dialog reads, only reads: the mapping
from tray to spool is resolved at the time of the read.

How a record finds its spool: the record's ``tray_uuid``, else its ``tag_uid``, against
``physical_spools``. A key is the evidence, as Bambuddy treats it
(``get_spool_by_tag``, ``backend/app/services/spool_tag_matcher.py:405-436`` at
maziggy/bambuddy ``0d427510``, "Prefers tray_uuid match over tag_uid"): a record a person
linked by hand that later reports a key no spool has gives that key to its spool, and
one whose key names another spool moves there, its old spool keeping its tray link.
Without a key, a person's link stands. A spool is made for a key never seen before. A
key two of one provider's records carry links neither of them, since it cannot say
which is in the tray (Bambuddy's own recording,
``tests/bambuddy/recordings/inventory-spools.json``, carries one placeholder ``tag_uid``
on seven spools). Two records of one provider never share a spool
(``UNIQUE (physical_spool_id, provider)``): RFID claims first, then the links already
made, and a record whose spool another claimed is left unlinked.

How a tray finds its spool: its keys; else, untagged, a person's tray link whose
fingerprint still matches; else a fresh provider record whose own location is this
tray (`SpoolSnapshot.location`), which never overrides a person's link.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from opentelemetry.trace import Span
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.core.tracing import span
from scadbuddy.spools.models import (
    FoundBy,
    LinkedBy,
    Overlay,
    ProviderRefresh,
    SeenTray,
    ShelfSpool,
    SpoolSnapshot,
    TraySpool,
    clean_tag_uid,
    clean_tray_uuid,
)

#: An overlay older than this is left out with a note: three missed refreshes at the
#: default five-minute interval (plan 2026-10-18 D1).
SPOOL_OVERLAY_STALE = timedelta(minutes=30)

_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('scadbuddy:spools', 0))"

Conn = Connection[DictRow]
Keys = tuple[str | None, str | None]


class SpoolStore:
    def __init__(
        self, pool: ConnectionPool[Conn], *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._pool = pool
        #: What a write's deadline (`replace_records`' ``timeout``) is measured on.
        self._clock = clock

    async def _call[T](self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(fn, *args, **kwargs)

    # -- the refresh ----------------------------------------------------------------

    async def replace_records(
        self,
        provider: str,
        snapshots: Sequence[SpoolSnapshot],
        at: datetime,
        *,
        timeout: float | None = None,
    ) -> None:
        """``provider``'s whole inventory as of ``at``: its cached records replaced, the
        RFID links brought up to date, and its refresh recorded (clearing any failure).

        Within ``timeout`` seconds or not at all: each statement, the lock's wait
        included, is bounded by what is left, and a write that is past it at the end
        rolls back (`TimeoutError`). The refresh's caller cancels its wait at the same
        deadline and records the provider as failed, and a thread it left running must
        not then commit."""
        await self._call(self._replace_records, provider, list(snapshots), at, timeout)

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
        printer_id: UUID,
        trays: Sequence[SeenTray],
        *,
        now: datetime,
        providers: Sequence[str],
        located: Mapping[str, str] | None = None,
    ) -> Overlay:
        """Each tray's spool and the fresh records of it, and every one of ``providers``
        (in priority order) whose records are stale or missing. ``located`` names this
        printer in each provider's own terms (``{"bambuddy": "bambuddy:1"}``), for the
        providers' own assignments of untagged trays. Reads only."""
        return await self._call(
            self._overlay, printer_id, list(trays), now, list(providers), dict(located or {})
        )

    # -- a person's links ---------------------------------------------------------------

    async def link(
        self,
        printer_id: UUID,
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

    async def unlink(self, printer_id: UUID, ams_id: int, tray_id: int) -> None:
        """Forget which spool is in the tray; the spool and its records stay."""
        await self._call(self._unlink, printer_id, ams_id, tray_id)

    # -- sync ---------------------------------------------------------------------------

    def _replace_records(
        self,
        provider: str,
        snapshots: list[SpoolSnapshot],
        at: datetime,
        timeout: float | None,
    ) -> None:
        deadline = None if timeout is None else self._clock() + timeout
        listed: dict[str, SpoolSnapshot] = {}
        for snapshot in snapshots:
            listed.setdefault(snapshot.record_id, snapshot)
        with self._pool.connection() as conn, conn.transaction():
            if deadline is not None:
                bound = f"{max(1, int((deadline - self._clock()) * 1000))}ms"
                conn.execute("SELECT set_config('lock_timeout', %s, true)", (bound,))
                conn.execute("SELECT set_config('statement_timeout', %s, true)", (bound,))
            conn.execute(_LOCK)
            conn.execute("DELETE FROM spool_provider_records WHERE provider = %s", (provider,))
            for snapshot in listed.values():
                conn.execute(
                    "INSERT INTO spool_provider_records (provider, record_id, snapshot,"
                    " refreshed_at) VALUES (%s, %s, %s, %s)",
                    (provider, snapshot.record_id, Jsonb(snapshot.model_dump(mode="json")), at),
                )
            existing = {
                row["record_id"]: row
                for row in conn.execute(
                    "SELECT record_id, physical_spool_id, linked_by, linked_at"
                    " FROM spool_record_links WHERE provider = %s ORDER BY linked_at",
                    (provider,),
                )
            }
            ambiguous = _ambiguous(_keys_of(s.tray_uuid, s.tag_uid) for s in listed.values())
            claimed: dict[UUID, str] = {}
            handled: set[str] = set()
            final: dict[str, tuple[UUID, LinkedBy]] = {}
            for snapshot in listed.values():
                keys = _unambiguous(_keys_of(snapshot.tray_uuid, snapshot.tag_uid), ambiguous)
                if keys == (None, None):
                    continue
                handled.add(snapshot.record_id)
                current = existing.get(snapshot.record_id)
                spool = _spool_for_record(
                    conn, keys, snapshot, current["physical_spool_id"] if current else None
                )
                if spool not in claimed:
                    claimed[spool] = snapshot.record_id
                    final[snapshot.record_id] = (spool, "rfid")
            for record_id, row in existing.items():
                spool = row["physical_spool_id"]
                if record_id in handled or spool in claimed:
                    continue
                claimed[spool] = record_id
                final[record_id] = (spool, row["linked_by"])
            conn.execute("DELETE FROM spool_record_links WHERE provider = %s", (provider,))
            for record_id, (spool, linked_by) in final.items():
                kept = existing.get(record_id)
                since = (
                    kept["linked_at"]
                    if kept is not None
                    and kept["physical_spool_id"] == spool
                    and kept["linked_by"] == linked_by
                    else None
                )
                conn.execute(
                    "INSERT INTO spool_record_links"
                    " (provider, record_id, physical_spool_id, linked_by, linked_at)"
                    " VALUES (%s, %s, %s, %s, COALESCE(%s, now()))",
                    (provider, record_id, spool, linked_by, since),
                )
            conn.execute(
                "INSERT INTO spool_provider_refreshes (provider, refreshed_at) VALUES (%s, %s)"
                " ON CONFLICT (provider) DO UPDATE SET refreshed_at = EXCLUDED.refreshed_at,"
                " failed_at = NULL, failure = NULL",
                (provider, at),
            )
            if deadline is not None and self._clock() >= deadline:
                raise TimeoutError(f"the refresh of {provider} ran past its deadline")

    def _failed(self, provider: str, at: datetime, failure: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO spool_provider_refreshes (provider, failed_at, failure)"
                " VALUES (%s, %s, %s) ON CONFLICT (provider) DO UPDATE"
                " SET failed_at = EXCLUDED.failed_at, failure = EXCLUDED.failure",
                (provider, at, failure),
            )

    def _seen(self, trays: list[SeenTray]) -> None:
        ambiguous = _ambiguous(_keys_of(t.tray_uuid, t.tag_uid) for t in trays)
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(_LOCK)
            for tray in trays:
                keys = _unambiguous(_keys_of(tray.tray_uuid, tray.tag_uid), ambiguous)
                if keys != (None, None):
                    _spool_seen(conn, keys, tray)

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
        self,
        printer_id: UUID,
        trays: list[SeenTray],
        now: datetime,
        providers: list[str],
        located: dict[str, str],
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
            found = [_tray_spool(conn, printer_id, tray, fresh, located) for tray in trays]
            with span("spools.shelf") as current:
                shelf = _shelf(conn, printer_id, trays, fresh, current)
        return Overlay(trays=found, shelf=shelf, stale=stale)

    def _link(
        self,
        printer_id: UUID,
        tray: SeenTray,
        physical_spool_id: UUID | None,
        provider: str | None,
        record_id: str | None,
    ) -> TraySpool:
        keys = _keys_of(tray.tray_uuid, tray.tag_uid)
        tagged = keys != (None, None)
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(_LOCK)
            record_spool: UUID | None = None
            if provider is not None and record_id is not None:
                if (
                    conn.execute(
                        "SELECT 1 FROM spool_provider_records WHERE provider = %s"
                        " AND record_id = %s",
                        (provider, record_id),
                    ).fetchone()
                    is None
                ):
                    raise LookupError(f"{provider} has no spool record {record_id}")
                record_spool = _linked_spool(conn, provider, record_id)
            if (
                physical_spool_id is not None
                and conn.execute(
                    "SELECT 1 FROM physical_spools WHERE id = %s", (physical_spool_id,)
                ).fetchone()
                is None
            ):
                raise LookupError(f"no physical spool {physical_spool_id}")
            if tagged:
                spool = _spool_seen(conn, keys, tray)
                if physical_spool_id is not None and physical_spool_id != spool:
                    raise ValueError("the tray's RFID tag names another spool")
            elif physical_spool_id is not None:
                spool = physical_spool_id
            elif record_spool is not None:
                spool = record_spool
            else:
                current = _manual_tray_spool(conn, printer_id, tray)
                spool = current if current is not None else _new_spool(conn, tray)
            if provider is not None and record_id is not None:
                conn.execute(
                    "DELETE FROM spool_record_links WHERE provider = %s AND"
                    " (physical_spool_id = %s OR record_id = %s)",
                    (provider, spool, record_id),
                )
                conn.execute(
                    "INSERT INTO spool_record_links (provider, record_id, physical_spool_id,"
                    " linked_by) VALUES (%s, %s, %s, 'manual')",
                    (provider, record_id, spool),
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
            records = _records(conn, spool, None)
        return TraySpool(
            ams_id=tray.ams_id,
            tray_id=tray.tray_id,
            physical_spool_id=spool,
            linked_by="rfid" if tagged else "manual",
            records=records,
        )

    def _unlink(self, printer_id: UUID, ams_id: int, tray_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM tray_spool_links WHERE printer_id = %s AND ams_id = %s"
                " AND tray_id = %s",
                (printer_id, ams_id, tray_id),
            )


def _keys_of(tray_uuid: str | None, tag_uid: str | None) -> Keys:
    return clean_tray_uuid(tray_uuid), clean_tag_uid(tag_uid)


def _ambiguous(all_keys: Any) -> set[str]:
    """The keys more than one of ``all_keys`` carries."""
    seen: set[str] = set()
    twice: set[str] = set()
    for keys in all_keys:
        for key in {key for key in keys if key is not None}:
            (twice if key in seen else seen).add(key)
    return twice


def _unambiguous(keys: Keys, ambiguous: set[str]) -> Keys:
    tray_uuid, tag_uid = keys
    return (
        None if tray_uuid in ambiguous else tray_uuid,
        None if tag_uid in ambiguous else tag_uid,
    )


def _find(conn: Conn, keys: Keys) -> UUID | None:
    """The spool ``tray_uuid`` names, else the one ``tag_uid`` names, unless that spool
    has another ``tray_uuid``: then the tag is on a spool of its own."""
    tray_uuid, tag_uid = keys
    row = None
    if tray_uuid is not None:
        row = conn.execute(
            "SELECT id FROM physical_spools WHERE tray_uuid = %s", (tray_uuid,)
        ).fetchone()
    if row is None and tag_uid is not None:
        row = conn.execute(
            "SELECT id FROM physical_spools WHERE tag_uid = %s"
            " AND (%s::text IS NULL OR tray_uuid IS NULL)",
            (tag_uid, tray_uuid),
        ).fetchone()
    return None if row is None else row["id"]


def _tag_free(conn: Conn, tag_uid: str | None, spool: UUID | None) -> str | None:
    """``tag_uid`` unless a spool other than ``spool`` has it: only a tray's read moves a
    tag, never a provider's record."""
    if tag_uid is None:
        return None
    taken = conn.execute(
        "SELECT 1 FROM physical_spools WHERE tag_uid = %s AND id IS DISTINCT FROM %s",
        (tag_uid, spool),
    ).fetchone()
    return None if taken else tag_uid


def _spool_for_record(conn: Conn, keys: Keys, snapshot: SpoolSnapshot, linked: UUID | None) -> UUID:
    """The spool a record's keys name: an existing one (its missing keys filled in), else
    the untagged spool it is already linked to (given the keys), else a new one."""
    tray_uuid, tag_uid = keys
    spool = _find(conn, keys)
    if spool is None and linked is not None:
        untagged = conn.execute(
            "SELECT 1 FROM physical_spools WHERE id = %s AND tray_uuid IS NULL AND tag_uid IS NULL",
            (linked,),
        ).fetchone()
        spool = linked if untagged else None
    if spool is None:
        made = conn.execute(
            "INSERT INTO physical_spools (tray_uuid, tag_uid, material, colour)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (tray_uuid, _tag_free(conn, tag_uid, None), snapshot.material, snapshot.colour),
        ).fetchone()
        assert made is not None
        new: UUID = made["id"]
        return new
    # Found by its tag when it has a tray_uuid no spool has: free to fill in.
    conn.execute(
        "UPDATE physical_spools SET tray_uuid = COALESCE(tray_uuid, %s),"
        " tag_uid = COALESCE(tag_uid, %s), material = COALESCE(material, %s),"
        " colour = COALESCE(colour, %s) WHERE id = %s",
        (tray_uuid, _tag_free(conn, tag_uid, spool), snapshot.material, snapshot.colour, spool),
    )
    return spool


def _spool_seen(conn: Conn, keys: Keys, tray: SeenTray) -> UUID:
    """The spool a tray's keys name, made when none does, brought up to what the tray
    reports. The tag read moves to it: each read of a spool may be either of its tags."""
    tray_uuid, tag_uid = keys
    spool = _find(conn, keys)
    if spool is None:
        if tag_uid is not None:
            conn.execute("UPDATE physical_spools SET tag_uid = NULL WHERE tag_uid = %s", (tag_uid,))
        made = conn.execute(
            "INSERT INTO physical_spools"
            " (tray_uuid, tag_uid, material, colour, tray_info_idx, last_seen_at)"
            " VALUES (%s, %s, %s, %s, %s, now()) RETURNING id",
            (tray_uuid, tag_uid, tray.material, tray.colour, tray.tray_info_idx),
        ).fetchone()
        assert made is not None
        new: UUID = made["id"]
        return new
    if tag_uid is not None:
        conn.execute(
            "UPDATE physical_spools SET tag_uid = NULL WHERE tag_uid = %s AND id <> %s",
            (tag_uid, spool),
        )
    conn.execute(
        "UPDATE physical_spools SET tray_uuid = COALESCE(tray_uuid, %s),"
        " tag_uid = COALESCE(%s, tag_uid), material = COALESCE(%s, material),"
        " colour = COALESCE(%s, colour), tray_info_idx = COALESCE(%s, tray_info_idx),"
        " last_seen_at = now() WHERE id = %s",
        (tray_uuid, tag_uid, tray.material, tray.colour, tray.tray_info_idx, spool),
    )
    return spool


def _new_spool(conn: Conn, tray: SeenTray) -> UUID:
    made = conn.execute(
        "INSERT INTO physical_spools (material, colour, tray_info_idx) VALUES (%s, %s, %s)"
        " RETURNING id",
        (tray.material, tray.colour, tray.tray_info_idx),
    ).fetchone()
    assert made is not None
    spool: UUID = made["id"]
    return spool


def _linked_spool(conn: Conn, provider: str, record_id: str) -> UUID | None:
    row = conn.execute(
        "SELECT physical_spool_id FROM spool_record_links WHERE provider = %s AND record_id = %s",
        (provider, record_id),
    ).fetchone()
    return None if row is None else row["physical_spool_id"]


def _manual_tray_spool(conn: Conn, printer_id: UUID, tray: SeenTray) -> UUID | None:
    row = conn.execute(
        "SELECT physical_spool_id FROM tray_spool_links"
        " WHERE printer_id = %s AND ams_id = %s AND tray_id = %s AND fingerprint = %s",
        (printer_id, tray.ams_id, tray.tray_id, tray.fingerprint),
    ).fetchone()
    return None if row is None else row["physical_spool_id"]


def _located(
    conn: Conn, printer_id: UUID, tray: SeenTray, fresh: list[str], located: dict[str, str]
) -> tuple[str, SpoolSnapshot] | None:
    """The first fresh provider's record whose own location is this tray, with what the
    provider saw there still in it."""
    for provider in fresh:
        printer = located.get(provider)
        if printer is None:
            continue
        # A record whose spool better evidence places is not this tray's: a tagged spool
        # is wherever its keys are reported, and a person's link elsewhere is where the
        # person put it (re-review of #4180, N1).
        for row in conn.execute(
            "SELECT r.snapshot FROM spool_provider_records r"
            " LEFT JOIN spool_record_links l USING (provider, record_id)"
            " LEFT JOIN physical_spools p ON p.id = l.physical_spool_id"
            " WHERE r.provider = %s"
            " AND r.snapshot->'location'->>'printer' = %s"
            " AND (r.snapshot->'location'->>'ams_id')::int = %s"
            " AND (r.snapshot->'location'->>'tray_id')::int = %s"
            " AND (p.id IS NULL OR (p.tray_uuid IS NULL AND p.tag_uid IS NULL))"
            " AND NOT EXISTS (SELECT 1 FROM tray_spool_links t"
            "   WHERE t.physical_spool_id = l.physical_spool_id"
            "   AND NOT (t.printer_id = %s AND t.ams_id = %s AND t.tray_id = %s))"
            " ORDER BY r.record_id",
            (provider, printer, tray.ams_id, tray.tray_id, printer_id, tray.ams_id, tray.tray_id),
        ):
            snapshot = SpoolSnapshot.model_validate(row["snapshot"])
            assert snapshot.location is not None
            if snapshot.location.fingerprint in (None, tray.fingerprint):
                return provider, snapshot
    return None


def _tray_spool(
    conn: Conn, printer_id: UUID, tray: SeenTray, fresh: list[str], located: dict[str, str]
) -> TraySpool:
    keys = _keys_of(tray.tray_uuid, tray.tag_uid)
    spool: UUID | None
    found_by: FoundBy | None = None
    records: dict[str, SpoolSnapshot] = {}
    if keys != (None, None):
        # A tagged tray is its tag's spool or none: a link from before it was tagged is
        # not its spool.
        spool = _find(conn, keys)
        found_by = "rfid" if spool is not None else None
    else:
        spool = _manual_tray_spool(conn, printer_id, tray)
        found_by = "manual" if spool is not None else None
        if spool is None and (hit := _located(conn, printer_id, tray, fresh, located)) is not None:
            provider, snapshot = hit
            spool = _linked_spool(conn, provider, snapshot.record_id)
            found_by = "provider"
            records[provider] = snapshot
    if spool is not None:
        records = {**_records(conn, spool, fresh), **records}
    return TraySpool(
        ams_id=tray.ams_id,
        tray_id=tray.tray_id,
        physical_spool_id=spool,
        linked_by=found_by,
        records=records,
    )


def _shelf(
    conn: Conn, printer_id: UUID, trays: list[SeenTray], fresh: list[str], traced: Span
) -> list[ShelfSpool]:
    """``fresh``'s records no tray holds (`Overlay.shelf`), one entry per physical spool
    (or per record linked to none), in the providers' order, then by record id. A record
    with a key of its own is a tagged spool even when no spool is linked to it (two
    records naming one tag link neither): it is wherever its tag is read. A spool any
    of whose records a provider places in a tray is left out whole.

    Only an untagged tray is asked about, so with none nothing is read. Otherwise every
    fresh record is read, not only those of the trays' materials: which materials may
    share a tray is `bambuddy.filaments.family`'s rule, applied once by the question
    (`trays.close_spools`), and spelling it again in SQL would let the two drift."""
    if not fresh or all(_keys_of(tray.tray_uuid, tray.tag_uid) != (None, None) for tray in trays):
        traced.set_attribute("scadbuddy.spools.shelf_records", 0)
        traced.set_attribute("scadbuddy.spools.shelf_spools", 0)
        return []
    found: list[tuple[str, SpoolSnapshot, UUID | None]] = []
    read = 0
    for row in conn.execute(
        "SELECT r.provider, r.snapshot, l.physical_spool_id FROM spool_provider_records r"
        " LEFT JOIN spool_record_links l USING (provider, record_id)"
        " LEFT JOIN physical_spools p ON p.id = l.physical_spool_id"
        " WHERE r.provider = ANY(%s::text[]) AND r.snapshot->>'location' IS NULL"
        " AND p.tray_uuid IS NULL AND p.tag_uid IS NULL"
        # A spool one of its fresh records places in a tray is that tray's, whichever
        # record says so; a stale provider's placement counts no more than its records.
        " AND NOT EXISTS (SELECT 1 FROM spool_record_links ol"
        "   JOIN spool_provider_records orr USING (provider, record_id)"
        "   WHERE ol.physical_spool_id = l.physical_spool_id"
        "   AND ol.provider = ANY(%s::text[])"
        "   AND orr.snapshot->>'location' IS NOT NULL)",
        (fresh, fresh),
    ):
        read += 1
        snapshot = SpoolSnapshot.model_validate(row["snapshot"])
        if _keys_of(snapshot.tray_uuid, snapshot.tag_uid) == (None, None):
            found.append((row["provider"], snapshot, row["physical_spool_id"]))
    # A tagged tray is its tag's spool, never a person's link (`_tray_spool`).
    shown = {
        (tray.ams_id, tray.tray_id): tray.fingerprint
        for tray in trays
        if _keys_of(tray.tray_uuid, tray.tag_uid) == (None, None)
    }
    spools = list({spool for _, _, spool in found if spool is not None})
    held = {
        row["physical_spool_id"]
        for row in conn.execute(
            "SELECT physical_spool_id, printer_id, ams_id, tray_id, fingerprint"
            " FROM tray_spool_links WHERE physical_spool_id = ANY(%s::uuid[])",
            (spools,),
        )
        if row["printer_id"] != printer_id
        or shown.get((row["ams_id"], row["tray_id"])) == row["fingerprint"]
    }
    rank = {name: index for index, name in enumerate(fresh)}
    found.sort(key=lambda hit: (rank[hit[0]], hit[1].record_id))
    shelf: list[ShelfSpool] = []
    by_spool: dict[UUID, ShelfSpool] = {}
    for provider, snapshot, spool in found:
        if spool in held:
            continue
        entry = by_spool.get(spool) if spool is not None else None
        if entry is None:
            entry = ShelfSpool(physical_spool_id=spool)
            shelf.append(entry)
            if spool is not None:
                by_spool[spool] = entry
        entry.records[provider] = snapshot
    # Counts only, never a record's values (`core/tracing.py`).
    traced.set_attribute("scadbuddy.spools.shelf_records", read)
    traced.set_attribute("scadbuddy.spools.shelf_spools", len(shelf))
    return shelf


def _records(conn: Conn, spool: UUID, providers: list[str] | None) -> dict[str, SpoolSnapshot]:
    """The spool's listed records, of ``providers`` only when given, in their order."""
    found = {
        row["provider"]: SpoolSnapshot.model_validate(row["snapshot"])
        for row in conn.execute(
            "SELECT l.provider, r.snapshot FROM spool_record_links l"
            " JOIN spool_provider_records r USING (provider, record_id)"
            " WHERE l.physical_spool_id = %s AND (%s::text[] IS NULL OR l.provider = ANY(%s))",
            (spool, providers, providers),
        )
    }
    order = providers if providers is not None else sorted(found)
    return {name: found[name] for name in order if name in found}


def repoint_tray_links(conn: Conn, merged: UUID, into: UUID) -> None:
    """Move printer ``merged``'s tray links to printer ``into``, on ``conn`` and in its
    transaction: the printer registry's merge of a synthetic-serial Bambuddy row into
    the real one runs it beside the other references it repoints. Where both printers
    have a link for one tray, ``into``'s stands. It takes the store's lock, held to the
    end of the caller's transaction, as every writer here does."""
    conn.execute(_LOCK)
    conn.execute(
        "DELETE FROM tray_spool_links m USING tray_spool_links i WHERE m.printer_id = %s"
        " AND i.printer_id = %s AND i.ams_id = m.ams_id AND i.tray_id = m.tray_id",
        (merged, into),
    )
    conn.execute(
        "UPDATE tray_spool_links SET printer_id = %s WHERE printer_id = %s", (into, merged)
    )
