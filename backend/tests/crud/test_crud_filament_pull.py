"""The Bambuddy pull's CRUD routes (#6772): the print worker sends what it read of
Bambuddy through them, only with the print credential, and the API applies it with no
Bambuddy call. `RemoteBbPull` and `LocalBbPull` give one answer. Invented ids only."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.api import crud_print
from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.bambuddy.models import Archive, SpoolUsage
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudClient, CrudRefusedError
from scadbuddy.crud.print import BASE, RemoteBbPull
from scadbuddy.filament.bambuddy_pull import ACTOR, Pulls
from scadbuddy.filament.bb_pull_apply import LocalBbPull
from scadbuddy.filament.bb_pull_models import (
    HaBody,
    PullBody,
    PullInstanceChangedError,
    TraysBody,
    UsageBody,
)
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import (
    filament_on,
    print_crud_app,
    served_print_crud,
    threaded_print_crud,
)

pg = pytest.mark.requires_postgres

BB = "https://bambuddy.test"
INSTANCE = instance_for(BB)
PULL = f"{BASE}/filament/pull"
PATHS = (PULL, f"{PULL}/usage", f"{PULL}/ha-states", f"{PULL}/trays")
SPOOL = {
    "id": 7,
    "material": "PLA",
    "label_weight": 1000,
    "weight_used": 100.0,
    "updated_at": "2026-11-14T10:00:00",
}


def _usage(**over: Any) -> SpoolUsage:
    body: dict[str, Any] = {
        "id": 3,
        "spool_id": 7,
        "printer_id": 1,
        "print_name": "job",
        "weight_used": 12.5,
        "created_at": "2026-11-14T10:00:00",
    } | over
    return SpoolUsage.model_validate(body)


def _body(**over: Any) -> PullBody:
    fields: dict[str, Any] = {
        "instance": INSTANCE,
        "actor": ACTOR,
        "pulls": Pulls(),
        "spools": [SPOOL],
        "catalog": [],
    } | over
    return PullBody.model_validate(fields)


def test_the_print_areas_list_filament_pull() -> None:
    """The routes are an area of their own, appended last, so a new worker waits for an
    API that serves them (`wait_for_areas`) rather than sending its reads into a 404."""
    assert "filament-pull" in PRINT_CRUD_AREAS
    assert CRUD_AREAS["print"][-1] == "filament-pull"
    assert set(CRUD_AREAS["print"]) == PRINT_CRUD_AREAS


@pytest.fixture
def store(tmp_path: Path, pg_conninfo: str) -> Iterator[SettingsStore]:
    opened = SettingsStore(
        Settings(
            data_dir=tmp_path,
            database_url=pg_conninfo,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
            auth_mode="enforce",
        )
    )
    opened.open()
    opened.save(SettingsPatch(bambuddy_url=BB))
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
async def crud(store: SettingsStore) -> AsyncIterator[CrudClient]:
    async with served_print_crud(store) as client:
        yield client


def _local(store: SettingsStore) -> LocalBbPull:
    return LocalBbPull(filament_on(store.pool), store.load)


@pg
async def test_the_routes_need_the_print_credential(store: SettingsStore) -> None:
    """No credential is 401, another caller's is 403: nothing is pulled."""
    app = print_crud_app(store, ServiceCredentialStore(store.pool))
    body = _body().model_dump(mode="json")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as bare:
        for path in PATHS:
            assert (await bare.post(path, json=body)).status_code == 401, path
    with threaded_print_crud(store, caller="render") as other:
        for path in PATHS:
            with pytest.raises(CrudRefusedError) as wrong:
                other.request("POST", path, json=body)
            assert wrong.value.status == 403, path
            assert wrong.value.problem["reason"] == "wrong_caller", path
    assert filament_on(store.pool).spools.list() == []


@pg
async def test_a_body_past_the_cap_is_413_unread(
    store: SettingsStore, crud: CrudClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crud_print, "PULL_BODY_BYTES_MAX", 64)
    for path in PATHS:
        with pytest.raises(CrudRefusedError) as refused:
            await crud.request("POST", path, json=_body().model_dump(mode="json"))
        assert refused.value.status == 413, path
    assert filament_on(store.pool).spools.list() == []


async def _chunked() -> AsyncIterator[bytes]:
    yield b"{}"


@pg
async def test_a_body_with_no_length_is_411(store: SettingsStore) -> None:
    async with served_print_crud(store) as crud:
        for path in PATHS:
            with pytest.raises(CrudRefusedError) as refused:
                await crud.request(
                    "POST",
                    path,
                    content=_chunked(),  # type: ignore[arg-type]  # no Content-Length
                    headers={"Content-Type": "application/json"},
                )
            assert refused.value.status == 411, path


@pg
@pytest.mark.parametrize(
    ("path", "body"),
    [
        (PULL, {"instance": "not-hex"}),
        (PULL, {"extra": 1}),
        (PULL, {"actor": ""}),
        (PULL, {"spools": [SPOOL] * 20_001}),
        (f"{PULL}/usage", {"usage": [{"id": "x"}]}),
        (f"{PULL}/ha-states", {"states": {"a": 5}}),
        (f"{PULL}/trays", {"assignments": "none"}),
    ],
)
async def test_a_malformed_body_is_422(
    store: SettingsStore, crud: CrudClient, path: str, body: dict[str, Any]
) -> None:
    base: dict[str, Any] = {
        PULL: _body().model_dump(mode="json"),
        f"{PULL}/usage": UsageBody(instance=INSTANCE, actor=ACTOR, usage=[]).model_dump(
            mode="json"
        ),
        f"{PULL}/ha-states": {"instance": INSTANCE, "states": {}},
        f"{PULL}/trays": {"instance": INSTANCE, "assignments": []},
    }[path]
    with pytest.raises(CrudRefusedError) as refused:
        await crud.request("POST", path, json=base | body)
    assert refused.value.status == 422
    assert filament_on(store.pool).spools.list() == []


