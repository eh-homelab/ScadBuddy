"""The push of tag bindings to Bambuddy (#6264 Task 4): `filament/rfid_push.py`. Bambuddy
is respx; every route and body is the one the recordings name
(`tests/bambuddy/recordings/openapi/rfid-routes.json`)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx
import psycopg
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import rfid_push
from scadbuddy.filament.bambuddy_pull import pull
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.component import Filament
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.invoices import InvoiceStore
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.metrics_store import MetricsSettings, MetricsStore
from scadbuddy.filament.models import Material, SpoolCreate, TagKind, TagPushReport
from scadbuddy.filament.presets import PoolSpoolPresets
from scadbuddy.filament.rfid_mode import RfidModeStore
from scadbuddy.filament.rfid_push import PUSH_LOCK, drain_tags, error_label, push_pending
from scadbuddy.filament.sensors import SensorStore
from scadbuddy.filament.shopping import ShoppingStore
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.component import Printers
from scadbuddy.printers.lan_command_models import LanCommandRun
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from scadbuddy.spools.activities import filament_rfid_activity
from scadbuddy.workflows.lan_command import LanCommandAnswer
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"
TRAY = "0123456789ABCDEF0123456789ABCDEF"
UID = "AABBCCDD11223344"
UID2 = "0102030405060708"


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
    config = BambuddyConfig(base_url=BASE, api_key="s3cret")
    async with BambuddyClient(config) as bambuddy:
        yield bambuddy


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


def _bind(f: Filament, spool: UUID, *, uid: str | None = None, tray: str | None = None) -> None:
    kind: TagKind = "bambu_mifare" if tray else "ntag_opentag"
    f.spools.bind_tag(spool, tag_uid=uid, tray_uuid=tray, kind=kind, actor="ann")


def _printers(pool: PgPool) -> Printers:
    """What the activity reads of the registry: its store and its reports."""
    return cast(
        Printers,
        SimpleNamespace(store=PrinterStore(pool, None), reports=PrinterReportStore(pool)),
    )


class _NoLan:
    """A `LanCommandSender` no test here reaches: no printer here is linked over the LAN,
    so the slot step sends nothing (`test_rfid_slots.py` covers it)."""

    async def run(
        self, printer_id: str, run: LanCommandRun, *, key: str | None = None
    ) -> LanCommandAnswer:
        raise AssertionError("no LAN command is sent here")


def _rfid_activity(
    printers: Printers,
    filament: Filament,
    load: Callable[[], StoredSettings],
    sync: Callable[[], FilamentSync],
) -> Callable[[], Any]:
    """`filament_rfid_activity` with no LAN link and no slicer profiles."""
    return filament_rfid_activity(printers, filament, load, sync, _NoLan(), lambda: None)


def _pending(pool: PgPool) -> list[dict[str, object]]:
    with pool.connection() as conn:
        return list(conn.execute("SELECT * FROM filament_bb_tag_pending"))


def _bb(bb_id: int, **over: object) -> dict[str, object]:
    return {"id": bb_id, "material": "PLA", **over}


def _calls() -> list[tuple[str, str, object]]:
    return [
        (c.request.method, c.request.url.path, json.loads(c.request.content or "null"))
        for c in respx.calls
    ]


def _by_tag(found: dict[str, object] | None) -> respx.Route:
    if found is None:
        return respx.get(f"{API}/inventory/spools/by-tag").mock(
            return_value=httpx.Response(404, json={"detail": "no spool"})
        )
    return respx.get(f"{API}/inventory/spools/by-tag").mock(
        return_value=httpx.Response(200, json=found)
    )


@respx.mock
async def test_a_bound_tag_is_linked_once(pg_pool: PgPool, client: BambuddyClient) -> None:
    f = _filament(pg_pool)
    _bind(f, _spool(f, pg_pool, 11), uid=UID)
    by_tag = _by_tag(None)
    link = respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        return_value=httpx.Response(200, json=_bb(11, tag_uid=UID))
    )

    report = await drain_tags(f, client)

    assert (report.pushed, report.failed) == (1, 0)
    assert link.call_count == 1
    assert json.loads(link.calls.last.request.content) == {
        "tag_uid": UID,
        "tray_uuid": None,
        "tag_type": "ntag",
        "data_origin": "nfc_link",
    }
    assert _pending(pg_pool) == []

    by_tag.mock(return_value=httpx.Response(200, json=_bb(11, tag_uid=UID)))
    again = await drain_tags(f, client)
    assert again.model_dump() == {
        "pushed": 0,
        "moved": 0,
        "cleared": 0,
        "unchanged": 0,
        "failed": 0,
    }
    assert link.call_count == 1


@respx.mock
async def test_the_link_sends_the_spools_origin(pg_pool: PgPool, client: BambuddyClient) -> None:
    """#7149 R-7149-1: the spool's kept origin, not a fixed ``nfc_link``."""
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    _bind(f, spool, uid=UID)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET data_origin = 'opentag3d' WHERE id = %s", (spool,))
    _by_tag(None)
    link = respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        return_value=httpx.Response(200, json=_bb(11, tag_uid=UID))
    )

    await drain_tags(f, client)

    assert json.loads(link.calls.last.request.content)["data_origin"] == "opentag3d"


