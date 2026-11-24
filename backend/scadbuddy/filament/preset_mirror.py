"""The Bambuddy mirror of ScadBuddy's own filament presets (#7176, plan
2026-11-23-scadbuddy-filament-presets.md Decision 11).

ScadBuddy is the record; Bambuddy gets a copy only while the stored setting
``filament_preset_mirror`` is on (the default: Bambuddy's AMS slot settings and spool
records name presets it holds, the plan's "Measurement"). A preset ScadBuddy owns
(``3dfp``, ``manual``) whose document Bambuddy has not taken (``mirrored_sha256`` is not
its ``sha256``) is sent: ``PUT /local-presets/{id}`` when it has a Bambuddy id, which
rewrites Bambuddy's preset in place and keeps the id every spool and slot names (#7174),
else ``POST /local-presets/``, whose id is kept. A ``PUT`` that finds the preset gone
(404) creates it again. A ``bambuddy`` preset is never sent back: it is Bambuddy's.

A failure is the preset's ``mirror_error`` and never fails the write that asked for the
mirror; the Schedule's next pull (`mirror_after_pull`) sends it again. An edit
made in Bambuddy to a mirrored preset is not read back.

One mirror runs at a time (the session try-lock `LOCK`): another caller meanwhile sends
nothing, and the next pull sends what it left. A preset's Bambuddy id is kept with
the Bambuddy it belongs to (``bb_instance``), and it is a Bambuddy link as the filament
sync's are (`bambuddy_pull.ANY_LINK`): the write that stores it records its Bambuddy as
the links' one when none is recorded (`sb_presets.record_instance_on`), so the links'
instance is recorded even when presets are the only links. After the Bambuddy URL changes
the mirror waits, as the filament sync does, until a person resets the links (#6773): a
reset that finds the same Bambuddy (proved by the spool links, so never when presets are
the only links) moves the ids to the new URL's instance (`bb_sync_results.reset_links`),
and one that deletes the links drops them, so each preset is created on the new
Bambuddy. A preset whose id is still another Bambuddy's (a mirror that raced a reset
stored it) is created on the configured one, never rewritten there by id. Bambuddy's
``PUT`` merges the document over the OrcaSlicer base its ``inherits`` names, while its
``POST`` keeps it as sent (`BambuddyClient.update_local_preset`), so a rewritten copy can
carry inherited keys the first one lacked. Before a ``POST`` the mirror keeps the highest
id Bambuddy lists (``mirror_create_floor``), so when the answer is lost the next send
takes the one preset above it that has the same name and holds the document sent
(`_Listing.made_after`) instead of making a second one. One preset's failure, a Bambuddy
id another ScadBuddy preset holds included, is its own ``mirror_error`` and never stops
the presets after it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection
from typing import Any

from fastapi import status
from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.models import LocalPreset
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.filament.rfid_push import acquire_lock
from scadbuddy.filament.sb_presets import (
    Conn,
    FilamentPresetStore,
    record_instance_on,
)
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.store import BlobStore

logger = logging.getLogger(__name__)

#: The longest mirror error kept on a preset.
MAX_ERROR = 500
#: The session try-lock one mirror run holds.
LOCK = "scadbuddy:filament:preset_mirror"


def wants_mirror(settings: StoredSettings) -> bool:
    """Whether the mirror runs: the setting is on and a Bambuddy is configured."""
    return bool(settings.filament_preset_mirror and settings.bambuddy_url)


def instance_of(settings: StoredSettings) -> str | None:
    """The configured Bambuddy's instance id (`bb_sync_nudge.instance_for`); ``None``
    with no Bambuddy URL."""
    return instance_for(settings.bambuddy_url) if settings.bambuddy_url else None


def _links_elsewhere(pool: ConnectionPool[Conn], instance: str) -> bool:
    """Whether the links' recorded Bambuddy (the filament sync's and the presets' ids,
    `record_instance_on`) is another than ``instance``: the URL changed, and until a
    person resets the links (#6773) it is not known whether the new URL reaches the same
    Bambuddy, so the mirror waits, as the sync does."""
    with pool.connection() as conn:
        row = conn.execute("SELECT instance FROM filament_bb_sync_state").fetchone()
    return row is not None and row["instance"] not in (None, instance)


def _pending(
    pool: ConnectionPool[Conn], ids: Collection[int] | None, instance: str
) -> list[dict[str, Any]]:
    """The presets to send: a document Bambuddy has not taken, or an id of another
    Bambuddy than ``instance``."""
    with pool.connection() as conn:
        return conn.execute(
            "SELECT id, name, blob_key, sha256, bb_preset_id, bb_instance, mirror_create_floor"
            " FROM filament_presets"
            " WHERE source IN ('3dfp', 'manual')"
            " AND (mirrored_sha256 IS DISTINCT FROM sha256"
            " OR (bb_preset_id IS NOT NULL AND bb_instance IS DISTINCT FROM %(instance)s"
            " AND bb_instance IS NOT NULL))"
            " AND (%(ids)s::bigint[] IS NULL OR id = ANY(%(ids)s))"
            " ORDER BY id",
            {"ids": None if ids is None else sorted(set(ids)), "instance": instance},
        ).fetchall()


def _mirrored(
    pool: ConnectionPool[Conn], preset_id: int, sha256: str, bb_id: int, instance: str
) -> None:
    """Bambuddy ``instance`` holds document ``sha256`` as its preset ``bb_id``. The id is
    kept whatever the document; the document counts as sent only while the preset still
    holds it: a version written meanwhile stays pending. With the id, ``instance`` is
    recorded as the links' Bambuddy when none is (`record_instance_on`)."""
    with pool.connection() as conn, conn.transaction():
        conn.execute(
            "UPDATE filament_presets SET bb_preset_id = %s, bb_instance = %s,"
            " mirror_create_floor = NULL,"
            " mirrored_sha256 = CASE WHEN sha256 = %s THEN %s ELSE mirrored_sha256 END,"
            " mirror_error = NULL, mirror_error_at = NULL"
            " WHERE id = %s",
            (bb_id, instance, sha256, sha256, preset_id),
        )
        record_instance_on(conn, instance)


def _create_floor(pool: ConnectionPool[Conn], preset_id: int, floor: int) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_presets SET mirror_create_floor = %s WHERE id = %s",
            (floor, preset_id),
        )


