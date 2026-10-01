"""`// file` uploads through the store (spec 2026-09-27 §6.1). The API's `AssetStore`
stays the place uploads are validated, capped and swept (#204, #296); this mirrors each
upload into the store and brings it into a worker's own `AssetStore` before a render."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime

from scadbuddy.library.assets import (
    AssetMeta,
    AssetNotFoundError,
    AssetRejectedError,
    AssetStore,
)
from scadbuddy.store.content import BlobCorruptError, BlobMissingError, BlobScope, ContentStore

logger = logging.getLogger(__name__)


#: The meta flag on a copy whose upload a sweep removed; `mirror` rewrites meta without it.
SWEPT = "swept"


def asset_key(asset_id: str) -> str:
    return f"asset-{asset_id}"


class RemoteAssets:
    def __init__(self, content: ContentStore) -> None:
        self.content = content

    async def mirror(
        self, store: AssetStore, meta: AssetMeta, *, slug: str | None, title: str | None
    ) -> None:
        data = await asyncio.to_thread(store.blob_path(meta).read_bytes)
        await self.content.put(
            "asset",
            data,
            name=f"asset-{meta.id}.{meta.kind}",
            scope=BlobScope(slug=slug, title=title),
            key=asset_key(meta.id),
            meta=meta.model_dump(),
        )

    async def clock(self) -> datetime:
        """Now, by the database's clock: a sweep's cutoff is compared with `touched_at`."""
        return await asyncio.to_thread(self.content.index.now)

    async def ensure(self, store: AssetStore, ids: Iterable[str]) -> list[str]:
        """Bring every id the store holds into this process's ``store``; the ids it could
        not provide, each logged by id and reason: the render then refuses the parameter
        as not in the blob store."""
        absent: list[str] = []
        for asset_id in sorted(set(ids)):
            try:
                # `use`, not `get`: a hit is a use, so the worker's own sweep (by last
                # use) never takes an upload a render in flight is about to read.
                await asyncio.to_thread(store.use, asset_id)
                continue
            except AssetNotFoundError:
                pass
            key = asset_key(asset_id)
            stat = await asyncio.to_thread(self.content.index.get, key)
            # This backend's row only: `read` downloads from this backend.
            if stat is None or stat.ref.backend != self.content.name:
                # `asset_ids_in` is loose: any 64-hex value in the params is a candidate.
                logger.info(
                    "a parameter's value is not an upload in the blob store",
                    extra={"asset_id": asset_id, "reason": "no row"},
                )
                absent.append(asset_id)
                continue
            try:
                data = await self.content.read(stat.ref)
                await asyncio.to_thread(store.adopt, AssetMeta.model_validate(stat.meta), data)
            except (BlobMissingError, BlobCorruptError, AssetRejectedError) as error:
                logger.warning(
                    "an uploaded file is gone or altered in the blob store",
                    extra={"asset_id": asset_id, "reason": repr(error)},
                )
                await self._drop_bad(key)
                absent.append(asset_id)
        return absent

    async def _drop_bad(self, key: str) -> None:
        """Drop a copy that cannot be read, object too, so the API's `backfill` mirrors
        the upload again; only the row when the object cannot be removed."""
        try:
            await self.content.delete(key)
        except Exception:
            logger.exception("could not remove an unreadable upload's copy", extra={"key": key})
            await self.content.forget(key)

    async def drop(self, ids: Iterable[str], *, cutoff: datetime) -> list[str]:
        """Remove the store's copies of uploads swept from the volume, the ids dropped.
        Only a copy not stored again since ``cutoff`` (the sweep's start): a re-upload
        during the sweep bumps its `touched_at` and keeps it. Each copy is first marked
        swept, so a failure (logged) leaves it for the next sweep's `reconcile`."""
        ids = list(ids)
        await asyncio.to_thread(
            self.content.index.mark,
            [asset_key(asset_id) for asset_id in ids],
            backend=self.content.name,
            cutoff=cutoff,
            **{SWEPT: True},
        )
        dropped: list[str] = []
        for asset_id in ids:
            try:
                if await self.content.delete_if_stale(asset_key(asset_id), cutoff):
                    dropped.append(asset_id)
            except Exception:
                logger.exception(
                    "could not remove a swept upload's copy", extra={"asset_id": asset_id}
                )
        return dropped

    async def reconcile(self, store: AssetStore, *, cutoff: datetime) -> list[str]:
        """Retry the drops a sweep decided and could not finish: copies marked swept
        whose upload ``store`` still lacks. Never a diff against the volume: a copy the
        volume merely lacks (restored from a backup) may be the only one left."""
        rows = await asyncio.to_thread(
            self.content.index.stats, ["asset"], backend=self.content.name
        )
        local = set(await asyncio.to_thread(store.ids))
        orphans = [
            asset_id
            for asset_id, row in ((row.key.removeprefix(asset_key("")), row) for row in rows)
            if row.meta.get(SWEPT) and asset_id not in local
        ]
        return await self.drop(orphans, cutoff=cutoff)

    async def backfill(self, store: AssetStore) -> int:
        """Mirror every upload the store has no copy of on this backend."""
        done = 0
        for asset_id in await asyncio.to_thread(store.ids):
            stat = await asyncio.to_thread(self.content.index.get, asset_key(asset_id))
            if stat is None or stat.ref.backend != self.content.name:
                try:
                    meta = await asyncio.to_thread(store.get, asset_id)
                except AssetNotFoundError:
                    continue  # swept since `ids()`
                await self.mirror(store, meta, slug=None, title=None)
                done += 1
        return done
