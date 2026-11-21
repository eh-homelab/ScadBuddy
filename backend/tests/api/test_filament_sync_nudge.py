"""The API's writes nudge the filament sync (#6265 Task 8, plan Decision 17): every 2xx
non-GET answer of the ``/filament`` and tray-spool routers, and a settings save that
changes ``filament_sync``. ``filament_nudges`` (`tests/api/conftest.py`) records them."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.filament import ntag
from scadbuddy.filament.models import ScanResult
from tests.api.test_filament import SPOOLS, _spool
from tests.api.test_send import API, configure
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


@respx.mock
def test_the_bambuddy_import_and_the_rfid_scan_do_not_nudge(
    client: TestClient, filament_nudges: list[bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 163: the import pulls and the scan writes nothing, so neither nudges a
    push pass."""
    configure(client)
    filament_nudges.clear()
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/inventory/catalog").mock(return_value=httpx.Response(200, json=[]))
    imported = client.post("/api/v1/filament/bambuddy/import")
    assert imported.status_code == 200, imported.text

    async def scan(*args: Any, **kwargs: Any) -> ScanResult:
        return ScanResult(status="timeout")

    monkeypatch.setattr(ntag, "scan", scan)
    scanned = client.post("/api/v1/filament/rfid/scan", json={"timeout_s": 1})
    assert scanned.status_code == 200, scanned.text
    assert scanned.json()["status"] == "timeout"
    assert filament_nudges == []


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


def test_a_save_that_stops_every_push_signals_only(
    client: TestClient, filament_nudges: list[bool], filament_signals: list[str | None]
) -> None:
    """R-T10-3: a running workflow learns that nothing pushes (its next pass is
    ``nothing_pushes`` and it ends); no workflow is started for it."""
    saved = client.put("/api/v1/settings", json={"filament_sync": {"locations": "two_way"}})
    assert saved.is_success
    assert (filament_nudges, filament_signals) == ([False], [])

    stopped = client.put("/api/v1/settings", json={"filament_sync": {"locations": "bb_to_sb"}})
    assert stopped.status_code == 200, stopped.text
    assert (filament_nudges, filament_signals) == ([False], [None])

    # Nothing pushed before either: no signal, no nudge.
    again = client.put("/api/v1/settings", json={"filament_sync": {"locations": "off"}})
    assert again.status_code == 200, again.text
    assert (filament_nudges, filament_signals) == ([False], [None])


def test_a_save_of_the_interval_signals_a_running_workflow(
    client: TestClient, filament_nudges: list[bool], filament_signals: list[str | None]
) -> None:
    """Decision 153: a running workflow waits the interval its last pass read; a new one
    reaches it by a signal (a pass then reads it), also from 0 (no timer). Nothing is
    started, and the same value again sends nothing."""
    assert client.put("/api/v1/settings", json={"filament_sync": {"spools": "two_way"}}).is_success
    filament_nudges.clear()

    # The test app starts with 0 (no timer).
    on = client.put("/api/v1/settings", json={"spool_overlay_refresh_seconds": 600})
    assert on.status_code == 200, on.text
    assert (filament_nudges, filament_signals) == ([], [None])

    again = client.put("/api/v1/settings", json={"spool_overlay_refresh_seconds": 600})
    assert again.status_code == 200, again.text
    assert (filament_nudges, filament_signals) == ([], [None])

    off = client.put("/api/v1/settings", json={"spool_overlay_refresh_seconds": 0})
    assert off.status_code == 200, off.text
    assert (filament_nudges, filament_signals) == ([], [None, None])


def test_a_save_of_the_bambuddy_url_nudges_when_something_pushes(
    client: TestClient, filament_nudges: list[bool], filament_signals: list[str | None]
) -> None:
    """Decision 153: a new ``bambuddy_url`` starts that instance's workflow at once when
    something pushes (its plan answers ``instance_changed`` when the links belong to the
    old one); with nothing pushing it starts nothing."""
    moved = client.put("/api/v1/settings", json={"bambuddy_url": "http://bambuddy-a.test"})
    assert moved.status_code == 200, moved.text
    assert (filament_nudges, filament_signals) == ([], [])

    assert client.put("/api/v1/settings", json={"filament_sync": {"spools": "two_way"}}).is_success
    filament_nudges.clear()
    moved = client.put("/api/v1/settings", json={"bambuddy_url": "http://bambuddy-b.test"})
    assert moved.status_code == 200, moved.text
    assert (filament_nudges, filament_signals) == ([False], ["http://bambuddy-a.test"])


def test_a_save_of_the_bambuddy_url_signals_the_old_instance(
    client: TestClient, filament_nudges: list[bool], filament_signals: list[str | None]
) -> None:
    """Decision 161: a new ``bambuddy_url`` signals the old instance's workflow, so its
    next pass is ``instance_changed`` and it ends now, not at its next timer. The same URL
    again, and a first URL, signal nothing."""
    assert client.put(
        "/api/v1/settings", json={"bambuddy_url": "http://bambuddy-a.test"}
    ).is_success
    filament_signals.clear()
    same = client.put("/api/v1/settings", json={"bambuddy_url": "http://bambuddy-a.test"})
    assert same.status_code == 200, same.text
    assert filament_signals == []
    moved = client.put("/api/v1/settings", json={"bambuddy_url": "http://bambuddy-b.test"})
    assert moved.status_code == 200, moved.text
    assert filament_signals == ["http://bambuddy-a.test"]
    cleared = client.put("/api/v1/settings", json={"bambuddy_url": None})
    assert cleared.status_code == 200, cleared.text
    assert filament_signals == ["http://bambuddy-a.test", "http://bambuddy-b.test"]
    assert filament_nudges == []


def test_saving_another_setting_does_not_nudge(
    client: TestClient, filament_nudges: list[bool]
) -> None:
    saved = client.put("/api/v1/settings", json={"filament_low_stock_pct": 15})
    assert saved.status_code == 200, saved.text
    assert filament_nudges == []