def _failed(pool: ConnectionPool[Conn], preset_id: int, error: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_presets SET mirror_error = %s, mirror_error_at = now() WHERE id = %s",
            (error[:MAX_ERROR], preset_id),
        )


class _Listing:
    """Bambuddy's filament local presets, read at most once per mirror run."""

    def __init__(self, client: BambuddyClient) -> None:
        self._client = client
        self._presets: list[LocalPreset] | None = None

    async def _all(self) -> list[LocalPreset]:
        if self._presets is None:
            self._presets = (await self._client.local_presets()).filament
        return self._presets

    async def made_after(self, name: str, doc: dict[str, Any], floor: int) -> int | None:
        """The one preset named ``name`` with an id above ``floor`` whose document is
        ``doc``, else ``None``. Bambuddy's ``POST`` stores the document as sent
        (`BambuddyClient.create_local_preset`), so a lost create of ``doc`` reads back as
        ``doc``; another type's preset of the same name holds another document. A
        version written since the lost create matches nothing, and the preset is created
        again."""
        found = []
        for preset in await self._all():
            if preset.name == name and preset.id > floor:
                detail = await self._client.local_preset(preset.id)
                if detail.setting == doc:
                    found.append(preset.id)
        return found[0] if len(found) == 1 else None

    async def floor(self) -> int:
        """The highest id listed or made in this run, 0 for none."""
        return max((p.id for p in await self._all()), default=0)

    async def made(self, preset: LocalPreset) -> None:
        """``preset``, made in this run, counts for the next floor."""
        (await self._all()).append(preset)


async def _send(
    client: BambuddyClient,
    pool: ConnectionPool[Conn],
    listing: _Listing,
    row: dict[str, Any],
    doc: dict[str, Any],
    instance: str,
) -> int:
    """The Bambuddy id that holds ``doc`` once sent. An id is rewritten in place only on
    the Bambuddy it belongs to."""
    bb_id = row["bb_preset_id"] if row["bb_instance"] in (None, instance) else None
    if bb_id is None and row["mirror_create_floor"] is not None:
        # A create whose answer was lost: the preset it made, when Bambuddy lists one.
        bb_id = await listing.made_after(row["name"], doc, row["mirror_create_floor"])
    if bb_id is not None:
        try:
            await client.update_local_preset(bb_id, row["name"], doc)
            return int(bb_id)
        except ApiError as error:
            if error.status != status.HTTP_404_NOT_FOUND:
                raise
    await asyncio.to_thread(_create_floor, pool, row["id"], await listing.floor())
    made = await client.create_local_preset(row["name"], "filament", doc)
    await listing.made(made)
    return made.id


