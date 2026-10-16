"""Which store a process runs on, built once at start (spec 2026-09-27 §6.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from pydantic import BaseModel

from scadbuddy.core.config import Config, StoreBackend
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.fonts import FontService
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.settings_store import RenderStoreSettings
from scadbuddy.store import BlobStore
from scadbuddy.store.assets import RemoteAssets
from scadbuddy.store.backends import RECOVER_LOCAL_SQL as RECOVER_LOCAL_SQL
from scadbuddy.store.backends import RemoteBackend, build_backend
from scadbuddy.store.bambuddy import RenderSettingsSource
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.content import ContentStore, StoreUsage
from scadbuddy.store.download_cache import DownloadCache
from scadbuddy.store.fonts import FontMirror
from scadbuddy.store.index import BlobIndex, Pool
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.locks import KeyLocks
from scadbuddy.store.snapshots import SnapshotStore

if TYPE_CHECKING:
    from scadbuddy.core.metrics import Metrics


@dataclass
class StoreBundle:
    backend: StoreBackend
    blobs: BlobStore
    content: ContentStore | None
    snapshots: SnapshotStore | None
    remote_assets: RemoteAssets | None
    fonts: FontMirror | None
    source: RenderSettingsSource
    remote: RemoteBackend | None = None
    #: #2788: whole Bambuddy downloads by sha256, when this process keeps them.
    downloads: DownloadCache | None = None
    #: The local backend's last walk, `(monotonic time, usage)`, for `store_usage`'s max age.
    local_usage: tuple[float, StoreUsage] | None = field(default=None, repr=False)

    async def aclose(self) -> None:
        if self.snapshots is not None:
            await self.snapshots.aclose()
        if self.content is not None:
            await self.content.aclose()
        if self.remote is not None:
            await self.remote.aclose()


def build_store(
    *,
    backend: StoreBackend,
    current: RenderStoreSettings,
    config: Config,
    paths: DataPaths,
    pool: Pool,
    source: RenderSettingsSource,
    history: ModelHistory | None,
    fonts: FontService,
    metrics: Metrics | None,
    downloads_dir: Path | None = None,
) -> StoreBundle:
    """``downloads_dir`` is where this process keeps whole Bambuddy downloads (#2788),
    None for none: the API passes its default, a worker only one it is set (README)."""
    local = LocalBlobStore(paths.blobs)
    downloads = (
        DownloadCache(
            downloads_dir,
            max_bytes=config.download_cache_max_bytes,
            min_age=config.activity_timeout,
        )
        if downloads_dir is not None and config.download_cache_max_bytes > 0
        else None
    )
    if backend == "local":
        return StoreBundle("local", local, None, None, None, None, source, downloads=downloads)
    remote = build_backend(backend, current, source, pool)
    content = ContentStore(
        remote,
        BlobIndex(pool),
        max_total_bytes=config.store_max_total_bytes,
        max_count=config.store_max_count,
        metrics=metrics,
        downloads=downloads,
        spool=paths.cache / "blob-spool",
    )
    blobs = CachedBlobStore(
        local,
        content,
        max_bytes=config.worker_cache_max_bytes,
        min_age=config.activity_timeout,
        metrics=metrics,
    )
    locks = KeyLocks()
    return StoreBundle(
        backend,
        blobs,
        content,
        SnapshotStore(content, paths, history, locks=locks),
        RemoteAssets(content),
        FontMirror(content, fonts, locks=locks),
        source,
        remote,
        downloads=downloads,
    )


def store_usage(bundle: StoreBundle, config: Config, *, max_age: float = 0) -> StoreUsage:
    """The store's totals. The local backend's are a walk of its tree, so a caller that
    asks often (the `/metrics` scrape) passes `max_age` to reuse a walk that recent."""
    if bundle.content is not None:
        return bundle.content.usage()
    now = monotonic()
    if max_age and bundle.local_usage is not None and now - bundle.local_usage[0] < max_age:
        return bundle.local_usage[1]
    blobs = bundle.blobs
    assert isinstance(blobs, LocalBlobStore)
    keys = blobs.keys()  # a list of blob keys, not a dict view
    total = sum(_tree_bytes(blobs.root / key) for key in keys)
    usage = StoreUsage(
        backend="local",
        count=len(keys),
        bytes=total,
        max_count=config.store_max_count,
        max_total_bytes=config.store_max_total_bytes,
        by_kind={"piece": total},
    )
    bundle.local_usage = (now, usage)
    return usage


class StoreHealth(BaseModel):
    #: The backend this process runs on (read at start).
    backend: StoreBackend
    #: The backend stored in Settings; differs from `backend` until a restart.
    configured_backend: StoreBackend
    #: Render workers hold the full Bambuddy key (spec §9): template code can print.
    render_key_fallback: bool
    #: The render worker Deployment may run more than one replica (spec §3.1).
    multi_worker: bool
    #: False while the stored settings cannot be re-read: the fields above are then
    #: the last good ones, and the API's `/healthz` says `degraded`.
    settings_current: bool


async def store_health(bundle: StoreBundle) -> StoreHealth:
    current = await bundle.source.current()
    return StoreHealth(
        backend=bundle.backend,
        configured_backend=current.store_backend,
        render_key_fallback=current.key_is_fallback and bool(current.api_key),
        multi_worker=bundle.backend != "local",
        settings_current=bundle.source.fresh,
    )


def _tree_bytes(root: Path) -> int:
    """The bytes under ``root``. The local sweep may remove a piece mid-walk: what is
    gone by the time it is read counts for nothing, rather than failing the caller."""
    total = 0
    try:
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                continue
    except FileNotFoundError:
        pass
    return total
