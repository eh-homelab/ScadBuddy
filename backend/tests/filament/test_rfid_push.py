"""The push of tag bindings to Bambuddy (#6264 Task 4): `filament/rfid_push.py`. Bambuddy
is respx; every route and body is the one the recordings name
(`tests/bambuddy/recordings/openapi/rfid-routes.json`)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament.bambuddy_pull import pull
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.component import Filament
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.models import Material, SpoolCreate, TagKind
from scadbuddy.filament.presets import PoolSpoolPresets
from scadbuddy.filament.rfid_push import drain_tags, error_label, push_pending
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.library.settings_store import StoredSettings
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"
TRAY = "0123456789ABCDEF0123456789ABCDEF"
UID = "AABBCCDD11223344"
UID2 = "0102030405060708"


def _filament(pool: PgPool) -> Filament:
    return Filament(
        catalog=CatalogStore(pool),
        types=SpoolTypeStore(pool),
        spools=FilamentSpoolStore(pool),
        locations=LocationStore(pool),
        inventory=FilamentInventory(pool),
        presets=PoolSpoolPresets(pool),
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


@respx.mock
async def test_a_rebind_across_kinds_never_clears_the_new_tag(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    _bind(f, spool, uid=UID)
    _bind(f, spool, tray=TRAY)
    assert len(_pending(pg_pool)) == 2
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


async def test_the_pull_after_a_push_changes_nothing(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, pg_pool, 11)
    _bind(f, spool, uid=UID)
    before = f.spools.open_binding(spool)
    assert before is not None

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