@respx.mock
async def test_a_409_moves_the_tag(pg_pool: PgPool, client: BambuddyClient) -> None:
    f = _filament(pg_pool)
    _bind(f, _spool(f, pg_pool, 11), tray=TRAY)
    _by_tag(_bb(7, tray_uuid=TRAY))
    conflict = {
        "detail": {
            "code": "tag_already_linked",
            "message": "taken",
            "spool_id": 7,
            "field": "tray_uuid",
        }
    }
    link = respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        side_effect=[
            httpx.Response(409, json=conflict),
            httpx.Response(200, json=_bb(11, tray_uuid=TRAY)),
        ]
    )
    clear = respx.patch(f"{API}/inventory/spools/7").mock(
        return_value=httpx.Response(200, json=_bb(7))
    )

    report = await drain_tags(f, client)

    assert (report.moved, report.pushed, report.failed) == (1, 0, 0)
    assert [(m, p) for m, p, _ in _calls()] == [
        ("GET", "/api/v1/inventory/spools/by-tag"),
        ("PATCH", "/api/v1/inventory/spools/11/link-tag"),
        ("PATCH", "/api/v1/inventory/spools/7"),
        ("PATCH", "/api/v1/inventory/spools/11/link-tag"),
    ]
    assert json.loads(clear.calls.last.request.content) == {"tag_uid": None, "tray_uuid": None}
    assert link.call_count == 2
    assert _pending(pg_pool) == []


@respx.mock
async def test_a_failed_push_keeps_the_row(pg_pool: PgPool, client: BambuddyClient) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    _bind(f, spool, uid=UID)
    _by_tag(None)
    link = respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        side_effect=httpx.ConnectError("no route to host 10.0.0.9")
    )

    report = await drain_tags(f, client)

    assert (report.failed, report.pushed) == (1, 0)
    (row,) = _pending(pg_pool)
    assert row["attempts"] == 1
    assert row["last_attempt_at"] is not None
    assert row["last_error"] == UNAVAILABLE_PROBLEM
    assert "10.0.0.9" not in str(row["last_error"])
    assert f.spools.open_binding(spool) is not None

    link.mock(return_value=httpx.Response(200, json=_bb(11, tag_uid=UID)))
    again = await drain_tags(f, client)

    assert again.pushed == 1
    assert _pending(pg_pool) == []
    links = [c for c in respx.calls if c.request.url.path.endswith("/link-tag")]
    assert {c.request.url.path for c in links} == {"/api/v1/inventory/spools/11/link-tag"}


def test_the_label_is_the_type_and_the_status() -> None:
    from scadbuddy.bambuddy.errors import CONFLICT_PROBLEM
    from scadbuddy.core.problems import ApiError

    assert error_label(ApiError(502, "x", type_=UNAVAILABLE_PROBLEM)) == UNAVAILABLE_PROBLEM
    labelled = ApiError(409, "x", type_=CONFLICT_PROBLEM, bambuddy_status=409)
    assert error_label(labelled) == f"{CONFLICT_PROBLEM}:409"


