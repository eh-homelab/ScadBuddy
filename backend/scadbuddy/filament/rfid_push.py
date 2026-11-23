"""The queue of tags whose binding changed and that Bambuddy has not been told of, and the
push that drains it (#6264)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.errors import TagAlreadyLinkedError
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import FilamentSync, pushes
from scadbuddy.filament.models import TagPushReport
from scadbuddy.library.settings_store import StoredSettings

if TYPE_CHECKING:
    # `filament.spools` imports this module for `enqueue_tag`.
    from scadbuddy.filament.component import Filament

Conn = Connection[DictRow]

logger = logging.getLogger(__name__)

#: The session advisory lock (hashed with ``hashtextextended``) that one drain of the
#: pending-tag queue holds, keyed by name as the backend's other advisory locks are.
PUSH_LOCK = "scadbuddy:filament:rfid_push"
#: Bambuddy's ``data_origin`` for a tag linked by hand (``LinkTagRequest``'s default,
#: ``data_origin_values`` in ``tests/bambuddy/recordings/openapi/rfid-routes.json``): what a
#: person's bind in ScadBuddy sets on the spool, and what the push sends for a spool with
#: no origin (#7149 R-7149-1).
HAND_LINK_ORIGIN = "nfc_link"
#: Bambuddy's ``data_origin`` when it matches a Bambu tag to a spool
#: (``spool_tag_matcher.py`` L417, ``data_origin_values``): what a person's bind of a
#: ``bambu_mifare`` tag sets (#7149 R-7149-10).
BAMBU_LINK_ORIGIN = "rfid_linked"


def link_origin(kind: str | None) -> str:
    """The ``data_origin`` a person's bind of a tag of ``kind`` gives the spool."""
    return BAMBU_LINK_ORIGIN if kind == "bambu_mifare" else HAND_LINK_ORIGIN


def enqueue_tag(conn: Conn, tag_id: UUID) -> None:
    """Queue ``tag_id`` for the push, on ``conn``'s transaction. A tag already queued is
    queued again from the start: its attempts and last error are cleared."""
    conn.execute(
        "INSERT INTO filament_bb_tag_pending (tag_id) VALUES (%s)"
        " ON CONFLICT (tag_id) DO UPDATE SET queued_at = now(), attempts = 0, last_error = NULL",
        (tag_id,),
    )


def error_label(exc: ApiError) -> str:
    """What a failed push records: the problem ``type``, plus ``:<status>`` when Bambuddy
    answered with one. Never the message, which may carry what Bambuddy sent."""
    code = exc.extensions.get("bambuddy_status")
    return exc.type if code is None else f"{exc.type}:{code}"


def failure_label(exc: Exception) -> str:
    """`error_label` for a Bambuddy problem, else one fixed word: an exception's class
    would leak what failed inside (a database driver's error, say)."""
    return error_label(exc) if isinstance(exc, ApiError) else "internal"


def _read(store: Filament, tag_id: UUID) -> tuple[dict[str, Any], int | None] | None:
    """The queued tag's keys and ``queued_at``, with ``origin`` the bound spool's
    ``data_origin`` (#7149 R-7149-1), and the Bambuddy id of the spool it is bound to
    (``None`` when unbound or the spool has no Bambuddy link); ``None`` when it is no
    longer queued."""
    with store.spools.transaction() as conn:
        tag = conn.execute(
            "SELECT t.kind, t.tag_uid, t.tray_uuid, p.queued_at"
            " FROM filament_bb_tag_pending p JOIN filament_tags t ON t.id = p.tag_id"
            " WHERE p.tag_id = %s",
            (tag_id,),
        ).fetchone()
        if tag is None:
            return None
        held = conn.execute(
            "SELECT l.bb_spool_id, s.data_origin FROM filament_tag_bindings b"
            " JOIN filament_bb_links l ON l.spool_id = b.spool_id"
            " JOIN filament_spools s ON s.id = b.spool_id"
            " WHERE b.tag_id = %s AND b.unbound_at IS NULL",
            (tag_id,),
        ).fetchone()
    origin = None if held is None else held["data_origin"]
    return dict(tag) | {"origin": origin}, None if held is None else held["bb_spool_id"]


def _sb_holder(store: Filament, bb_spool_id: int) -> bool | None:
    """Whether the SB spool linked to Bambuddy's ``bb_spool_id`` holds an open binding
    now; ``None`` when no SB spool is linked to it."""
    with store.spools.transaction() as conn:
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM filament_tag_bindings b"
            " WHERE b.spool_id = l.spool_id AND b.unbound_at IS NULL) AS bound"
            " FROM filament_bb_links l WHERE l.bb_spool_id = %s",
            (bb_spool_id,),
        ).fetchone()
    return None if row is None else bool(row["bound"])


def _done(store: Filament, tag_id: UUID, queued_at: datetime) -> None:
    """Drop the row, unless the tag was queued again since it was read."""
    with store.spools.transaction() as conn:
        conn.execute(
            "DELETE FROM filament_bb_tag_pending WHERE tag_id = %s AND queued_at = %s",
            (tag_id, queued_at),
        )


def _failed(store: Filament, tag_id: UUID, queued_at: datetime, label: str) -> None:
    """Count the failure, unless the tag was queued again since it was listed."""
    with store.spools.transaction() as conn:
        conn.execute(
            "UPDATE filament_bb_tag_pending SET attempts = attempts + 1,"
            " last_attempt_at = now(), last_error = %s WHERE tag_id = %s AND queued_at = %s",
            (label, tag_id, queued_at),
        )


