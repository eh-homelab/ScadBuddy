"""A Bambuddy store for a render worker's test (#5298 plan D11): a render worker with no
database runs only on a remote store. `fake_bambuddy` serves the in-memory Bambuddy
library (`fake_bambuddy_library`) on a thread of its own, with the store's inbox made;
`use_bambuddy_store` saves it as the store in the API's settings; `pin_snapshot` stores a
revision's snapshot as the API's `pin` does, so the worker, which has no git, renders it."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import httpx
import uvicorn
from psycopg.types.json import Jsonb

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.fonts import FontService
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.settings_store import RenderStoreSettings, load_render_store_settings
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.store.bambuddy import RenderSettingsSource
from scadbuddy.store.factory import build_store
from scadbuddy.store.index import BlobIndex, Pool
from tests.support.fake_bambuddy_library import build_app

#: The API key the store settings carry; the fake Bambuddy takes any.
API_KEY = "k"


@dataclass(frozen=True)
class FakeBambuddy:
    url: str
    inbox: int

    def store_settings(self) -> RenderStoreSettings:
        """The store settings that name it, as the API's CRUD route answers them."""
        return RenderStoreSettings(
            store_backend="bambuddy",
            bambuddy_url=self.url,
            library_folder_id=self.inbox,
            api_key=API_KEY,
        )


@contextmanager
def fake_bambuddy() -> Iterator[FakeBambuddy]:
    server = uvicorn.Server(
        uvicorn.Config(build_app(), host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, name="fake bambuddy", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the server did not start"
        time.sleep(0.01)
    url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    try:
        made = httpx.post(
            f"{url}/api/v1/library/folders/", json={"name": "ScadBuddy", "parent_id": None}
        )
        made.raise_for_status()
        yield FakeBambuddy(url, made.json()["id"])
    finally:
        server.should_exit = True
        thread.join(30)


def use_bambuddy_store(pool: Pool, fake: FakeBambuddy) -> None:
    """Save ``fake`` as the store in the API's settings on ``pool``."""
    values = {
        "store_backend": "bambuddy",
        "bambuddy_url": fake.url,
        "library_folder_id": fake.inbox,
        "bambuddy_api_key": API_KEY,
        "bambuddy_render_api_key": API_KEY,
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
