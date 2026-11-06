"""The render worker's stores over the CRUD routes (#5298). The routes are
`api/crud_render.py`.

`RemoteRenderSettingsSource` is `store/bambuddy.py` `RenderSettingsSource` read from the
API instead of the database: the same 30 s re-read, and a re-read that fails keeps the
last good settings.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from scadbuddy.crud.client import CrudClient
from scadbuddy.crud.guard import PREFIX
from scadbuddy.library.settings_store import RenderStoreSettings
from scadbuddy.store.bambuddy import StoreSettingsSource

BASE = f"/api/v1{PREFIX}/render"


class RemoteRenderSettingsSource(StoreSettingsSource):
    def __init__(
        self,
        client: CrudClient,
        *,
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(ttl=ttl, clock=clock)
        self._client = client

    async def _read(self) -> RenderStoreSettings:
        response = await self._client.request("GET", f"{BASE}/settings")
        return RenderStoreSettings.model_validate(response.json())