@pg
async def test_a_read_of_another_bambuddy_is_409_and_writes_nothing(
    store: SettingsStore, crud: CrudClient
) -> None:
    """The URL changed after the worker read: the route and the in-process apply refuse,
    and `RemoteBbPull` says so as the in-process one does."""
    store.save(SettingsPatch(bambuddy_url="https://new-bambuddy.test"))
    for path in PATHS:
        with pytest.raises(CrudRefusedError) as refused:
            body = {
                PULL: _body().model_dump(mode="json"),
                f"{PULL}/usage": {"instance": INSTANCE, "actor": ACTOR, "usage": []},
                f"{PULL}/ha-states": {"instance": INSTANCE, "states": None},
                f"{PULL}/trays": {"instance": INSTANCE, "assignments": []},
            }[path]
            await crud.request("POST", path, json=body)
        assert (refused.value.status, refused.value.problem["reason"]) == (
            409,
            "instance_changed",
        )
    for api in (RemoteBbPull(crud), _local(store)):
        with pytest.raises(PullInstanceChangedError):
            await api.apply(_body())
        with pytest.raises(PullInstanceChangedError):
            await api.reconcile_trays(TraysBody(instance=INSTANCE, assignments=[]))
    assert filament_on(store.pool).spools.list() == []


@pg
async def test_the_pull_route_pulls_and_answers_the_report(
    store: SettingsStore, crud: CrudClient
) -> None:
    """The rows as Bambuddy sent them: a good one makes an SB spool, a malformed one is
    counted ``invalid``; a catalog the worker could not read is ``catalog_failed``. The
    route calls no Bambuddy (respx sees no call to it)."""
    with respx.mock(assert_all_called=False) as router:
        router.route(host="127.0.0.1").pass_through()
        bambuddy = router.route(host="bambuddy.test")
        applied = await RemoteBbPull(crud).apply(
            _body(spools=[SPOOL, {"id": 8, "label_weight": "x"}], catalog=None)
        )
        assert not bambuddy.calls
    report = applied.report
    assert (report.created, report.invalid, report.invalid_bb_ids) == (1, 1, [8])
    assert report.catalog_failed is True
    assert applied.archives_since is None
    (spool,) = filament_on(store.pool).spools.list()
    assert spool.id == report.created_ids[0]
    events = filament_on(store.pool).spools.events(spool.id)
    assert {e.actor for e in events} == {ACTOR}
    with store.pool.connection() as conn:
        state = conn.execute("SELECT instance FROM filament_bb_sync_state").fetchone()
    assert state == {"instance": INSTANCE}
    again = await RemoteBbPull(crud).apply(_body())
    assert (again.report.created, again.report.linked, again.report.updated) == (0, 0, 0)


@pg
async def test_usage_rows_name_the_archives_date_then_book(
    store: SettingsStore, crud: CrudClient
) -> None:
    """A usage row of a linked spool that is not booked yet: the pull names the date the
    worker reads archives from (a day before the row), and the usage route books it once
    (natural: a resend books nothing)."""
    remote = RemoteBbPull(crud)
    await remote.apply(_body())
    # After the link's reading: a row from before it is in what was read.
    ended = datetime.now(UTC) + timedelta(minutes=1)
    usage = [_usage(created_at=ended.replace(tzinfo=None).isoformat())]
    applied = await remote.apply(_body(usage=usage))
    assert applied.archives_since == ended.date() - timedelta(days=1)
    archive = Archive(id=40, printer_id=1, completed_at=datetime(2026, 11, 14, 10, tzinfo=UTC))
    body = UsageBody(instance=INSTANCE, actor=ACTOR, usage=usage, archives=[archive])
    booked = await remote.book_usage(body)
    assert (booked.booked, booked.unlinked) == (1, 0)
    assert (await remote.book_usage(body)).booked == 0
    assert (await remote.apply(_body(usage=usage))).archives_since is None
    unlinked = await _local(store).book_usage(
        body.model_copy(
            update={"usage": [_usage(id=4, spool_id=99, created_at=usage[0].created_at)]}
        )
    )
    assert (unlinked.booked, unlinked.unlinked) == (0, 1)


@pg
async def test_the_ha_and_trays_routes_answer_as_in_process(
    store: SettingsStore, crud: CrudClient
) -> None:
    """No sensor and no tray to set right: both answer as the in-process apply does."""
    for api in (RemoteBbPull(crud), _local(store)):
        await api.ha_states(HaBody(instance=INSTANCE, states={"sensor.a": "18"}))
        await api.ha_states(HaBody(instance=INSTANCE, states=None))
        assert await api.reconcile_trays(TraysBody(instance=INSTANCE, assignments=[])) == 0


@pg
async def test_remote_and_local_give_the_same_report(
    store: SettingsStore, crud: CrudClient
) -> None:
    local = await _local(store).apply(_body(spools=[SPOOL | {"id": 9}]))
    remote = await RemoteBbPull(crud).apply(_body(spools=[SPOOL | {"id": 10}]))
    assert local.report.model_dump(exclude={"created_ids", "needs_type_ids"}) == (
        remote.report.model_dump(exclude={"created_ids", "needs_type_ids"})
    )
    assert len(local.report.created_ids) == len(remote.report.created_ids) == 1
