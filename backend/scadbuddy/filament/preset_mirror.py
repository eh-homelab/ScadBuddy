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
mirror; the next refresh of the kept presets (`filament/sb_presets.py` `sync_from_held`)
sends it again. An edit made in Bambuddy to a mirrored preset is not read back.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection
from typing import Any

from fastapi import status
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.sb_presets import Conn, FilamentPresetStore, sync_from_held
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.slicing.held_presets import HeldPresetStore
from scadbuddy.store import BlobStore

logger = logging.getLogger(__name__)

#: The longest mirror error kept on a preset.
MAX_ERROR = 500


def wants_mirror(settings: StoredSettings) -> bool:
    """Whether the mirror runs: the setting is on and a Bambuddy is configured."""
    return bool(settings.filament_preset_mirror and settings.bambuddy_url)


def _pending(pool: ConnectionPool[Conn], ids: Collection[int] | None) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT id, name, blob_key, sha256, bb_preset_id FROM filament_presets"
            " WHERE source IN ('3dfp', 'manual')"
            " AND mirrored_sha256 IS DISTINCT FROM sha256"
            " AND (%(ids)s::bigint[] IS NULL OR id = ANY(%(ids)s))"
            " ORDER BY id",
            {"ids": None if ids is None else sorted(set(ids))},
        ).fetchall()


def _mirrored(pool: ConnectionPool[Conn], preset_id: int, sha256: str, bb_id: int) -> None:
    """Bambuddy holds document ``sha256`` as its preset ``bb_id``. Only while the preset
    still holds that document: a version written meanwhile stays pending."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_presets SET bb_preset_id = %s, mirrored_sha256 = %s,"
            " mirror_error = NULL, mirror_error_at = NULL"
            " WHERE id = %s AND sha256 = %s",
            (bb_id, sha256, preset_id, sha256),
        )


def _failed(pool: ConnectionPool[Conn], preset_id: int, error: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_presets SET mirror_error = %s, mirror_error_at = now() WHERE id = %s",
            (error[:MAX_ERROR], preset_id),
        )


async def _send(client: BambuddyClient, row: dict[str, Any], doc: dict[str, Any]) -> int:
    """The Bambuddy id that holds ``doc`` once sent."""
    if row["bb_preset_id"] is not None:
        try:
            await client.update_local_preset(row["bb_preset_id"], row["name"], doc)
            return int(row["bb_preset_id"])
        except ApiError as error:
            if error.status != status.HTTP_404_NOT_FOUND:
                raise
    made = await client.create_local_preset(row["name"], "filament", doc)
    return made.id


async def mirror_with(
    client: BambuddyClient,
    pool: ConnectionPool[Conn],
    store: FilamentPresetStore,
    ids: Collection[int] | None = None,
) -> int:
    """Send each pending preset of ``ids`` (every one for ``None``) through ``client``;
    how many Bambuddy took. Each failure is kept on its preset and logged by type."""
    sent = 0
    for row in await asyncio.to_thread(_pending, pool, ids):
        doc = await store.read(row["blob_key"])
        if doc is None:
            await asyncio.to_thread(_failed, pool, row["id"], "the preset's document is missing")
            continue
        try:
            bb_id = await _send(client, row, doc)
        except ApiError as error:
            await asyncio.to_thread(_failed, pool, row["id"], f"Bambuddy: {error.detail}")
            logger.warning(
                "could not mirror a filament preset to Bambuddy",
                extra={"preset_id": row["id"], "status": error.status},
            )
            continue
        await asyncio.to_thread(_mirrored, pool, row["id"], row["sha256"], bb_id)
        sent += 1
    return sent


async def mirror_pending(
    pool: ConnectionPool[Conn],
    store: FilamentPresetStore,
    settings: StoredSettings,
    ids: Collection[int] | None = None,
) -> int:
    """`mirror_with` over a client of its own, while the mirror runs (`wants_mirror`);
    0 otherwise. Never raises: a failure is logged, and the next refresh sends again."""
    if not wants_mirror(settings):
        return 0
    try:
        async with client_for(settings) as client:
            return await mirror_with(client, pool, store, ids)
    except Exception as error:
        logger.warning(
            "the filament preset mirror did not run; the next refresh retries",
            extra={"error": type(error).__name__},
        )
        return 0


async def sync_and_mirror(
    pool: ConnectionPool[Conn],
    blobs: BlobStore,
    held: HeldPresetStore,
    settings: Callable[[], StoredSettings],
) -> None:
    """What the kept presets' refresh runs after it: `sync_from_held`, then every pending
    preset sent (`mirror_pending`), so one that failed before is sent again."""
    await sync_from_held(pool, blobs, held)
    loaded = await asyncio.to_thread(settings)
    await mirror_pending(pool, FilamentPresetStore(pool, lambda: blobs), loaded)
