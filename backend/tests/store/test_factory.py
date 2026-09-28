from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.fonts import FontService
from scadbuddy.library.settings_store import (
    RenderStoreSettings,
    StoreNotReadyError,
    load_render_store_settings,
)
from scadbuddy.store.bambuddy import RenderSettingsSource
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.factory import build_store, store_health, store_usage
from scadbuddy.store.index import Pool
from scadbuddy.store.local import LocalBlobStore
from tests.conftest import UNUSED_DATABASE_URL

pytestmark = pytest.mark.requires_postgres


READY = RenderStoreSettings(
    store_backend="bambuddy", bambuddy_url="http://bambuddy.test", library_folder_id=7
)


def _build(  # type: ignore[no-untyped-def]
    tmp_path: Path, pool: Pool, backend: str, current: RenderStoreSettings = READY
):
    config = Config(data_dir=tmp_path)
    source = RenderSettingsSource(
        pool, Settings(data_dir=tmp_path, database_url=UNUSED_DATABASE_URL)
    )
    return config, build_store(
        backend=backend,  # type: ignore[arg-type]
        current=current,
        config=config,
        paths=DataPaths(tmp_path),
        pool=pool,
        source=source,
        history=None,
        fonts=FontService(tmp_path),
        metrics=None,
    )


async def test_local_is_phase_one_exactly(tmp_path: Path, pool: Pool) -> None:
    config, bundle = _build(tmp_path, pool, "local", RenderStoreSettings())
    assert isinstance(bundle.blobs, LocalBlobStore) and bundle.content is None
    assert bundle.snapshots is None and bundle.remote_assets is None and bundle.fonts is None
    (bundle.blobs.dir_for("k") / "m").write_bytes(b"12345")
    usage = store_usage(bundle, config)
    assert (usage.backend, usage.count, usage.bytes) == ("local", 1, 5)
    health = await store_health(bundle)
    assert health.backend == "local" and health.multi_worker is False
    await bundle.aclose()


async def test_bambuddy_puts_a_cache_in_front_of_the_remote(tmp_path: Path, pool: Pool) -> None:
    config, bundle = _build(tmp_path, pool, "bambuddy")
    assert isinstance(bundle.blobs, CachedBlobStore)
    assert bundle.blobs.local.root == DataPaths(tmp_path).blobs
    # The worker cache: nothing younger than an activity's budget is evicted.
    assert bundle.blobs.min_age == config.activity_timeout
    assert bundle.blobs.max_bytes == config.worker_cache_max_bytes
    assert bundle.content is not None and bundle.content.name == "bambuddy"
    assert (await store_health(bundle)).multi_worker is True
    await bundle.aclose()


@pytest.mark.parametrize(
    ("url", "folder"), [(None, 7), ("http://bambuddy.test", None), (None, None)]
)
def test_an_unready_bambuddy_backend_is_refused(
    tmp_path: Path, pool: Pool, url: str | None, folder: int | None
) -> None:
    current = READY.model_copy(update={"bambuddy_url": url, "library_folder_id": folder})
    with pytest.raises(StoreNotReadyError):
        _build(tmp_path, pool, "bambuddy", current)


def test_an_env_seeded_bambuddy_backend_without_a_url_or_inbox_is_refused(
    tmp_path: Path, pool: Pool
) -> None:
    """SCADBUDDY_STORE_BACKEND=bambuddy with nothing stored: the seed is refused too."""
    seeded = Settings(data_dir=tmp_path, database_url=UNUSED_DATABASE_URL, store_backend="bambuddy")
    current = load_render_store_settings(pool, seeded)
    assert current.store_backend == "bambuddy"
    with pytest.raises(StoreNotReadyError):
        _build(tmp_path, pool, "bambuddy", current)