@respx.mock
async def test_an_unbind_clears_only_a_known_holder(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    _bind(f, spool, uid=UID)
    binding = f.spools.open_binding(spool)
    assert binding is not None
    await drain_tags(f, client)  # the bind itself, no Bambuddy call matters here
    respx.reset()
    f.spools.unbind_tag(binding.tag_id, actor="ann")
    _by_tag(_bb(11, tag_uid=UID))
    clear = respx.patch(f"{API}/inventory/spools/11").mock(
        return_value=httpx.Response(200, json=_bb(11))
    )

    report = await drain_tags(f, client)

    assert (report.cleared, report.failed) == (1, 0)
    assert clear.call_count == 1
    assert _pending(pg_pool) == []

    # The holder 99 is no SB spool: it is left alone.
    other = _spool(f, pg_pool)
    _bind(f, other, uid=UID2)
    second = f.spools.open_binding(other)
    assert second is not None
    f.spools.unbind_tag(second.tag_id, actor="ann")
    respx.reset()
    _by_tag(_bb(99, tag_uid=UID2))
    stray = respx.patch(f"{API}/inventory/spools/99").mock(return_value=httpx.Response(200))

    left = await drain_tags(f, client)

    assert (left.unchanged, left.cleared) == (1, 0)
    assert stray.call_count == 0
    assert _pending(pg_pool) == []


@pytest.mark.parametrize("order", ["old_first", "new_first", "tie"])
@respx.mock
async def test_a_rebind_across_kinds_never_clears_the_new_tag(
    pg_pool: PgPool, client: BambuddyClient, order: str
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    _bind(f, spool, uid=UID)
    _bind(f, spool, tray=TRAY)
    assert len(_pending(pg_pool)) == 2
    # The closed tag and the new one of one `bind_on` share ``queued_at`` (Plan F4); force
    # that tie, and each order the drain could take them in.
    skew = {"old_first": "-1 second", "new_first": "1 second", "tie": "0 seconds"}[order]
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_bb_tag_pending SET queued_at = '2026-11-17T12:00:00Z'")
        conn.execute(
            "UPDATE filament_bb_tag_pending p SET queued_at = queued_at + %s::interval"
            " FROM filament_tags t WHERE t.id = p.tag_id AND t.tag_uid IS NOT NULL",
            (skew,),
        )
    by_tag = respx.get(f"{API}/inventory/spools/by-tag").mock(
        side_effect=lambda request: (
            httpx.Response(200, json=_bb(11, tag_uid=UID))
            if request.url.params.get("tag_uid") == UID
            else httpx.Response(404, json={"detail": "none"})
        )
    )
    link = respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        return_value=httpx.Response(200, json=_bb(11, tray_uuid=TRAY))
    )
    clear = respx.patch(f"{API}/inventory/spools/11").mock(return_value=httpx.Response(200))

    report = await drain_tags(f, client)

    assert (report.pushed, report.unchanged, report.cleared, report.failed) == (1, 1, 0, 0)
    assert by_tag.call_count == 2
    assert link.call_count == 1
    assert clear.call_count == 0
    assert _pending(pg_pool) == []


@respx.mock
async def test_a_spool_with_no_bambuddy_link_pushes_nothing(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    _bind(f, _spool(f, pg_pool), uid=UID)
    _by_tag(None)

    report = await drain_tags(f, client)

    assert report.unchanged == 1
    assert [m for m, _, _ in _calls()] == ["GET"]
    assert _pending(pg_pool) == []


def _settings() -> StoredSettings:
    return StoredSettings(bambuddy_url=BASE, bambuddy_api_key="s3cret")


@respx.mock
async def test_the_drain_is_off_unless_tags_is_sb_to_bb(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _bind(f, _spool(f, pg_pool, 11), uid=UID)

    off = await push_pending(f, _settings, lambda: FilamentSync(tags="off"))

    assert off.model_dump() == {"pushed": 0, "moved": 0, "cleared": 0, "unchanged": 0, "failed": 0}
    assert len(respx.calls) == 0
    assert len(_pending(pg_pool)) == 1

    _by_tag(None)
    respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        return_value=httpx.Response(200, json=_bb(11, tag_uid=UID))
    )
    on = await push_pending(f, _settings, lambda: FilamentSync(tags="sb_to_bb"))
    assert on.pushed == 1
    assert _pending(pg_pool) == []


@respx.mock
async def test_tags_two_way_pushes(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _bind(f, _spool(f, pg_pool, 11), uid=UID)
    _by_tag(None)
    respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        return_value=httpx.Response(200, json=_bb(11, tag_uid=UID))
    )

    both = await push_pending(f, _settings, lambda: FilamentSync(tags="two_way"))

    assert both.pushed == 1
    assert _pending(pg_pool) == []


@respx.mock
async def test_tags_bb_to_sb_pushes_nothing(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _bind(f, _spool(f, pg_pool, 11), uid=UID)

    pulled = await push_pending(f, _settings, lambda: FilamentSync(tags="bb_to_sb"))

    assert pulled == TagPushReport()
    assert len(respx.calls) == 0
    assert len(_pending(pg_pool)) == 1


@respx.mock
async def test_a_second_drain_touches_nothing_while_one_runs(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _bind(f, _spool(f, pg_pool, 11), uid=UID)
    with psycopg.connect(f.spools.conninfo, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (PUSH_LOCK,))

        held = await push_pending(f, _settings, lambda: FilamentSync(tags="sb_to_bb"))

        assert held == TagPushReport()
        assert len(respx.calls) == 0
        assert len(_pending(pg_pool)) == 1
    _by_tag(None)
    respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        return_value=httpx.Response(200, json=_bb(11, tag_uid=UID))
    )

    free = await push_pending(f, _settings, lambda: FilamentSync(tags="sb_to_bb"))

    assert free.pushed == 1


@respx.mock
async def test_the_activity_answers_failed_and_never_raises(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _bind(f, _spool(f, pg_pool, 11), uid=UID)
    on = lambda: FilamentSync(tags="sb_to_bb")  # noqa: E731
    run = _rfid_activity(_printers(pg_pool), f, _settings, on)

    respx.get(f"{API}/inventory/spools/by-tag").mock(side_effect=httpx.ConnectError("down"))
    assert await run() == "failed"  # a tag failed: the report counts it

    def broken() -> FilamentSync:
        raise RuntimeError("settings are gone")

    assert await _rfid_activity(_printers(pg_pool), f, _settings, broken)() == "failed"

    respx.get(f"{API}/inventory/spools/by-tag").mock(return_value=httpx.Response(404, json={}))
    respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        return_value=httpx.Response(200, json=_bb(11, tag_uid=UID))
    )
    assert await run() == "done"


@respx.mock
async def test_a_failing_slot_step_fails_the_activity_and_stops_no_other_step(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    _bind(f, _spool(f, pg_pool, 11), uid=UID)
    _by_tag(None)
    link = respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        return_value=httpx.Response(200, json=_bb(11, tag_uid=UID))
    )
    on = lambda: FilamentSync(tags="sb_to_bb")  # noqa: E731
    broken = cast(Printers, SimpleNamespace())  # no store: the slot step raises

    assert await _rfid_activity(broken, f, _settings, on)() == "failed"
    assert link.call_count == 1  # the tags step ran before it


@respx.mock
async def test_a_failure_that_is_not_bambuddys_is_labelled_internal(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    _bind(f, _spool(f, pg_pool, 11), uid=UID)
    _by_tag(None)
    respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        side_effect=RuntimeError("password=hunter2")
    )

    report = await drain_tags(f, client)

    assert report.failed == 1
    (row,) = _pending(pg_pool)
    assert row["last_error"] == "internal"


@respx.mock
async def test_a_failure_does_not_count_against_a_requeued_tag(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    _bind(f, _spool(f, pg_pool, 11), uid=UID)
    _by_tag(None)

    def requeue_then_fail(request: httpx.Request) -> httpx.Response:
        with pg_pool.connection() as conn:
            conn.execute("UPDATE filament_bb_tag_pending SET queued_at = now() + interval '1 s'")
        raise httpx.ConnectError("down")

    respx.patch(f"{API}/inventory/spools/11/link-tag").mock(side_effect=requeue_then_fail)

    await drain_tags(f, client)

    (row,) = _pending(pg_pool)
    assert (row["attempts"], row["last_error"]) == (0, None)


@respx.mock
async def test_the_pull_after_a_push_changes_nothing(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    _bind(f, spool, uid=UID)
    before = f.spools.open_binding(spool)
    assert before is not None
    _by_tag(None)
    respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        return_value=httpx.Response(200, json=_bb(11, tag_uid=UID))
    )
    assert (await drain_tags(f, client)).pushed == 1

    rows = [BbSpool.model_validate(_bb(11, tag_uid=UID, updated_at="2026-11-17T10:00:00"))]
    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    # The first pull of a linked spool records BB's snapshot; the tag is not part of what
    # it changes, so the binding survives it.
    pull(f, rows, now=now)
    held = f.spools.open_binding(spool)
    assert held is not None and held.id == before.id

    report = pull(f, rows, now=now)

    assert report.updated == 0
    after = f.spools.open_binding(spool)
    assert after is not None and after.id == before.id


@respx.mock
async def test_a_rebind_while_bambuddy_is_down_leaves_one_holder(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    a, b = _spool(f, pg_pool, 11), _spool(f, pg_pool, 12)
    holders: dict[str, int] = {}

    def by_tag(request: httpx.Request) -> httpx.Response:
        held = holders.get(request.url.params["tag_uid"])
        return (
            httpx.Response(200, json=_bb(held, tag_uid=UID))
            if held
            else httpx.Response(404, json={"detail": "none"})
        )

    def link(request: httpx.Request) -> httpx.Response:
        target = int(request.url.path.split("/")[-2])
        held = holders.get(UID)
        if held is not None and held != target:
            detail = {
                "code": "tag_already_linked",
                "message": "taken",
                "spool_id": held,
                "field": "tag_uid",
            }
            return httpx.Response(409, json={"detail": detail})
        holders[UID] = target
        return httpx.Response(200, json=_bb(target, tag_uid=UID))

    def clear(request: httpx.Request) -> httpx.Response:
        holders.clear()
        return httpx.Response(200, json=_bb(11))

    by_tag_route = respx.get(f"{API}/inventory/spools/by-tag").mock(side_effect=by_tag)
    respx.patch(url__regex=rf"{API}/inventory/spools/\d+/link-tag").mock(side_effect=link)
    respx.patch(f"{API}/inventory/spools/11").mock(side_effect=clear)
    _bind(f, a, uid=UID)
    assert (await drain_tags(f, client)).pushed == 1
    assert holders == {UID: 11}

    # T moves to B while Bambuddy is down: the push fails and the row stays.
    _bind(f, b, uid=UID)
    by_tag_route.mock(side_effect=httpx.ConnectError("down"))
    assert (await drain_tags(f, client)).failed == 1
    assert holders == {UID: 11}
    assert len(_pending(pg_pool)) == 1

    # Bambuddy is up again: B's BB spool ends as the one holder; the old one is cleared.
    by_tag_route.mock(side_effect=by_tag)
    report = await drain_tags(f, client)
    assert (report.moved, report.failed) == (1, 0)
    assert holders == {UID: 12}
    assert _pending(pg_pool) == []


async def test_a_lock_attempt_cancelled_mid_thread_does_not_keep_the_lock(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _filament(pg_pool)
    name = "scadbuddy:test:cancelled-lock"
    kept: list[object] = []  # no garbage collection hides a leaked connection
    real = rfid_push.try_lock_on

    def slow(conninfo: str, lock: str) -> psycopg.Connection[Any] | None:
        time.sleep(0.4)
        got = real(conninfo, lock)
        kept.append(got)
        return got

    monkeypatch.setattr(rfid_push, "try_lock_on", slow)
    task = asyncio.ensure_future(rfid_push.acquire_lock(f.spools.conninfo, name))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.8)

    monkeypatch.setattr(rfid_push, "try_lock_on", real)
    again = rfid_push.try_lock_on(f.spools.conninfo, name)
    assert again is not None
    again.close()


@respx.mock
async def test_a_row_that_keeps_failing_does_not_starve_a_fresh_one(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    _bind(f, _spool(f, pg_pool, 11), uid=UID)
    _bind(f, _spool(f, pg_pool, 12), uid=UID2)
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_bb_tag_pending p SET attempts = 5,"
            " queued_at = queued_at - '1 hour'::interval"
            " FROM filament_tags t WHERE t.id = p.tag_id AND t.tag_uid = %s",
            (UID,),
        )
    _by_tag(None)
    link = respx.patch(f"{API}/inventory/spools/12/link-tag").mock(
        return_value=httpx.Response(200, json=_bb(12, tag_uid=UID2))
    )

    report = await drain_tags(f, client, limit=1)

    assert (report.pushed, report.failed) == (1, 0)
    assert link.call_count == 1