async def _push_one(
    store: Filament, client: BambuddyClient, tag_id: UUID, report: TagPushReport
) -> None:
    read = await asyncio.to_thread(_read, store, tag_id)
    if read is None:
        return
    tag, desired = read
    uid, tray = tag["tag_uid"], tag["tray_uuid"]
    found = await client.spool_by_tag(tray_uuid=tray, tag_uid=uid)
    observed = None if found is None else found.id
    if desired == observed:
        report.unchanged += 1
    elif desired is not None:
        kind = "bambu" if tag["kind"] == "bambu_mifare" else "ntag"
        # The spool's own origin; a hand link's word when it has none (R-7149-10).
        origin = tag["origin"] or link_origin(tag["kind"])
        try:
            await client.link_tag(
                desired, tag_uid=uid, tray_uuid=tray, tag_type=kind, data_origin=origin
            )
            report.pushed += 1
        except TagAlreadyLinkedError as clash:
            await client.clear_spool_tag(clash.holder_id)
            await client.link_tag(
                desired, tag_uid=uid, tray_uuid=tray, tag_type=kind, data_origin=origin
            )
            report.moved += 1
    else:
        assert observed is not None
        bound = await asyncio.to_thread(_sb_holder, store, observed)
        if bound is None or bound:
            # An unknown holder is left alone (Decision 7). A holder that now has a tag of
            # its own was rebound across kinds: clearing both keys would wipe the new tag.
            report.unchanged += 1
        else:
            await client.clear_spool_tag(observed)
            report.cleared += 1
    await asyncio.to_thread(_done, store, tag_id, tag["queued_at"])


async def drain_tags(store: Filament, client: BambuddyClient, *, limit: int = 50) -> TagPushReport:
    """Tell Bambuddy of every queued tag, fewest failed attempts first (so a row that
    always fails, such as an archived target, cannot starve the rest past ``limit``), then
    oldest first (a closed and a new tag of one ``bind_on`` share ``queued_at``, so
    ``tag_id`` breaks the tie). A tag whose Bambuddy
    state already matches ScadBuddy's is only dequeued. A failure is recorded on the row
    (``attempts``, ``last_error``) and the next tag goes on."""
    report = TagPushReport()
    with store.spools.transaction() as conn:
        queued = [
            (row["tag_id"], row["queued_at"])
            for row in conn.execute(
                "SELECT tag_id, queued_at FROM filament_bb_tag_pending"
                " ORDER BY attempts, queued_at, tag_id LIMIT %s",
                (limit,),
            )
        ]
    for tag_id, queued_at in queued:
        try:
            await _push_one(store, client, tag_id, report)
        except Exception as error:
            label = failure_label(error)
            logger.warning(
                "could not push a tag to Bambuddy",
                extra={"tag_id": str(tag_id), "error": label},
            )
            await asyncio.to_thread(_failed, store, tag_id, queued_at, label)
            report.failed += 1
    return report


async def push_pending(
    store: Filament, load: Callable[[], StoredSettings], sync: Callable[[], FilamentSync]
) -> TagPushReport:
    """`drain_tags` when ``filament_sync.tags`` pushes (``sb_to_bb`` or ``two_way``) and a
    Bambuddy is configured; otherwise nothing, and the rows stay queued."""
    if not pushes((await asyncio.to_thread(sync)).tags):
        return TagPushReport()
    settings = await asyncio.to_thread(load)
    if not settings.bambuddy_url:
        return TagPushReport()
    # One drain at a time across the route and the Schedule (and replicas): a session
    # lock on a connection of its own, never the row pool's. A drain that finds it held
    # returns at once and touches nothing; the holder pushes the queue.
    lock = await acquire_lock(store.spools.conninfo, PUSH_LOCK)
    if lock is None:
        return TagPushReport()
    try:
        async with client_for(settings) as client:
            return await drain_tags(store, client)
    finally:
        await asyncio.to_thread(lock.close)  # closing the connection ends the lock


def try_lock(store: Filament, name: str = PUSH_LOCK) -> psycopg.Connection[Any] | None:
    """The session advisory lock ``name`` on a connection of its own (never the row
    pool's), or ``None`` when another holds it. Closing the connection ends the lock."""
    return try_lock_on(store.spools.conninfo, name)


def try_lock_on(conninfo: str, name: str) -> psycopg.Connection[Any] | None:
    """`try_lock` for a database address."""
    conn = psycopg.connect(conninfo, autocommit=True, connect_timeout=10)
    try:
        got = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (name,)
        ).fetchone()
    except BaseException:
        conn.close()
        raise
    if got is not None and got[0]:
        return conn
    conn.close()
    return None


def _close_late(task: asyncio.Future[psycopg.Connection[Any] | None]) -> None:
    if task.cancelled() or task.exception() is not None:
        return
    held = task.result()
    if held is not None:
        held.close()


async def acquire_lock(conninfo: str, name: str) -> psycopg.Connection[Any] | None:
    """`try_lock_on` off the event loop. A cancel while the thread runs (the inline push's
    timeout, a client that left) cannot drop the connection unclosed, which would keep the
    lock until garbage collection: the thread's result is closed when it lands."""
    task = asyncio.ensure_future(asyncio.to_thread(try_lock_on, conninfo, name))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_close_late)
        raise
