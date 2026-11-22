"""The Bambuddy pull's CRUD routes (#6772): the print worker sends what it read of
Bambuddy through them, only with the print credential, and the API applies it with no
Bambuddy call. `RemoteBbPull` and `LocalBbPull` give one answer. Invented ids only."""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.api.limits import MAX_TEXT_BODY_BYTES
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.bambuddy.models import Archive, SpoolUsage
from scadbuddy.core.cancel import CancelledError, CancelToken
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudClient, CrudRefusedError
from scadbuddy.crud.listener import crud_listener_port
from scadbuddy.crud.print import BASE, RemoteBbPull
from scadbuddy.filament import bambuddy_usage
from scadbuddy.filament.bambuddy_pull import ACTOR, Pulls
from scadbuddy.filament.bb_pull_apply import LocalBbPull, forget_actor, remember_actor
from scadbuddy.filament.bb_pull_models import (
    ColoursBody,
    HaBody,
    PullBody,
    PullInstanceChangedError,
    TraysBody,
    UsageBody,
)
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore
from scadbuddy.main import create_app
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
PULL_ID = f"filament-bb-pull-{'b' * 32}"
PULL = f"{BASE}/filament/pull"
PATHS = (PULL, f"{PULL}/usage", f"{PULL}/ha-states", f"{PULL}/trays", f"{PULL}/colours")
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
        "pull_id": PULL_ID,
        "pulls": Pulls(),
        "spools": [SPOOL],
        "catalog": [],
    } | over
    return PullBody.model_validate(fields)


def test_the_print_areas_list_filament_pull() -> None:
    """The routes are an area of their own, appended after the older ones, so a new worker waits
    for an API that serves them (`wait_for_areas`) rather than sending its reads into a 404."""
    assert "filament-pull" in PRINT_CRUD_AREAS
    assert CRUD_AREAS["print"].index("filament-pull") > CRUD_AREAS["print"].index("filament-sync")
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
def test_a_body_past_the_gate_is_413_on_its_headers(settings: Settings) -> None:
    """Decision 178, revised by Decision 182: the routes have no cap in a dependency. The
    CRUD listener serves the same app, whose `BodySizeGate` refuses an ``application/json``
    body past the routes' `RouteLimit`, `MAX_TEXT_BODY_BYTES` (8 MiB), on its Content-Length,
    before any route reads it: the request sends its headers only. Through the real app,
    with the print worker's real credential."""
    app = create_app(settings.model_copy(update={"crud_port": 0}))
    with TestClient(app):
        store = getattr(app.state, STATE_ATTR).settings_store
        issued = ServiceCredentialStore(store.pool).issue("print", "bootstrap", rate_limited=False)
        assert issued.token is not None
        headers = {"Authorization": f"Bearer {issued.token}", "Content-Type": "application/json"}
        port = crud_listener_port(app)
        assert port is not None
        for path in PATHS:
            status, problem = _post_headers_only(port, path, headers, MAX_TEXT_BODY_BYTES + 2)
            assert status == 413, (path, problem)
            assert "a Bambuddy pull is at most 8 MB" in problem["detail"], path
        assert filament_on(store.pool).spools.list() == []


@pg
def test_a_body_with_no_content_type_is_413_too(settings: Settings) -> None:
    """#6861: the gate counts only a type in its table and FastAPI reads a body with no
    Content-Type as JSON, so the four routes carry their own `RouteLimit` (Decision 182):
    a length past 8 MiB with the header left out is 413 on each, on its Content-Length."""
    app = create_app(settings.model_copy(update={"crud_port": 0}))
    with TestClient(app):
        store = getattr(app.state, STATE_ATTR).settings_store
        issued = ServiceCredentialStore(store.pool).issue("print", "bootstrap", rate_limited=False)
        assert issued.token is not None
        port = crud_listener_port(app)
        assert port is not None
        headers = {"Authorization": f"Bearer {issued.token}"}  # no Content-Type
        for path in PATHS:
            status, problem = _post_headers_only(port, path, headers, MAX_TEXT_BODY_BYTES + 2)
            assert status == 413, (path, problem)
            assert "a Bambuddy pull is at most 8 MB" in problem["detail"], path
        assert filament_on(store.pool).spools.list() == []


