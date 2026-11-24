"""The projects worker's tasks routes (#7364): `RemoteTasks` answers as `TaskStore`
does, a write sent twice answers as it did the first time (D5), a refusal carries its
code, and no credential but the projects worker's gets in."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from fastapi import Depends, FastAPI

from scadbuddy.api import crud_tasks
from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import SESSION_COOKIE, AuthGate
from scadbuddy.auth.models import AuthSession, Service
from scadbuddy.auth.service_credentials import ServiceAuth
from scadbuddy.core.events import Event, TaskChanged
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.crud.client import CrudClient, CrudRefusedError
from scadbuddy.crud.tasks import RemoteTasks
from scadbuddy.tasks.component import TASKS
from scadbuddy.tasks.models import TaskOpen
from scadbuddy.tasks.store import RunEndedError, RunNotFoundError, TaskNotOpenError, TaskStore
from scadbuddy.worker import PROJECTS_CRUD_AREAS
from tests.conftest import PgPool
from tests.test_flow_store import NOW

pytestmark = pytest.mark.requires_postgres

TOKEN = "sbsvc_projects"
OTHER = "sbsvc_render"
BASE = "/api/v1/internal/crud/projects/tasks"
SESSION = "a-live-session"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
RUN = str(uuid.uuid4())
ENDED = str(uuid.uuid4())
T1, T2 = str(uuid.uuid4()), str(uuid.uuid4())


class _Credentials:
    def resolve(self, token: str) -> Service | None:
        if token == TOKEN:
            return Service("projects", frozenset({"crud.projects"}))
        if token == OTHER:
            return Service("render", frozenset({"crud.render"}))
        return None


class _Sessions:
    def resolve(self, token: str) -> AuthSession | None:
        if token != SESSION:
            return None
        return AuthSession(
            id_hash=b"h",
            principal="user:https://idp.test#alice",
            issuer="https://idp.test",
            subject="alice",
            name=None,
            email=None,
            kind="login",
            created_at=NOW,
            last_seen_at=NOW,
            idle_expires_at=NOW + timedelta(days=3650),
            absolute_expires_at=NOW + timedelta(days=3650),
        )


class _Source:
    def token(self) -> str | None:
        return TOKEN

    def invalidate(self, token: str) -> None: ...


class _Noted:
    def note(self, *args: object) -> None: ...


class _Events:
    def __init__(self) -> None:
        self.sent: list[Event] = []

    def publish_in(self, conn: Any, event: Event) -> None:
        self.sent.append(event)

    def changes(self) -> list[tuple[str, str]]:
        return [(e.task_id, e.state) for e in self.sent if isinstance(e, TaskChanged)]


@pytest.fixture
def events() -> _Events:
    return _Events()


@pytest.fixture
def store(pg_pool: PgPool, events: _Events) -> TaskStore:
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO workflow_definitions (id, name, version, created_by)"
            " VALUES ('def-1', 'tasks', 1, '{}')"
        )
        for run, status in ((RUN, "waiting"), (ENDED, "succeeded")):
            conn.execute(
                "INSERT INTO workflow_runs (id, definition_id, version, name, status,"
                " workflow_id, workflow_run_id, started_by)"
                " VALUES (%s, 'def-1', 1, 'tasks', %s, %s, 'x', '{}')",
                (run, status, f"flow-{run}"),
            )
    return TaskStore(pg_pool, events=events)


@pytest.fixture
def app(store: TaskStore) -> FastAPI:
    app = FastAPI(dependencies=[Depends(auth_check)])
    parts = {AUTH: SimpleNamespace(would_refuse=_Noted()), TASKS: SimpleNamespace(store=store)}
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode="enforce"),
        components=SimpleNamespace(get=lambda key: parts[key]),
    )
    app.include_router(crud_tasks.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: _Sessions(), credentials=lambda: _Credentials())
    return app


@pytest.fixture
async def http(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        yield client


@pytest.fixture
async def remote(app: FastAPI) -> AsyncIterator[RemoteTasks]:
    client = CrudClient(
        "http://api",
        "projects",
        ServiceAuth(_Source()),
        transport=httpx.ASGITransport(app=app),
        unavailable_budget=0.2,
    )
    try:
        yield RemoteTasks(client)
    finally:
        await client.aclose()


def _task(task_id: str, run_id: str = RUN) -> TaskOpen:
    return TaskOpen(id=task_id, run_id=run_id, title="Ask", schema=SCHEMA)


def _wire(task_id: str, run_id: str = RUN) -> dict[str, Any]:
    return _task(task_id, run_id).model_dump(mode="json", by_alias=True)


def _routes() -> list[tuple[str, dict[str, Any]]]:
    return [
        (BASE, _wire(T1)),
        (f"{BASE}/{T1}/close", {"state": "expired"}),
        (f"{BASE}/{T1}/answered", {"answer": None, "by": None}),
    ]


def test_the_projects_areas_include_tasks() -> None:
    assert "tasks" in CRUD_AREAS["projects"]
    assert "tasks" in PROJECTS_CRUD_AREAS


async def test_every_route_without_a_credential_or_with_a_person_is_401(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        for path, body in _routes():
            nobody = await client.post(path, json=body)
            person = await client.post(
                path, json=body, headers={"Cookie": f"{SESSION_COOKIE}={SESSION}"}
            )
            assert nobody.status_code == person.status_code == 401, path
            assert nobody.headers["X-ScadBuddy-Crud"] == "1"


async def test_every_route_with_another_callers_credential_is_403(
    app: FastAPI, store: TaskStore
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api",
        headers={"Authorization": f"Bearer {OTHER}"},
    ) as client:
        for path, body in _routes():
            refused = await client.post(path, json=body)
            assert refused.status_code == 403, path
            assert refused.json()["reason"] == "wrong_caller"
    assert store.list(state=None) == []


async def test_open_twice_answers_the_same_row(
    remote: RemoteTasks, store: TaskStore, events: _Events
) -> None:
    first = await remote.open(_task(T1))
    again = await remote.open(_task(T1).model_copy(update={"title": "Other"}))
    assert first == again == store.get(T1)
    assert first.state == "open" and first.title == "Ask"
    assert events.changes() == [(T1, "open")]


async def test_open_on_an_unknown_run_is_a_typed_404(
    remote: RemoteTasks, http: httpx.AsyncClient
) -> None:
    other = str(uuid.uuid4())
    with pytest.raises(RunNotFoundError):
        await remote.open(_task(T1, other))
    raw = await http.post(BASE, json=_wire(T1, other))
    assert raw.status_code == 404 and raw.json()["reason"] == "run-not-found"


async def test_open_on_an_ended_run_is_a_typed_409_and_opens_nothing(
    remote: RemoteTasks, http: httpx.AsyncClient, store: TaskStore
) -> None:
    with pytest.raises(RunEndedError):
        await remote.open(_task(T1, ENDED))
    raw = await http.post(BASE, json=_wire(T1, ENDED))
    assert raw.status_code == 409 and raw.json()["reason"] == "run-ended"
    assert store.list(state=None) == []


async def test_the_same_id_again_answers_the_stored_row_after_its_run_ended(
    remote: RemoteTasks, pg_pool: PgPool
) -> None:
    first = await remote.open(_task(T1))
    with pg_pool.connection() as conn:
        conn.execute("UPDATE workflow_runs SET status = 'failed' WHERE id = %s", (RUN,))
    assert await remote.open(_task(T1)) == first


async def test_close_of_an_answered_task_is_409_with_its_state(
    remote: RemoteTasks, http: httpx.AsyncClient, store: TaskStore
) -> None:
    await remote.open(_task(T1))
    await remote.mark_answered(T1, {"ok": True}, "alice")
    raw = await http.post(f"{BASE}/{T1}/close", json={"state": "cancelled"})
    assert raw.status_code == 409
    assert raw.json()["reason"] == "task-not-open" and raw.json()["state"] == "answered"
    assert await remote.close(T1, "cancelled") is None
    assert (task := store.get(T1)) is not None and task.state == "answered"


async def test_close_twice_answers_the_row_both_times(remote: RemoteTasks, events: _Events) -> None:
    await remote.open(_task(T1))
    first = await remote.close(T1, "expired")
    again = await remote.close(T1, "expired")
    assert first is not None and first.state == "expired"
    assert again == first
    # Another state is another request: the task already ended another way.
    assert await remote.close(T1, "cancelled") is None
    assert events.changes() == [(T1, "open"), (T1, "expired")]


async def test_close_of_a_missing_task_is_none_like_the_store(remote: RemoteTasks) -> None:
    assert await remote.close(T1, "expired") is None


async def test_the_same_answer_twice_answers_the_same_row(
    remote: RemoteTasks, events: _Events
) -> None:
    await remote.open(_task(T1))
    first = await remote.mark_answered(T1, {"ok": True}, "alice")
    again = await remote.mark_answered(T1, {"ok": True}, "alice")
    assert first == again and first.state == "answered" and first.answer == {"ok": True}
    assert events.changes() == [(T1, "open"), (T1, "answered")]


async def test_another_answer_or_a_closed_task_raises_with_its_state(
    remote: RemoteTasks,
) -> None:
    await remote.open(_task(T1))
    await remote.open(_task(T2))
    await remote.mark_answered(T1, {"ok": True}, None)
    with pytest.raises(TaskNotOpenError) as another:
        await remote.mark_answered(T1, {"ok": False}, None)
    assert another.value.state == "answered"
    await remote.close(T2, "cancelled")
    with pytest.raises(TaskNotOpenError) as closed:
        await remote.mark_answered(T2, {"ok": True}, None)
    assert closed.value.state == "cancelled"


async def test_answering_a_missing_task_raises_a_key_error(
    remote: RemoteTasks, http: httpx.AsyncClient
) -> None:
    with pytest.raises(KeyError):
        await remote.mark_answered(T1, {"ok": True}, None)
    raw = await http.post(f"{BASE}/{T1}/answered", json={"answer": None, "by": None})
    assert raw.status_code == 404 and raw.json()["reason"] == "task-not-found"


@pytest.mark.parametrize("bad", ["t1", str(uuid.uuid4()).upper(), f"{uuid.uuid4()}\n"])
async def test_an_id_that_is_not_a_uuid_is_422(
    http: httpx.AsyncClient, store: TaskStore, bad: str
) -> None:
    assert (await http.post(BASE, json=_wire(bad))).status_code == 422
    assert (await http.post(BASE, json=_wire(T1, bad))).status_code == 422
    shut = await http.post(f"{BASE}/{quote(bad)}/close", json={"state": "expired"})
    answered = await http.post(f"{BASE}/{quote(bad)}/answered", json={"answer": None, "by": None})
    assert shut.status_code == answered.status_code == 422
    assert store.list(state=None) == []


async def test_a_nul_never_reaches_postgres(http: httpx.AsyncClient, store: TaskStore) -> None:
    await http.post(BASE, json=_wire(T1))
    for body in (
        {"answer": {"x": "a\u0000b"}, "by": None},
        {"answer": {"a\u0000b": 1}, "by": None},
        {"answer": None, "by": "al\u0000ice"},
    ):
        raw = await http.post(f"{BASE}/{T1}/answered", content=json.dumps(body))
        assert raw.status_code == 422, body
    for field, value in (("title", "a\u0000"), ("place", "a\u0000"), ("ui", "\u0000")):
        raw = await http.post(BASE, json={**_wire(T2), field: value})
        assert raw.status_code == 422, field
    assert (task := store.get(T1)) is not None and task.state == "open"
    assert store.get(T2) is None


async def test_a_refusal_never_quotes_an_answer_or_a_context_and_the_log_holds_none(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    answer, context, schema = "ans-s3cret", "ctx-s3cret", "schema-s3cret"
    sent = [
        await http.post(
            BASE, json={**_wire(T1), "title": "", "context": [{"type": "text", "text": context}]}
        ),
        await http.post(BASE, json={**_wire(T1), "schema": {"type": "object", "x-note": schema}}),
        await http.post(
            f"{BASE}/{T1}/answered", json={"answer": {"a": answer}, "by": f"{answer}\u0000"}
        ),
    ]
    assert {r.status_code for r in sent} == {422}
    ok = await http.post(
        BASE,
        json={
            **_wire(T2),
            "schema": {"type": "object", "properties": {"a": {"type": "string", "title": schema}}},
            "context": [{"type": "text", "text": context}],
        },
    )
    assert ok.status_code == 200
    sent += [
        await http.post(f"{BASE}/{T2}/answered", json={"answer": {"a": answer}, "by": None}),
        await http.post(f"{BASE}/{T2}/answered", json={"answer": {"a": "other"}, "by": None}),
    ]
    assert sent[-1].status_code == 409
    for response in sent[:3] + sent[-1:]:
        assert answer not in response.text and context not in response.text
    for text in (caplog.text,):
        assert answer not in text and context not in text and schema not in text


async def test_a_big_context_or_answer_or_name_is_422(
    http: httpx.AsyncClient, store: TaskStore
) -> None:
    big = [{"type": "text", "text": "x" * 17_000}]
    assert (await http.post(BASE, json={**_wire(T1), "context": big})).status_code == 422
    assert (await http.post(BASE, json={**_wire(T1), "ui": "x" * 201})).status_code == 422
    assert store.get(T1) is None
    await http.post(BASE, json=_wire(T1))
    huge = {"answer": {"a": "x" * 17_000}, "by": None}
    assert (await http.post(f"{BASE}/{T1}/answered", json=huge)).status_code == 422
    long_by = {"answer": None, "by": "x" * 201}
    assert (await http.post(f"{BASE}/{T1}/answered", json=long_by)).status_code == 422
    fine = {"answer": {"a": "x" * 200}, "by": "x" * 200, "newer": 1}
    assert (await http.post(f"{BASE}/{T1}/answered", json=fine)).status_code == 200
    assert (await http.post(BASE, json={**_wire(T2), "newer": 1})).status_code == 200


async def test_a_refusal_it_does_not_know_is_raised(remote: RemoteTasks) -> None:
    with pytest.raises(CrudRefusedError) as refused:
        await remote.open(_task("not-a-uuid"))
    assert refused.value.status == 422
