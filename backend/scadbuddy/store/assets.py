"""`// file` uploads through the store (spec 2026-09-27 §6.1). The API's `AssetStore`
stays the place uploads are validated, capped and swept (#204, #296); this mirrors each
upload into the store and brings it into a worker's own `AssetStore` before a render."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from scadbuddy.library.assets import (
    AssetMeta,
    AssetNotFoundError,
    AssetRejectedError,
    AssetStore,
)
from scadbuddy.store.content import BlobCorruptError, BlobMissingError, BlobScope, ContentStore


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

    async def ensure(self, store: AssetStore, ids: Iterable[str]) -> list[str]:
        fetched: list[str] = []
        for asset_id in sorted(set(ids)):
            try:
                await asyncio.to_thread(store.get, asset_id)
                continue
            except AssetNotFoundError:
                pass
            key = asset_key(asset_id)
            stat = await asyncio.to_thread(self.content.index.get, key)
            # This backend's row only: `read` downloads from this backend.
            if stat is None or stat.ref.backend != self.content.name:
                continue
            try:
                data = await self.content.read(stat.ref)
                await asyncio.to_thread(store.adopt, AssetMeta.model_validate(stat.meta), data)
            except (BlobMissingError, BlobCorruptError, AssetRejectedError):
                # Gone or altered: forget it, so the API's `backfill` mirrors it again;
                # the render reports the id as missing, as for an unknown one.
                await self.content.forget(key)
                continue
            fetched.append(asset_id)
        return fetched

    async def drop(self, ids: Iterable[str]) -> None:
        for asset_id in ids:
            await self.content.delete(asset_key(asset_id))

    async def backfill(self, store: AssetStore) -> int:
        done = 0
        for asset_id in await asyncio.to_thread(store.ids):
            stat = await asyncio.to_thread(self.content.index.get, asset_key(asset_id))
            if stat is None or stat.ref.backend != self.content.name:
                meta = await asyncio.to_thread(store.get, asset_id)
                await self.mirror(store, meta, slug=None, title=None)
                done += 1
        return done
