"""Which store a process runs on, built once at start (spec 2026-09-27 §6.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING

from pydantic import BaseModel

from scadbuddy.core.config import Config, StoreBackend
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.fonts import FontService
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.settings_store import RenderStoreSettings, StoreNotReadyError
from scadbuddy.store import BlobStore
from scadbuddy.store.assets import RemoteAssets
from scadbuddy.store.bambuddy import BambuddyContentBackend, RenderSettingsSource
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.content import ContentStore, StoreUsage
from scadbuddy.store.fonts import FontMirror
from scadbuddy.store.index import BlobIndex, Pool
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.locks import KeyLocks
from scadbuddy.store.snapshots import SnapshotStore

if TYPE_CHECKING:
    from scadbuddy.core.metrics import Metrics


#: The recovery from a refused start: back to the local store (README, "Deploying").
RECOVER_LOCAL_SQL = "UPDATE settings SET value = '\"local\"' WHERE name = 'store_backend';"


@dataclass
class StoreBundle:
    backend: StoreBackend
    blobs: BlobStore
    content: ContentStore | None
    snapshots: SnapshotStore | None
    remote_assets: RemoteAssets | None
    fonts: FontMirror | None
    source: RenderSettingsSource
    remote: BambuddyContentBackend | None = None
    #: The local backend's last walk, `(monotonic time, usage)`, for `store_usage`'s max age.
    local_usage: tuple[float, StoreUsage] | None = field(default=None, repr=False)

    async def aclose(self) -> None:
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
) -> StoreBundle:
    local = LocalBlobStore(paths.blobs)
    if backend == "local":
        return StoreBundle("local", local, None, None, None, None, source)
    # From any source, the environment's seed included: a Bambuddy store without a URL
    # or an inbox could store nothing, so the process does not start on it.
    if not current.bambuddy_url or current.library_folder_id is None:
        raise StoreNotReadyError(
            "store_backend is bambuddy, but the Bambuddy store needs a Bambuddy URL and a"
            " library folder (its inbox). To start on the local store, run"
            f" {RECOVER_LOCAL_SQL} in ScadBuddy's database (or, when no store_backend is"
            " stored, set SCADBUDDY_STORE_BACKEND=local), then set both in Settings."
            ' See README, "Recovering an unready blob store".'
        )
    remote = BambuddyContentBackend(source.target, pool)
    content = ContentStore(
        remote,
        BlobIndex(pool),
        max_total_bytes=config.store_max_total_bytes,
        max_count=config.store_max_count,
        metrics=metrics,
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
        "bambuddy",
        blobs,
        content,
        SnapshotStore(content, paths, history, locks=locks),
        RemoteAssets(content),
        FontMirror(content, fonts, locks=locks),
        source,
        remote,
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
    total = sum(
        p.stat().st_size for key in keys for p in (blobs.root / key).rglob("*") if p.is_file()
    )
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


async def store_health(bundle: StoreBundle) -> StoreHealth:
    current = await bundle.source.current()
    return StoreHealth(
        backend=bundle.backend,
        configured_backend=current.store_backend,
        render_key_fallback=current.key_is_fallback and bool(current.api_key),
        multi_worker=bundle.backend != "local",
    )
