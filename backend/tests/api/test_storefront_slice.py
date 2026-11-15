"""The shop slice profile's choices (#6153, #6255 review): the options route on
Bambuddy's path, and the spools of a native route, which are the loaded trays."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api import storefront_slice
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.models import PrinterCreate
from tests.api.test_settings_runtime import _state

OPTIONS = "/api/v1/storefront/slice-profile/options"


def _printer(client: TestClient) -> str:
    store = _state(client).components.get(PRINTERS).store
    return store.create(PrinterCreate(serial="01P00A0000000AA", name="Garage")).id


@respx.mock
def test_the_options_list_bambuddys_spools_for_a_printer(client: TestClient) -> None:
    printer = _printer(client)
    client.put("/api/v1/settings", json={"bambuddy_url": "https://bambuddy.test"})
    respx.get("https://bambuddy.test/api/v1/inventory/spools").mock(
        return_value=httpx.Response(
            200, json=[{"id": 7, "material": "PLA", "brand": "Bambu", "color_name": "Red"}]
        )
    )
    answer = client.get(OPTIONS, params={"printer": printer})
    assert answer.status_code == 200, answer.text
    spools = answer.json()["spools"]
    assert [(s["spool_id"], s["material"]) for s in spools] == [(7, "PLA")]
    assert spools[0]["label"] == "Bambu PLA Red (#7)"


def test_the_options_name_no_spool_without_a_printer(client: TestClient) -> None:
    answer = client.get(OPTIONS)
    assert answer.status_code == 200, answer.text
    assert answer.json()["spools"] == []


def test_the_options_refuse_a_printer_the_registry_lacks(client: TestClient) -> None:
    answer = client.get(OPTIONS, params={"printer": "nowhere"})
    assert answer.status_code == 422


@pytest.mark.asyncio
async def test_a_native_route_offers_its_loaded_trays(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a native route the spools are the loaded trays, with their (negative) ids."""
    row = SimpleNamespace(id="p1", name="H2C")
    route = SimpleNamespace(row=row, status=object(), printer_id=None)

    async def a_route(*_: Any, **__: Any) -> Any:
        return route

    async def no_overlay(*_: Any, **__: Any) -> dict[str, Any]:
        return {}

    tray = SimpleNamespace(
        spool_id=-3, material="PETG", colour="#00FF00", label="Bambu", color_name="Green"
    )
    monkeypatch.setattr(storefront_slice, "dialog_route", a_route)
    monkeypatch.setattr(storefront_slice, "status_overlay", no_overlay)
    monkeypatch.setattr(storefront_slice, "loaded_trays", lambda *_: [tray])

    class Store:
        def get(self, printer: str) -> Any:
            return row if printer == "p1" else None

    printers: Any = SimpleNamespace(store=Store(), reports=None)
    offered = await storefront_slice.offered_spools(StoredSettings(), printers, None, "p1")  # type: ignore[arg-type]
    assert offered is not None
    assert [(s.spool_id, s.material, s.label) for s in offered] == [
        (-3, "PETG", "Bambu PETG Green")
    ]
    assert await storefront_slice.offered_spools(StoredSettings(), printers, None, "p2") is None  # type: ignore[arg-type]
