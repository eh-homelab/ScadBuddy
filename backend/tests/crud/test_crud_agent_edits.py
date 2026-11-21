"""An edit of a session through its CRUD route (#6449, #5299 step 9, `api/crud_agent_edits.py`):
only the ``agent`` credential reaches it, it is the agent's ``SessionEdits.update`` transaction
(`agent_data/edits.py`) with its events, a done needs the workflow told, and every refusal writes
nothing: a turn holding the row, anything parked on the person, another owner, a session gone.
The agent's tables are made from its own migration files (#5299 D6)."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import psycopg_pool
import pytest
from fastapi.testclient import TestClient

from scadbuddy.agent_data import edits
from scadbuddy.api import crud_agent_edits, crud_agent_turns
from scadbuddy.core.problems import ApiError
from tests.conftest import PgPool
from tests.crud.test_crud_agent_turns import BASE, Agent, _app, _row, _session, _sql, _token

pytestmark = pytest.mark.requires_postgres

BROWSER = {"kind": "browser", "id": "browser"}


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    from scadbuddy.render.agent_migrations import migrate_agent

    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


@pytest.fixture
def agent(tables: PgPool) -> Iterator[Agent]:
    app = _app(tables, crud_agent_edits.router)
    with TestClient(app) as client:
        yield Agent(client, _token(app))


def _events(pool: PgPool, sid: str) -> list[dict[str, Any]]:
    rows = _sql(
        pool, "SELECT event FROM ai_session_events WHERE session_id = %s ORDER BY seq", (sid,)
    )
    return [json.loads(r["event"]) for r in rows]


def _summary(pool: PgPool, sid: str, *, done: bool = True) -> str:
    """A posted ``done`` summary, or (``done=False``) a question waiting on the person."""
    qid = str(uuid.uuid4())
    if done:
        _sql(
            pool,
            "INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions,"
            " kind, attention_reason, summary) VALUES (%s, %s, gen_random_uuid(), 'attn',"
            " 'toolu_done', '[]', 'attention', 'done', 's')",
            (qid, sid),
        )
    else:
        _sql(
            pool,
            "INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions,"
            " expires_at) VALUES (%s, %s, gen_random_uuid(), 'AskUserQuestion', 'toolu_q', '[]',"
            " now() + interval '10 minutes')",
            (qid, sid),
        )
    return qid


def _edit(agent: Agent, sid: str, **body: Any) -> Any:
    return agent.status("POST", f"/sessions/{sid}/edit", {"principal": BROWSER, **body})


def test_only_the_agent_credential_reaches_the_route(tables: PgPool) -> None:
    app = _app(tables, crud_agent_edits.router)
    sid = _session(tables)
    body = {"principal": BROWSER, "title": "x"}
    with TestClient(app) as client:
        assert client.post(f"{BASE}/sessions/{sid}/edit", json=body).status_code == 401
        for caller in ("render", "print", "projects"):
            wrong = client.post(
                f"{BASE}/sessions/{sid}/edit", json=body, headers=_token(app, caller)
            )
            assert wrong.status_code == 403
        listed = client.get(f"{BASE}/areas", headers=_token(app)).json()["areas"]
        assert "session-edits" in listed
    assert _row(tables, sid)["title"] != "x"


def test_a_rename_writes_the_title_and_no_event(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    answer = _edit(agent, sid, title="new é")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"logged": [], "dismissed": []}
    assert _row(tables, sid)["title"] == "new é"


def test_a_done_needs_its_workflow_told(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    untold = _edit(agent, sid, done=True)
    assert (untold.status_code, untold.json()["reason"]) == (409, "untold")
    assert _row(tables, sid)["status"] == "idle"
    with tables.connection() as conn:
        conn.execute("LISTEN scadbuddy_warm_evict")
        told = _edit(agent, sid, done=True, told=True)
        assert told.status_code == 200
        notices = [n.payload for n in conn.notifies(timeout=1, stop_after=1)]
    assert notices == [sid]
    (batch,) = told.json()["logged"]
    assert batch["events"] == [
        {"v": 1, "type": "session.status", "sessionId": sid, "status": "done"}
    ]
    assert _row(tables, sid)["status"] == "done"
    # Done already: no event, nothing to tell.
    again = _edit(agent, sid, done=True)
    assert again.status_code == 200 and again.json()["logged"] == []


def test_an_archive_dismisses_the_done_summaries_with_their_events(
    agent: Agent, tables: PgPool
) -> None:
    sid = _session(tables)
    qid = _summary(tables, sid)
    answer = _edit(agent, sid, archived=True)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert [d["id"] for d in body["dismissed"]] == [qid]
    assert [e["type"] for e in body["logged"][0]["events"]] == [
        "question.resolved",
        "input.resolved",
    ]
    assert body["logged"][0]["events"][1] == {
        "v": 1,
        "type": "input.resolved",
        "sessionId": sid,
        "id": f"question:{qid}",
        "kind": "answer",
        "outcome": "cancelled",
        "reason": edits.ARCHIVED_REASON,
    }
    assert _row(tables, sid)["archived_at"] is not None
    assert [
        e["seq"]
        for e in _sql(
            tables, "SELECT seq FROM ai_session_events WHERE session_id = %s ORDER BY seq", (sid,)
        )
    ] == [1, 2]
    unarchived = _edit(agent, sid, archived=False)
    assert unarchived.status_code == 200
    assert _row(tables, sid)["archived_at"] is None


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        ("running", {"done": True, "told": True}, "busy_done"),
        ("waiting_input", {"archived": True}, "busy_archive"),
    ],
)
def test_a_turn_holding_the_row_refuses_done_and_archive(
    agent: Agent, tables: PgPool, status: str, body: dict[str, Any], reason: str
) -> None:
    sid = _session(tables, status=status, turn=str(uuid.uuid4()))
    refused = _edit(agent, sid, title="t", **body)
    assert (refused.status_code, refused.json()["reason"]) == (409, reason)
    row = _row(tables, sid)
    assert (row["status"], row["title"], row["archived_at"]) == (status, "", None)


def test_anything_parked_refuses_an_archive(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    _summary(tables, sid, done=False)
    refused = _edit(agent, sid, archived=True)
    assert (refused.status_code, refused.json()["reason"]) == (409, "parked")
    assert _row(tables, sid)["archived_at"] is None
    assert _events(tables, sid) == []


def test_another_owner_or_no_session_is_refused(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables, owner_kind="bearer", owner_id="a")
    refused = agent.status(
        "POST", f"/sessions/{sid}/edit", {"principal": {"kind": "bearer", "id": "b"}, "title": "x"}
    )
    assert (refused.status_code, refused.json()["reason"]) == (409, "changed_owner")
    gone = _edit(agent, str(uuid.uuid4()), title="x")
    assert (gone.status_code, gone.json()["reason"]) == (404, "gone")


def test_the_body_and_the_id_are_bounded(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    path = f"/sessions/{sid}/edit"
    for bad in (
        {"principal": BROWSER, "title": ""},
        {"principal": BROWSER, "title": "x" * (crud_agent_edits.TITLE_MAX + 1)},
        {"principal": {"kind": "nobody", "id": "a"}},
        {"principal": BROWSER, "extra": 1},
        {"title": "x"},
    ):
        assert agent.status("POST", path, bad).status_code == 422, bad
    huge = agent.client.post(
        f"{BASE}{path}",
        content=b'{"x":"' + b"y" * crud_agent_edits.BODY_BYTES_MAX + b'"}',
        headers={**agent.headers, "content-type": "application/json"},
    )
    assert huge.status_code == 413
    for bad_id in ("not-a-uuid", "..", "%2E%2E", sid.upper(), f"{sid}%2F..%2F"):
        refused = _edit(agent, bad_id, title="x")
        assert refused.status_code in (404, 422)
    assert _row(tables, sid)["title"] == ""


def test_an_edit_waits_for_a_claim_holding_the_row(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    result: dict[str, Any] = {}

    def run() -> None:
        answer = _edit(agent, sid, done=True, told=True)
        result.update(status=answer.status_code, reason=answer.json().get("reason"))

    with tables.connection() as conn, conn.transaction():
        conn.execute("SELECT 1 FROM ai_sessions WHERE id = %s FOR UPDATE", (sid,))
        thread = threading.Thread(target=run)
        thread.start()
        thread.join(0.5)
        assert thread.is_alive()
        conn.execute(
            "UPDATE ai_sessions SET status = 'running', durable_turn_id = gen_random_uuid()"
            " WHERE id = %s",
            (sid,),
        )
    thread.join(10)
    # The edit read the row once the claim committed: a turn holds it, so it is not marked done.
    assert result == {"status": 409, "reason": "busy_done"}
    assert _row(tables, sid)["status"] == "running"


def test_an_edit_that_waits_past_the_pools_bound_is_refused() -> None:
    class Busy:
        def connection(self, timeout: float) -> Any:
            assert timeout == crud_agent_turns.POOL_WAIT_S
            raise psycopg_pool.PoolTimeout("couldn't get a connection")

    state = SimpleNamespace(settings_store=SimpleNamespace(pool=Busy()))
    body = crud_agent_edits.Edit.model_validate({"principal": BROWSER, "title": "x"})
    with pytest.raises(ApiError) as refused:
        asyncio.run(crud_agent_edits.edit(str(uuid.uuid4()), body, state))  # type: ignore[arg-type]
    assert (refused.value.status, refused.value.extensions["reason"]) == (503, "pool_busy")
