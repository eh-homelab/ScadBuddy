"""The API's writes nudge the filament sync (#6265 Task 8, plan Decision 17): every 2xx
non-GET answer of the ``/filament`` and tray-spool routers, and a settings save that
changes ``filament_sync``. ``filament_nudges`` (`tests/api/conftest.py`) records them."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.test_filament import SPOOLS, _spool
from tests.api.test_tray_spools import _path, _sb_spool, lan_printer  # noqa: F401

pytestmark = pytest.mark.requires_postgres


def test_a_filament_write_nudges(client: TestClient, filament_nudges: list[bool]) -> None:
    one = f"{SPOOLS}/{_spool(client)['id']}"
    filament_nudges.clear()
    rows = [{"printer_model": "H2C", "nozzle_diameter": "0.4", "slicer_filament": "P1"}]
    saved = client.put(f"{one}/presets", json=rows)
    assert saved.status_code == 200, saved.text
    assert filament_nudges == [False]


def test_a_read_or_a_failed_write_does_not_nudge(
    client: TestClient, filament_nudges: list[bool]
) -> None:
    one = f"{SPOOLS}/{_spool(client)['id']}"
    filament_nudges.clear()
    assert client.get(one).status_code == 200
    rows = [{"printer_model": "H2C", "nozzle_diameter": "0.4", "slicer_filament": "P1"}]
    assert client.put(f"{one}/presets", json=[rows[0], rows[0]]).status_code == 422
    assert client.put(f"{one}/presets", json={"not": "a list"}).status_code == 422
    assert filament_nudges == []


def test_the_ams_weights_sync_does_not_nudge(
    client: TestClient, filament_nudges: list[bool]
) -> None:
    """R-T8-3, Decision 17: the AMS weights reach Bambuddy on the timer; another write on
    the same router still nudges."""
    synced = client.post(f"{SPOOLS}/sync-ams-weights")
    assert synced.status_code == 200, synced.text
    assert filament_nudges == []
    _spool(client)
    assert filament_nudges == [False]


def test_a_purchasing_write_nudges(client: TestClient, filament_nudges: list[bool]) -> None:
    made = client.post("/api/v1/filament/suppliers", json={"name": "Bambu Lab"})
    assert made.status_code == 201, made.text
    assert filament_nudges == [False]


def test_a_tray_spool_write_nudges(
    client: TestClient,
    lan_printer: str,  # noqa: F811
    filament_nudges: list[bool],
) -> None:
    spool = _sb_spool(client)
    linked = client.put(_path(lan_printer), json={"spool_id": str(spool)})
    assert linked.status_code == 200, linked.text
    assert filament_nudges == [False]


def test_saving_filament_sync_nudges(client: TestClient, filament_nudges: list[bool]) -> None:
    saved = client.put("/api/v1/settings", json={"filament_sync": {"spools": "two_way"}})
    assert saved.status_code == 200, saved.text
    assert filament_nudges == [False]
    # The same directions again change nothing: no nudge.
    again = client.put("/api/v1/settings", json={"filament_sync": {"spools": "two_way"}})
    assert again.status_code == 200, again.text
    assert filament_nudges == [False]


def test_saving_another_setting_does_not_nudge(
    client: TestClient, filament_nudges: list[bool]
) -> None:
    saved = client.put("/api/v1/settings", json={"filament_low_stock_pct": 15})
    assert saved.status_code == 200, saved.text
    assert filament_nudges == []
