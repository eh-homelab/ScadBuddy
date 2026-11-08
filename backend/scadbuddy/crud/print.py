"""The print worker's stores over the CRUD routes (#5298). The routes are
`api/crud_print.py`.

`RemoteSettingsStore` is the print worker's `SettingsReader`: the settings read from the
API on every use, as `SettingsStore.load` reads them from the database. It has no pool:
nothing reads the settings tables through it.
"""

from __future__ import annotations

from scadbuddy.crud.client import CrudSyncClient
from scadbuddy.crud.guard import PREFIX
from scadbuddy.library.settings_store import PrintSettings

BASE = f"/api/v1{PREFIX}/print"


class RemoteSettingsStore:
    """Blocking: called through ``asyncio.to_thread``, as the SQL store is."""

    def __init__(self, client: CrudSyncClient) -> None:
        self._client = client

    def load(self, timeout: float | None = None) -> PrintSettings:
        """``timeout`` bounds the whole read, retries included, as it bounds
        `SettingsStore.load`."""
        response = self._client.request("GET", f"{BASE}/settings", timeout=timeout, budget=timeout)
        return PrintSettings.model_validate(response.json())

    def plate_clear_required(self) -> bool:
        return self.load().lan_require_plate_clear

    def close(self) -> None:
        self._client.close()
