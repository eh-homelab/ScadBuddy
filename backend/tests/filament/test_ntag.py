"""NTAG scan, write, confirm and cancel through SpoolBuddy (#6264 Task 8):
`filament/ntag.py`. Bambuddy is respx and the scan is a stub of the client's read; the
routes and bodies are the ones in `tests/bambuddy/recordings/openapi/rfid-routes.json`."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.models import Spool, TagScan
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import ntag
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.component import Filament
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.invoices import InvoiceStore
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.metrics_store import MetricsSettings, MetricsStore
from scadbuddy.filament.models import Material, SpoolCreate
from scadbuddy.filament.presets import PoolSpoolPresets
from scadbuddy.filament.rfid_mode import RfidModeStore
from scadbuddy.filament.sensors import SensorStore
from scadbuddy.filament.shopping import ShoppingStore
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.filament.types import SpoolTypeStore
from tests.bambuddy.test_client_scan import PRINTER_STATUS, UNKNOWN, _fake
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

API = "https://bambuddy.test/api/v1"
UID = "04AABBCC"
OLD = "AABBCCDD11223344"
TRAY = "0123456789ABCDEF0123456789ABCDEF"


def _record(bb_id: int, uid: str | None, origin: str | None = "opentag3d") -> respx.Route:
    return respx.get(f"{API}/inventory/spools/{bb_id}").mock(
        return_value=httpx.Response(
            200, json={"id": bb_id, "material": "PLA", "tag_uid": uid, "data_origin": origin}
        )
    )


async def _queue(f: Filament, client: BambuddyClient, spool: UUID, was: str | None) -> None:
    """Queue a write while the Bambuddy record holds ``was``."""
    _record(11, was)
    respx.post(f"{API}/spoolbuddy/nfc/write-tag").mock(
        return_value=httpx.Response(200, json={"status": "queued", "warnings": []})
    )
    await ntag.write(f, client, spool, "sb-1", replace=True)


def _filament(pool: PgPool) -> Filament:
    types = SpoolTypeStore(pool)
    spools = FilamentSpoolStore(pool)
    return Filament(
        catalog=CatalogStore(pool),
        types=types,
        spools=spools,
        locations=LocationStore(pool),
        inventory=FilamentInventory(pool),
        presets=PoolSpoolPresets(pool),
        sensors=SensorStore(pool),
        rfid=RfidModeStore(pool),
        suppliers=SuppliersStore(pool, lambda: "USD"),
        invoices=InvoiceStore(pool, lambda: "USD", spools, types),
        shopping=ShoppingStore(pool),
        metrics=MetricsStore(pool, spools, MetricsSettings),
    )


@pytest.fixture
async def client() -> AsyncIterator[BambuddyClient]:
    async with BambuddyClient(BambuddyConfig(base_url="https://bambuddy.test", api_key="k")) as c:
        yield c


def _spool(f: Filament, pool: PgPool, bb_id: int | None = None) -> UUID:
    pla = next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    spool = f.spools.create(SpoolCreate(material_id=pla), actor="ann").id
    if bb_id is not None:
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO filament_bb_links (spool_id, bb_spool_id) VALUES (%s, %s)",
                (spool, bb_id),
            )
    return spool


def _scan(**over: object) -> TagScan:
    base: dict[str, object] = {
        "device_id": "sb-1",
        "tag_uid": UID,
        "tray_uuid": None,
        "tag_type": "NTAG215",
        "bb_spool_id": None,
    }
    return TagScan.model_validate({**base, **over})


class _Scanner:
    """A client whose `next_tag_scan` answers ``scan``, as the socket would."""

    def __init__(self, scan: TagScan | None, real: BambuddyClient | None = None) -> None:
        self._scan = scan
        self._real = real
        self.asked: list[tuple[float, str | None]] = []

    async def spool_row(self, spool_id: int) -> Spool:
        assert self._real is not None
        return await self._real.spool_row(spool_id)

    async def next_tag_scan(
        self, *, timeout: float, device_id: str | None = None
    ) -> TagScan | None:
        self.asked.append((timeout, device_id))
        return self._scan


def test_scan_kind() -> None:
    assert ntag.scan_kind(_scan(tray_uuid=TRAY, tag_type=None)) == "bambu_mifare"
    assert ntag.scan_kind(_scan(tag_type="NTAG215")) == "ntag_opentag"
    assert ntag.scan_kind(_scan(tag_type="ntag213")) == "ntag_opentag"
    assert ntag.scan_kind(_scan(tag_type=None)) == "other"


async def test_a_timed_out_scan_has_no_tag(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    scanner = _Scanner(None)

    result = await ntag.scan(f, scanner, timeout=2, device_id="sb-1")  # type: ignore[arg-type]

    assert result.status == "timeout"
    assert (result.tag_uid, result.kind, result.sb_spool) == (None, None, None)
    assert scanner.asked == [(2, "sb-1")]


async def test_a_scan_names_the_sb_spool_that_holds_the_tag(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    holder = _spool(f, pg_pool)
    f.spools.bind_tag(holder, tag_uid=UID, kind="ntag_opentag", actor="ann")
    linked = _spool(f, pg_pool, 11)

    by_keys = await ntag.scan(f, _Scanner(_scan()), timeout=1, device_id=None)  # type: ignore[arg-type]
    by_link = await ntag.scan(
        f,
        _Scanner(_scan(tag_uid="04FFFFFF", bb_spool_id=11)),  # type: ignore[arg-type]
        timeout=1,
        device_id=None,
    )
    unknown = await ntag.scan(
        f,
        _Scanner(_scan(tag_uid="04EEEEEE")),  # type: ignore[arg-type]
        timeout=1,
        device_id=None,
    )

    assert by_keys.status == "tag"
    assert by_keys.kind == "ntag_opentag"
    assert by_keys.sb_spool is not None
    assert by_keys.sb_spool.id == holder
    assert by_link.sb_spool is not None
    assert by_link.sb_spool.id == linked
    assert by_link.bambuddy_spool_id == 11
    assert unknown.sb_spool is None


async def test_a_frame_on_the_socket_reaches_the_scan_result(pg_pool: PgPool) -> None:
    """One end-to-end read: a frame on a local websockets fake, through the client's
    `next_tag_scan` and `ntag.scan`, to the SB spool that holds the tag."""
    f = _filament(pg_pool)
    holder = _spool(f, pg_pool)
    f.spools.bind_tag(holder, tag_uid=UID, kind="ntag_opentag", actor="ann")
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"http://127\.0\.0\.1:\d+/api/v1/auth/status").mock(
            return_value=httpx.Response(200, json={"auth_enabled": False})
        )
        async with _fake([PRINTER_STATUS, UNKNOWN]) as socket_client:
            result = await ntag.scan(f, socket_client, timeout=5, device_id="sb-1")

    assert (result.status, result.tag_uid, result.kind) == ("tag", UID, "ntag_opentag")
    assert result.sb_spool is not None and result.sb_spool.id == holder


@respx.mock
async def test_write_queues_for_the_linked_bambuddy_spool(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    _record(11, None)
    route = respx.post(f"{API}/spoolbuddy/nfc/write-tag").mock(
        return_value=httpx.Response(
            200, json={"status": "queued", "warnings": ["brand/vendor not set"]}
        )
    )

    queued = await ntag.write(f, client, spool, "sb-1", replace=False)

    assert queued.status == "queued"
    assert queued.warnings == ["brand/vendor not set"]
    assert json.loads(route.calls.last.request.content) == {"device_id": "sb-1", "spool_id": 11}


@respx.mock
async def test_write_without_a_bambuddy_link_is_a_409(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool)

    with pytest.raises(ntag.NtagNoBambuddySpoolError) as caught:
        await ntag.write(f, client, spool, "sb-1", replace=False)

    assert caught.value.code == "no_bambuddy_spool"
    assert len(respx.calls) == 0
    assert f.spools.open_binding(spool) is None


@respx.mock
async def test_write_over_an_override_tag_is_refused_unless_replace(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    f.spools.bind_tag(spool, tray_uuid=TRAY, kind="bambu_mifare", override=True, actor="ann")
    before = f.spools.open_binding(spool)
    _record(11, None)
    route = respx.post(f"{API}/spoolbuddy/nfc/write-tag").mock(
        return_value=httpx.Response(200, json={"status": "queued", "warnings": []})
    )

    with pytest.raises(ntag.NtagOverrideOpenError) as caught:
        await ntag.write(f, client, spool, "sb-1", replace=False)
    assert caught.value.code == "override_tag_open"
    assert route.call_count == 0

    queued = await ntag.write(f, client, spool, "sb-1", replace=True)

    assert queued.status == "queued"
    assert route.call_count == 1
    assert f.spools.open_binding(spool) == before  # only confirm binds


@respx.mock
async def test_confirm_binds_the_new_uid_as_ntag_opentag(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    f.spools.bind_tag(spool, tag_uid=OLD, kind="other", actor="ann")
    await _queue(f, client, spool, None)
    _record(11, UID)

    first = await ntag.confirm(f, client, spool, actor="ann")
    second = await ntag.confirm(f, client, spool, actor="ann")

    assert (first.bound, first.tag_uid) == (True, UID)
    assert second.bound is False
    binding = f.spools.open_binding(spool)
    assert binding is not None
    tag = f.spools.tag(binding.tag_id)
    assert tag is not None
    assert (tag.kind, tag.tag_uid) == ("ntag_opentag", UID)
    events = [e.kind for e in f.spools.events(spool)]
    assert events.count("tag_bound") == 2  # the old tag's, then the new one


@respx.mock
async def test_confirm_does_not_bind_the_tag_the_record_held_when_the_write_queued(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    """The person unbound NTAG X, whose UID Bambuddy's record still holds: the first poll
    after the write is queued must not bind X again."""
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    await _queue(f, client, spool, UID)

    first = await ntag.confirm(f, client, spool, actor="ann")
    assert first.bound is False
    assert f.spools.open_binding(spool) is None

    _record(11, "04DDEEFF")
    second = await ntag.confirm(f, client, spool, actor="ann")

    assert (second.bound, second.tag_uid) == (True, "04DDEEFF")


@respx.mock
async def test_confirm_with_no_write_queued_binds_nothing(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    _record(11, UID)

    result = await ntag.confirm(f, client, spool, actor="ann")

    assert result.bound is False
    assert f.spools.open_binding(spool) is None


@respx.mock
async def test_confirm_with_another_origin_binds_nothing(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    f.spools.bind_tag(spool, tag_uid=OLD, kind="other", actor="ann")
    await _queue(f, client, spool, None)
    _record(11, UID, "nfc_link")

    foreign = await ntag.confirm(f, client, spool, actor="ann")

    assert foreign.bound is False
    binding = f.spools.open_binding(spool)
    assert binding is not None
    tag = f.spools.tag(binding.tag_id)
    assert tag is not None
    assert tag.tag_uid == OLD


@respx.mock
async def test_cancel_posts_to_the_device(pg_pool: PgPool, client: BambuddyClient) -> None:
    route = respx.post(f"{API}/spoolbuddy/devices/sb-1/cancel-write").mock(
        return_value=httpx.Response(200, json={"status": "cancelled"})
    )

    f = _filament(pg_pool)
    await ntag.cancel(f, client, _spool(f, pg_pool, 11), "sb-1")

    assert route.call_count == 1


@respx.mock
async def test_cancel_keeps_a_spools_row_queued_on_another_device(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    await _queue(f, client, spool, None)
    respx.post(f"{API}/spoolbuddy/devices/sb-2/cancel-write").mock(
        return_value=httpx.Response(200, json={"status": "cancelled"})
    )

    await ntag.cancel(f, client, spool, "sb-2")

    assert ntag._baseline(f, spool)[0] is True


@respx.mock
async def test_cancel_drops_the_spools_row_that_names_no_device(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    with f.spools.transaction() as conn:
        conn.execute(
            "INSERT INTO filament_ntag_writes (spool_id, device_id, baseline_uid)"
            " VALUES (%s, NULL, NULL)",
            (spool,),
        )
    respx.post(f"{API}/spoolbuddy/devices/sb-2/cancel-write").mock(
        return_value=httpx.Response(200, json={"status": "cancelled"})
    )

    await ntag.cancel(f, client, spool, "sb-2")

    assert ntag._baseline(f, spool)[0] is False


@respx.mock
async def test_a_failed_device_cancel_keeps_the_spools_row(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    await _queue(f, client, spool, None)
    respx.post(f"{API}/spoolbuddy/devices/sb-1/cancel-write").mock(
        return_value=httpx.Response(500, json={"detail": "down"})
    )

    with pytest.raises(ApiError):
        await ntag.cancel(f, client, spool, "sb-1")

    assert ntag._baseline(f, spool)[0] is True


@respx.mock
async def test_a_device_cancel_clears_every_write_queued_on_that_device(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    first = _spool(f, pg_pool, 11)
    second = _spool(f, pg_pool, 12)
    other = _spool(f, pg_pool, 13)
    respx.post(f"{API}/spoolbuddy/nfc/write-tag").mock(
        return_value=httpx.Response(200, json={"status": "queued", "warnings": []})
    )
    for bb_id, spool, device in ((11, first, "sb-1"), (12, second, "sb-1"), (13, other, "sb-2")):
        _record(bb_id, None)
        await ntag.write(f, client, spool, device, replace=True)
    respx.post(f"{API}/spoolbuddy/devices/sb-1/cancel-write").mock(
        return_value=httpx.Response(200, json={"status": "cancelled"})
    )

    await ntag.cancel(f, client, first, "sb-1")

    assert ntag._baseline(f, first)[0] is False
    assert ntag._baseline(f, second)[0] is False
    assert ntag._baseline(f, other)[0] is True


@respx.mock
async def test_a_cancelled_write_cannot_be_confirmed(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    await _queue(f, client, spool, None)
    respx.post(f"{API}/spoolbuddy/devices/sb-1/cancel-write").mock(
        return_value=httpx.Response(200, json={"status": "cancelled"})
    )

    await ntag.cancel(f, client, spool, "sb-1")
    _record(11, UID)
    result = await ntag.confirm(f, client, spool, actor="ann")

    assert result.bound is False
    assert f.spools.open_binding(spool) is None


@respx.mock
async def test_a_matched_frame_takes_its_kind_from_the_spool_that_holds_the_keys(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    holder = _spool(f, pg_pool, 11)
    f.spools.bind_tag(holder, tag_uid=UID, kind="ntag_openspool", actor="ann")
    route = respx.get(f"{API}/inventory/spools/11")
    matched = _scan(tag_type=None, bb_spool_id=11)

    result = await ntag.scan(f, _Scanner(matched, client), timeout=1, device_id=None)  # type: ignore[arg-type]

    assert result.kind == "ntag_openspool"
    assert route.call_count == 0


@respx.mock
async def test_a_matched_frame_with_no_binding_asks_bambuddy_for_the_origin(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    _spool(f, pg_pool, 11)
    respx.get(f"{API}/inventory/spools/11").mock(
        return_value=httpx.Response(
            200, json={"id": 11, "material": "PLA", "tag_uid": UID, "data_origin": "opentag3d"}
        )
    )

    scanner = cast(BambuddyClient, _Scanner(_scan(tag_type=None, bb_spool_id=11), client))
    result = await ntag.scan(f, scanner, timeout=1, device_id=None)

    assert result.kind == "ntag_opentag"


@respx.mock
async def test_a_matched_frame_of_another_origin_is_other(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    _spool(f, pg_pool, 11)
    respx.get(f"{API}/inventory/spools/11").mock(
        return_value=httpx.Response(
            200, json={"id": 11, "material": "PLA", "tag_uid": UID, "data_origin": "nfc_link"}
        )
    )

    scanner = cast(BambuddyClient, _Scanner(_scan(tag_type=None, bb_spool_id=11), client))
    result = await ntag.scan(f, scanner, timeout=1, device_id=None)

    assert result.kind == "other"