async def mirror_with(
    client: BambuddyClient,
    pool: ConnectionPool[Conn],
    store: FilamentPresetStore,
    instance: str,
    ids: Collection[int] | None = None,
) -> int:
    """Send each pending preset of ``ids`` (every one for ``None``) through ``client``,
    to Bambuddy ``instance``; how many Bambuddy took. Each failure is kept on its preset
    and logged by type. 0 when another mirror holds `LOCK`."""
    conninfo = pool.conninfo
    lock = await acquire_lock(conninfo() if callable(conninfo) else str(conninfo), LOCK)
    if lock is None:
        logger.info("another filament preset mirror is running; the next pull sends")
        return 0
    try:
        return await _mirror_locked(client, pool, store, instance, ids)
    finally:
        await asyncio.to_thread(lock.close)  # closing the connection ends the lock


async def _mirror_locked(
    client: BambuddyClient,
    pool: ConnectionPool[Conn],
    store: FilamentPresetStore,
    instance: str,
    ids: Collection[int] | None,
) -> int:
    if await asyncio.to_thread(_links_elsewhere, pool, instance):
        logger.warning(
            "the filament preset mirror waits: the Bambuddy URL changed and its links"
            " are not reset yet"
        )
        return 0
    sent = 0
    listing = _Listing(client)
    for row in await asyncio.to_thread(_pending, pool, ids, instance):
        doc = await store.read(row["blob_key"])
        if doc is None:
            await asyncio.to_thread(_failed, pool, row["id"], "the preset's document is missing")
            continue
        try:
            bb_id = await _send(client, pool, listing, row, doc, instance)
            await asyncio.to_thread(_mirrored, pool, row["id"], row["sha256"], bb_id, instance)
        except UniqueViolation:
            # Another ScadBuddy preset holds that Bambuddy id: this one's error, and the
            # presets after it are still sent.
            await asyncio.to_thread(
                _failed, pool, row["id"], f"another preset holds Bambuddy preset {bb_id}"
            )
            logger.warning(
                "a mirrored filament preset's Bambuddy id is another preset's",
                extra={"preset_id": row["id"], "bb_preset_id": bb_id},
            )
            continue
        except ApiError as error:
            await asyncio.to_thread(_failed, pool, row["id"], f"Bambuddy: {error.detail}")
            logger.warning(
                "could not mirror a filament preset to Bambuddy",
                extra={"preset_id": row["id"], "status": error.status},
            )
            continue
        except Exception as error:
            # Not an answer of Bambuddy's (a transport error the client does not map): kept
            # on this preset, and the rest are still sent.
            await asyncio.to_thread(_failed, pool, row["id"], type(error).__name__)
            logger.warning(
                "could not mirror a filament preset to Bambuddy",
                extra={"preset_id": row["id"], "error": type(error).__name__},
            )
            continue
        sent += 1
    return sent


async def mirror_pending(
    pool: ConnectionPool[Conn],
    store: FilamentPresetStore,
    settings: StoredSettings,
    ids: Collection[int] | None = None,
) -> int:
    """`mirror_with` over a client of its own, while the mirror runs (`wants_mirror`);
    0 otherwise. Never raises: a failure is logged, and the next pull sends again."""
    instance = instance_of(settings)
    if not wants_mirror(settings) or instance is None:
        return 0
    try:
        async with client_for(settings) as client:
            return await mirror_with(client, pool, store, instance, ids)
    except Exception as error:
        logger.warning(
            "the filament preset mirror did not run; the next pull retries",
            extra={"error": type(error).__name__},
        )
        return 0


async def mirror_after_pull(
    pool: ConnectionPool[Conn], blobs: BlobStore, settings: Callable[[], StoredSettings]
) -> int:
    """What the Schedule's pull runs last (`spools/activities.py`
    `filament_pull_activity`), whatever ``filament_sync`` says: every pending preset sent
    (`mirror_pending`), so one that failed before is sent again. It reads nothing of
    Bambuddy's presets into ScadBuddy (#7244)."""
    loaded = await asyncio.to_thread(settings)
    return await mirror_pending(pool, FilamentPresetStore(pool, lambda: blobs), loaded)
