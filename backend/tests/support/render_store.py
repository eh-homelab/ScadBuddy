"""An S3 store for a render worker's test (#5298 plan D11): a render worker with no
database runs only on a remote store, and only the S3 store is one since #7257 (#7882).
`s3_store` serves moto's S3 on a thread of its own (`tests/support/store.py` `moto_s3`);
`use_s3_store` saves it as the store in the API's settings; `pin_snapshot` stores a
revision's snapshot as the API's `pin` does, so the worker, which has no git, renders it."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg.types.json import Jsonb

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.fonts import FontService
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.settings_store import S3Settings, load_render_store_settings
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.store.bambuddy import RenderSettingsSource
from scadbuddy.store.factory import build_store
from scadbuddy.store.index import BlobIndex, Pool
from tests.support.store import moto_s3


@contextmanager
def s3_store() -> Iterator[S3Settings]:
    with moto_s3() as s3:
        yield s3


def use_s3_store(pool: Pool, s3: S3Settings) -> None:
    """Save ``s3`` as the store in the API's settings on ``pool``."""
    values = {
        "store_backend": "s3",
        "store_s3_endpoint": s3.endpoint,
        "store_s3_region": s3.region,
        "store_s3_bucket": s3.bucket,
        "store_s3_prefix": s3.prefix,
        "store_s3_access_key_id": s3.access_key_id,
        "store_s3_secret_access_key": s3.secret_access_key,
        "store_s3_path_style": s3.path_style,
    }
    with pool.connection() as conn:
        for name, value in values.items():
            conn.execute(
                "INSERT INTO settings (name, value) VALUES (%s, %s)"
                " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
                (name, Jsonb(value)),
            )


async def pin_snapshot(pool: Pool, settings: Settings, slug: str, revision: str) -> None:
    """Store ``slug``'s snapshot at ``revision`` as the API does before it starts a render
    on a remote store: from its git, on its store, under ``settings`` (its data dir)."""
    current = load_render_store_settings(pool, settings)
    paths = DataPaths(settings.data_dir)
    source = RenderSettingsSource(pool, settings)
    source.seed(current)
    store = build_store(
        backend=current.store_backend,
        current=current,
        config=settings.to_config(),
        paths=paths,
        pool=pool,
        index=BlobIndex(pool),
        source=source,
        history=ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX),
        fonts=FontService(settings.data_dir),
        metrics=None,
    )
    try:
        assert store.snapshots is not None
        assert await store.snapshots.pin(slug, revision) == revision
    finally:
        await store.aclose()
