"""The SpoolBuddy scale's tare through Bambuddy (#7155): `filament/scale.py`. Bambuddy is
respx; the route and its answer are Bambuddy's `tare_scale` (routes/spoolbuddy.py, v1.3.1,
plan `docs/superpowers/plans/2026-11-23-spoolbuddy-tare.md`)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.filament import scale

API = "https://bambuddy.test/api/v1"
TARE = f"{API}/spoolbuddy/devices/sb-1/calibration/tare"


@pytest.fixture
async def client() -> AsyncIterator[BambuddyClient]:
    async with BambuddyClient(BambuddyConfig(base_url="https://bambuddy.test", api_key="k")) as c:
        yield c


def _devices(**values: object) -> respx.Route:
    row: dict[str, object] = {
        "id": 1,
        "device_id": "sb-1",
        "hostname": "spoolbuddy",
        "has_nfc": True,
        "has_scale": True,
        "online": True,
        "pending_command": None,
        **values,
    }
    return respx.get(f"{API}/spoolbuddy/devices").mock(return_value=httpx.Response(200, json=[row]))


def _tare() -> respx.Route:
    return respx.post(TARE).mock(
        return_value=httpx.Response(200, json={"status": "ok", "message": "Tare command queued"})
    )


@respx.mock
async def test_a_tare_is_queued_on_a_device_with_a_scale(client: BambuddyClient) -> None:
    _devices()
    route = _tare()

    queued = await scale.tare(client, "sb-1")

    assert route.call_count == 1
    assert queued.device_id == "sb-1"
    assert queued.status == "queued"


@respx.mock
async def test_a_tare_already_pending_is_queued_again(client: BambuddyClient) -> None:
    _devices(pending_command="tare")
    route = _tare()

    await scale.tare(client, "sb-1")

    assert route.call_count == 1


@respx.mock
async def test_a_device_bambuddy_does_not_list_is_refused(client: BambuddyClient) -> None:
    _devices(device_id="sb-2")
    route = _tare()

    with pytest.raises(scale.ScaleRefusedError) as caught:
        await scale.tare(client, "sb-1")

    assert caught.value.code == "no_device"
    assert route.call_count == 0


@respx.mock
async def test_a_device_with_no_scale_is_refused(client: BambuddyClient) -> None:
    _devices(has_scale=False)
    route = _tare()

    with pytest.raises(scale.ScaleRefusedError) as caught:
        await scale.tare(client, "sb-1")

    assert caught.value.code == "no_scale"
    assert route.call_count == 0


@respx.mock
async def test_a_tare_never_overwrites_a_queued_tag_write(client: BambuddyClient) -> None:
    # R-7155-4: Bambuddy keeps one pending command per device, and a tare replaces it.
    _devices(pending_command="write_tag")
    route = _tare()

    with pytest.raises(scale.ScaleRefusedError) as caught:
        await scale.tare(client, "sb-1")

    assert caught.value.code == "device_busy"
    assert "write_tag" in str(caught.value)
    assert route.call_count == 0


@respx.mock
async def test_a_write_queued_between_the_read_and_the_tare_is_still_replaced(
    client: BambuddyClient,
) -> None:
    """The module docstring's race, pinned: the device read and the tare are two calls, so a
    `write_tag` Bambuddy queues between them is replaced by the tare. Bambuddy is a small
    stand-in with its one pending-command slot (`tare_scale` sets it, L1317 at v1.3.1)."""
    slot: dict[str, str | None] = {"pending": None}

    def read(request: httpx.Request) -> httpx.Response:
        row = {"id": 1, "device_id": "sb-1", "hostname": "spoolbuddy", "has_scale": True}
        answer = httpx.Response(200, json=[{**row, "pending_command": slot["pending"]}])
        slot["pending"] = "write_tag"  # a person queues an NTAG write right after the read
        return answer

    def tare(request: httpx.Request) -> httpx.Response:
        slot["pending"] = "tare"
        return httpx.Response(200, json={"status": "ok", "message": "Tare command queued"})

    respx.get(f"{API}/spoolbuddy/devices").mock(side_effect=read)
    route = respx.post(TARE).mock(side_effect=tare)

    queued = await scale.tare(client, "sb-1")

    assert queued.status == "queued"
    assert route.call_count == 1
    assert slot["pending"] == "tare"  # the write is gone, as documented