def _post_headers_only(
    port: int, path: str, headers: dict[str, str], length: int
) -> tuple[int, dict[str, Any]]:
    """POST ``path`` to the CRUD listener with its headers only: ``Content-Length`` declares
    ``length`` bytes and none are sent. The gate judges a declared length on the headers
    alone, so it answers before any body; a client still writing the 8 MiB body when the
    server answers and closes raced it (httpx.WriteTimeout in CI, #6883)."""
    head = [f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", f"Content-Length: {length}"]
    head += [f"{name}: {value}" for name, value in headers.items()]
    with socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
        sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
        response = http.client.HTTPResponse(sock)
        response.begin()
        return response.status, json.loads(response.read())


@pg
@pytest.mark.parametrize(
    ("path", "body"),
    [
        (PULL, {"instance": "not-hex"}),
        (PULL, {"extra": 1}),
        # The worker never names the actor (Decision 181).
        (PULL, {"actor": "someone-else"}),
        (f"{PULL}/usage", {"actor": "someone-else"}),
        (PULL, {"pull_id": "not-a-run"}),
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
        f"{PULL}/usage": UsageBody(instance=INSTANCE, pull_id=PULL_ID, usage=[]).model_dump(
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
                f"{PULL}/usage": {"instance": INSTANCE, "pull_id": PULL_ID, "usage": []},
                f"{PULL}/ha-states": {"instance": INSTANCE, "states": None},
                f"{PULL}/trays": {"instance": INSTANCE, "assignments": []},
                f"{PULL}/colours": {"instance": INSTANCE, "colours": []},
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
    body = UsageBody(instance=INSTANCE, pull_id=PULL_ID, usage=usage, archives=[archive])
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
async def test_a_cancel_between_the_bookings_statements_books_nothing(
    store: SettingsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6910 review: Postgres ignores a cancel that reaches the backend between
    statements, so a cancel that lands after the booking's last statement is caught by
    the check before the commit, and nothing is booked. Sent again, the rows book once."""
    local = _local(store)
    await local.apply(_body())
    ended = datetime.now(UTC) + timedelta(minutes=1)
    usage = [_usage(created_at=ended.replace(tzinfo=None).isoformat())]
    archive = Archive(id=40, printer_id=1, completed_at=datetime(2026, 11, 14, 10, tzinfo=UTC))
    body = UsageBody(instance=INSTANCE, pull_id=PULL_ID, usage=usage, archives=[archive])
    token = CancelToken()
    real = bambuddy_usage.book_usage

    def then_cancel(*args: Any, **kwargs: Any) -> Any:
        booked = real(*args, **kwargs)
        token.cancel()
        return booked

    monkeypatch.setattr(bambuddy_usage, "book_usage", then_cancel)
    with pytest.raises(CancelledError):
        await local.book_usage(body, cancel=token)
    monkeypatch.setattr(bambuddy_usage, "book_usage", real)
    assert (await local.book_usage(body)).booked == 1


@pg
async def test_the_ha_and_trays_routes_answer_as_in_process(
    store: SettingsStore, crud: CrudClient
) -> None:
    """No sensor and no tray to set right: both answer as the in-process apply does."""
    for api in (RemoteBbPull(crud), _local(store)):
        await api.ha_states(HaBody(instance=INSTANCE, states={"sensor.a": "18"}))
        await api.ha_states(HaBody(instance=INSTANCE, states=None))
        assert await api.reconcile_trays(TraysBody(instance=INSTANCE, assignments=[])) == 0


COLOUR = {
    "id": 9,
    "manufacturer": "Bambu Lab",
    "color_name": "Jade White",
    "hex_color": "#ffffff",
    "material": "PLA Basic",
}


@pg
async def test_the_colours_route_fills_the_catalog_as_in_process(
    store: SettingsStore, crud: CrudClient
) -> None:
    """#6907: Bambuddy's colour catalog through the route, linked by its id; the same
    entries again change nothing, and a bad colour is skipped."""
    body = ColoursBody.model_validate(
        {"instance": INSTANCE, "colours": [COLOUR, COLOUR | {"id": 10, "hex_color": "bad"}]}
    )
    applied = await RemoteBbPull(crud).colours(body)
    assert (applied.added, applied.skipped) == (1, 1)
    again = await _local(store).colours(body)
    assert (again.added, again.unchanged) == (0, 1)
    with store.pool.connection() as conn:
        row = conn.execute("SELECT bb_colour_id, rgba FROM filament_colours").fetchone()
    assert row == {"bb_colour_id": 9, "rgba": "#FFFFFFFF"}


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


@pg
async def test_the_events_name_the_actor_the_api_kept_never_the_workers(
    store: SettingsStore, crud: CrudClient
) -> None:
    """Decision 181: the route names the person the API kept under the run's id (an
    import), and ``bambuddy`` for a run with none; the body carries no actor at all."""
    filament = filament_on(store.pool)
    remember_actor(filament, PULL_ID, "person:ann")
    kept = await RemoteBbPull(crud).apply(_body(spools=[SPOOL | {"id": 11}]))
    other = await RemoteBbPull(crud).apply(
        _body(pull_id=f"filament-bb-pull-{'d' * 32}", spools=[SPOOL | {"id": 12}])
    )
    (made,) = kept.report.created_ids
    (scheduled,) = other.report.created_ids
    assert {e.actor for e in filament.spools.events(made)} == {"person:ann"}
    assert {e.actor for e in filament.spools.events(scheduled)} == {ACTOR}
    forget_actor(filament, PULL_ID)
    again = await RemoteBbPull(crud).apply(_body(spools=[SPOOL | {"id": 13}]))
    (late,) = again.report.created_ids
    assert {e.actor for e in filament.spools.events(late)} == {ACTOR}
