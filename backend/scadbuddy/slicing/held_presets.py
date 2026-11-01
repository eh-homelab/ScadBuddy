"""Bambuddy's local presets as last kept (#4645, hot-path plan 2026-10-18 D5): the copy a
native run offers and slices them from, so it reads no Bambuddy.

Why a kept copy and not an import into ScadBuddy's own store: the presets are Bambuddy's
records (its spool intake makes them from 3dfilamentprofiles, and a person edits them
there), and its spools name them by id (``slicer_filament``). An import would be a
second record of each to keep in step with the first, and the ids the spools name would
mean nothing in it. A copy refreshed off the path keeps Bambuddy the one record, as the
spool providers' cache does for its inventory (`spools/store.py`), and costs a native
run nothing but a Postgres read.

The refresh (:func:`refresh_held_presets`) runs on the spool providers' Schedule
(`workflows/spool_refresh.py`, ``held_presets_refresh``), every
``spool_overlay_refresh_seconds``, never on the hot path. It lists ``GET
/local-presets/`` and reads ``GET /local-presets/{id}`` only for a preset it has not
kept, or whose ``updated_at`` moved since (Bambuddy's ``PUT`` rewrites a preset under
the same id and moves it: :attr:`~scadbuddy.bambuddy.models.LocalPreset.updated_at`). A
preset gone from the list is forgotten. One whose read fails is kept as it was, under its
old stamp, so the next refresh reads it again. Without ``bambuddy_url`` it is skipped
and the copy stays, as a spool provider's rows do.

A native slice flattens a kept document over the image's tree (`slicing/presets.py`),
and its key carries the built file's digest, so an edit, once kept, slices again. A
preset the copy lacks (imported since the last refresh) is not offered on a native run
until the next.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from fastapi import status
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from temporalio import activity

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.models import LocalPreset, LocalPresetDetail
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.workflows.spool_refresh import HELD_ACTIVITY

logger = logging.getLogger(__name__)

Conn = Connection[DictRow]
Outcome = Literal["refreshed", "skipped"]


@dataclass(frozen=True)
class HeldRow:
    """A kept preset as a catalogue lists it: its row, without its document, and the
    document's digest, which moves with any edit."""

    preset: LocalPreset
    sha256: str


def setting_sha256(setting: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(setting, sort_keys=True).encode()).hexdigest()


def held_row(detail: LocalPresetDetail) -> HeldRow:
    return HeldRow(
        preset=LocalPreset.model_validate(detail.model_dump(exclude={"setting"})),
        sha256=setting_sha256(detail.setting),
    )


class HeldPresets(Protocol):
    """What a native run reads of the kept copy (:class:`HeldPresetStore`)."""

    async def rows(self) -> list[HeldRow]:
        """Every kept preset, without its document."""
        ...

    async def documents(self, ids: Collection[int]) -> dict[int, LocalPresetDetail]:
        """The kept presets of ``ids`` with their documents; an id not kept is left out."""
        ...


class HeldPresetStore:
    """The kept copy in ``held_local_presets``. Every read and write is blocking I/O, run
    in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    async def rows(self) -> list[HeldRow]:
        return await asyncio.to_thread(self._rows)

    async def documents(self, ids: Collection[int]) -> dict[int, LocalPresetDetail]:
        if not ids:
            return {}
        return await asyncio.to_thread(self._documents, sorted(set(ids)))

    async def stamps(self) -> dict[int, str | None]:
        """Each kept preset's ``updated_at``, by id."""
        return await asyncio.to_thread(self._stamps)

    async def replace(self, listed: Collection[int], read: Collection[LocalPresetDetail]) -> None:
        """Keep ``read`` (each written whole) and forget every preset not ``listed``, in
        one transaction."""
        await asyncio.to_thread(self._replace, sorted(set(listed)), list(read))

    def _rows(self) -> list[HeldRow]:
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT preset - 'setting' AS preset, sha256 FROM held_local_presets ORDER BY id"
            ).fetchall()
        return [
            HeldRow(preset=LocalPreset.model_validate(row["preset"]), sha256=row["sha256"])
            for row in found
        ]

    def _documents(self, ids: list[int]) -> dict[int, LocalPresetDetail]:
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT id, preset FROM held_local_presets WHERE id = ANY(%s)", (ids,)
            ).fetchall()
        return {row["id"]: LocalPresetDetail.model_validate(row["preset"]) for row in found}

    def _stamps(self) -> dict[int, str | None]:
        with self._pool.connection() as conn:
            found = conn.execute("SELECT id, updated_at FROM held_local_presets").fetchall()
        return {row["id"]: row["updated_at"] for row in found}

    def _replace(self, listed: list[int], read: list[LocalPresetDetail]) -> None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM held_local_presets WHERE NOT (id = ANY(%s))", (listed,))
            for detail in read:
                conn.execute(
                    "INSERT INTO held_local_presets (id, preset, updated_at, sha256)"
                    " VALUES (%s, %s, %s, %s)"
                    " ON CONFLICT (id) DO UPDATE SET preset = EXCLUDED.preset,"
                    " updated_at = EXCLUDED.updated_at, sha256 = EXCLUDED.sha256,"
                    " kept_at = now()",
                    (
                        detail.id,
                        Jsonb(detail.model_dump(mode="json")),
                        detail.updated_at,
                        setting_sha256(detail.setting),
                    ),
                )


async def refresh_held_presets(store: HeldPresetStore, client: BambuddyClient) -> int:
    """Bring the kept copy up to Bambuddy's local presets (the module's docstring); how
    many presets were read whole. A failed list changes nothing and raises."""
    listing = await client.local_presets()
    listed = [*listing.printer, *listing.process, *listing.filament]
    stamps = await store.stamps()
    read: list[LocalPresetDetail] = []
    for row in listed:
        if row.id in stamps and row.updated_at is not None and stamps[row.id] == row.updated_at:
            continue
        try:
            read.append(await client.local_preset(row.id))
        except ApiError as error:
            if error.status != status.HTTP_404_NOT_FOUND:
                raise
            # Deleted since the list: forgotten by the next refresh's.
            logger.info("a local preset went while it was kept", extra={"preset_id": row.id})
    await store.replace([row.id for row in listed], read)
    return len(read)


def held_presets_activity(
    store: Callable[[], HeldPresetStore], load: Callable[[], StoredSettings]
) -> Callable[[], Any]:
    """``held_presets_refresh``, on the ``library`` worker beside the spool providers'
    activities; ``store`` and ``load`` are asked at each run, as the app's state is set
    up after its worker's activities are listed. Raises on a failed refresh, which the
    workflow logs; the next tick is the retry."""

    @activity.defn(name=HELD_ACTIVITY)
    async def held_presets_refresh() -> Outcome:
        return await refresh_held(store(), load)

    return held_presets_refresh


async def refresh_held(store: HeldPresetStore, load: Callable[[], StoredSettings]) -> Outcome:
    settings = await asyncio.to_thread(load)
    if not settings.bambuddy_url:
        return "skipped"
    async with client_for(settings) as client:
        await refresh_held_presets(store, client)
    return "refreshed"
