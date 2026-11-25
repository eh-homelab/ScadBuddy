"""Who may answer a flow's question at its two routes (#7471, spec 2026-11-25 §4, §8): a
person always; an assistant session or an MCP token on the agent's credential only when
the setting `flow_answerers` names it and the question is not for people only."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.models import principal_for
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.core.settings import Settings
from tests.api.test_auth_enforce import HEADER, _app, _configure, _sign_in
from tests.api.test_flows import register, script, until
from tests.auth.fake_oidc import CLIENT_ID, ISSUER, FakeIssuer
from tests.flows.flows_support import parent_session
from tests.support.agent_tables import AGENT_KEY_MIGRATIONS

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

PUBLIC = "https://scadbuddy.test"
NOT_ALLOWED = "/flow-answer-not-allowed"
#: A session of the browser user, one of an MCP token, and that token.
MINE = "5e551011-0000-4000-8000-00000000000a"
TOKENS = "5e551011-0000-4000-8000-00000000000b"
TOKEN = "70ce0000-0000-4000-8000-00000000000c"
SCHEMA = {"type": "object", "properties": {"size": {"type": "string"}}}


@pytest.fixture(params=["off", "observe"])
def settings(
    request: pytest.FixtureRequest, settings: Settings, tmp_path: Path, pg_conninfo: str
) -> Settings:
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(b"\x07" * 32).decode())
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        for sql in AGENT_KEY_MIGRATIONS:
            conn.execute(sql.read_text().encode())
        parent_session(conn, MINE, "browser")
        parent_session(conn, TOKENS, f"token:{TOKEN}", tier="write")
    return settings.model_copy(
        update={"secret_key_file": key, "public_url": PUBLIC, "auth_mode": request.param}
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=PUBLIC) as test_client:
        yield test_client


@pytest.fixture
def credential(app: FastAPI, client: TestClient) -> str:
    """The agent's service credential."""
    pool = getattr(app.state, STATE_ATTR).settings_store.pool
    return ServiceCredentialStore(pool).issue("agent", "bootstrap", rate_limited=False).token


def as_agent(
    credential: str | None, session: str | None = MINE, principal: str = "browser", **more: str
) -> dict[str, str]:
    headers = {"X-ScadBuddy-Agent-Author": principal, **more}
    if session is not None:
        headers["X-ScadBuddy-Agent-Author-Session"] = session
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    return headers


def as_token(credential: str) -> dict[str, str]:
    return as_agent(credential, None, f"token:{TOKEN}")


def answerers(client: TestClient, *rules: str) -> None:
    response = client.put("/api/v1/settings", json={"flow_answerers": list(rules)})
    assert response.status_code == 200, response.text


def waiting(client: TestClient, body: str, **start_headers: str) -> tuple[str, dict[str, Any]]:
    """A run parked on its one question, and the question's entry."""
    definition = register(client, script(body, "return 'done'"))
    started = client.post(
        f"/api/v1/workflows/{definition['id']}/runs",
        headers={"Idempotency-Key": "w", **start_headers},
    )
    assert started.status_code in (200, 201, 202), started.text
    run_id = started.json()["id"]
    view = until(client, run_id, lambda v: v["status"] == "waiting" and v["live"] and v["pending"])
    [entry] = view["pending"]
    return run_id, entry


def text_question(client: TestClient, **start_headers: str) -> tuple[str, dict[str, Any]]:
    return waiting(client, "await wait_for_human('Swap?')", **start_headers)


def answer(
    client: TestClient, run_id: str, entry: dict[str, Any], headers: dict[str, str], key: str = "a"
) -> Any:
    """The route the entry takes: a text, or its task's object."""
    idem = {"Idempotency-Key": key, **headers}
    if entry["task"] is not None:
        return client.post(
            f"/api/v1/tasks/{entry['call_id']}/answer", json={"answer": {"size": "S"}}, headers=idem
        )
    return client.post(
        f"/api/v1/workflow-runs/{run_id}/answer",
        json={"call_id": entry["call_id"], "answer": "yes"},
        headers=idem,
    )


def refused(response: Any) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["type"].endswith(NOT_ALLOWED)


def answered(response: Any) -> None:
    assert response.status_code in (200, 202), response.text


def decision(app: FastAPI, run_id: str) -> dict[str, Any] | None:
    pool = getattr(app.state, STATE_ATTR).settings_store.pool
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT responder, admitted_by FROM workflow_run_decisions WHERE run_id = %s",
            (run_id,),
        ).fetchone()
    return None if row is None else dict(row)


QUESTIONS = pytest.mark.parametrize(
    "body",
    ["await wait_for_human('Swap?')", f"await prompt('Size?', {SCHEMA!r})"],
    ids=["text", "task"],
)


@QUESTIONS
def test_with_the_default_only_a_person_answers(
    app: FastAPI, client: TestClient, credential: str, body: str
) -> None:
    run_id, entry = waiting(client, body)
    for headers in (as_agent(credential), as_token(credential), as_agent(None)):
        refused(answer(client, run_id, entry, headers))
    assert decision(app, run_id) is None
    answered(answer(client, run_id, entry, {}))
    assert decision(app, run_id) == {"responder": "browser", "admitted_by": None}


@QUESTIONS
def test_sessions_admits_a_browser_users_session(
    app: FastAPI, client: TestClient, credential: str, body: str
) -> None:
    answerers(client, "sessions")
    run_id, entry = waiting(client, body)
    answered(answer(client, run_id, entry, as_agent(credential)))
    assert decision(app, run_id) == {"responder": f"session:{MINE}", "admitted_by": "setting"}
    done = until(client, run_id, lambda v: v["status"] == "succeeded")
    assert done["run"]["result"] == "result: 'done'"
    if entry["task"] is not None:
        task = client.get(f"/api/v1/tasks/{entry['call_id']}").json()
        assert task["answered_by"] == f"session:{MINE}"


def test_each_rule_admits_its_answerer_and_no_other(
    app: FastAPI, client: TestClient, credential: str
) -> None:
    mine, tokens, token = (
        as_agent(credential),
        as_agent(credential, TOKENS, f"token:{TOKEN}"),
        (as_token(credential)),
    )
    cases = {
        ("sessions",): (mine,),
        (f"token:{TOKEN}",): (tokens, token),
    }
    for n, (rules, admits) in enumerate(cases.items()):
        answerers(client, *rules)
        for m, headers in enumerate((mine, tokens, token)):
            run_id, entry = text_question(client)
            response = answer(client, run_id, entry, headers, key=f"k{n}{m}")
            if headers in admits:
                answered(response)
            else:
                refused(response)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"X-ScadBuddy-Flow-Run": "r1"}, id="a run's own call"),
        pytest.param({"credential": "none"}, id="forged author"),
        pytest.param({"credential": "projects"}, id="projects credential"),
        pytest.param({"session": "none", "principal": "browser"}, id="no session or token"),
    ],
)
def test_these_never_answer_with_every_rule_set(
    app: FastAPI, client: TestClient, credential: str, headers: dict[str, str]
) -> None:
    answerers(client, "starter", "sessions", f"token:{TOKEN}")
    run_id, entry = text_question(client)
    options = dict(headers)
    which = options.pop("credential", "agent")
    session = None if options.pop("session", MINE) == "none" else MINE
    principal = options.pop("principal", "browser")
    if which == "projects":
        pool = getattr(app.state, STATE_ATTR).settings_store.pool
        token: str | None = (
            ServiceCredentialStore(pool).issue("projects", "bootstrap", rate_limited=False).token
        )
    else:
        token = None if which == "none" else credential
    refused(answer(client, run_id, entry, as_agent(token, session, principal, **options)))
    assert decision(app, run_id) is None


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE ai_sessions SET archived_at = now() WHERE id = %s",
        "UPDATE ai_sessions SET background_tools = false WHERE id = %s",
        "UPDATE ai_sessions SET turn_flow = true WHERE id = %s",
        "UPDATE ai_sessions SET owner_id = 'someone-else' WHERE id = %s",
    ],
    ids=["archived", "a flow's session", "a flow's turn", "handed to another owner"],
)
def test_a_session_that_may_not_answer_is_refused(
    app: FastAPI, client: TestClient, credential: str, change: str
) -> None:
    answerers(client, "starter", "sessions")
    run_id, entry = text_question(client)
    pool = getattr(app.state, STATE_ATTR).settings_store.pool
    with pool.connection() as conn:
        conn.execute(change, (MINE,))
    refused(answer(client, run_id, entry, as_agent(credential)))


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE ai_mcp_tokens SET revoked_at = now() WHERE id = %s",
        "UPDATE ai_mcp_tokens SET expires_at = now() WHERE id = %s",
        "UPDATE ai_mcp_tokens SET tier = 'read' WHERE id = %s",
    ],
    ids=["revoked", "expired", "read only"],
)
def test_a_token_that_may_not_answer_is_refused(
    app: FastAPI, client: TestClient, credential: str, change: str
) -> None:
    answerers(client, f"token:{TOKEN}")
    run_id, entry = text_question(client)
    pool = getattr(app.state, STATE_ATTR).settings_store.pool
    with pool.connection() as conn:
        conn.execute(change, (TOKEN,))
    refused(answer(client, run_id, entry, as_token(credential)))
    refused(answer(client, run_id, entry, as_agent(credential, TOKENS, f"token:{TOKEN}"), "b"))


def test_starter_admits_only_the_session_that_started_the_run(
    app: FastAPI, client: TestClient, credential: str
) -> None:
    answerers(client, "starter")
    # Started from the browser: nobody is its starter session.
    run_id, entry = text_question(client)
    refused(answer(client, run_id, entry, as_agent(credential)))
    # Started by the token's session: that session, and no other.
    started = as_agent(credential, TOKENS, f"token:{TOKEN}")
    run_id, entry = text_question(client, **started)
    refused(answer(client, run_id, entry, as_agent(credential)))
    refused(answer(client, run_id, entry, as_token(credential), "b"))
    answered(answer(client, run_id, entry, started, "c"))


@QUESTIONS
def test_a_question_for_people_only_wins_over_the_setting(
    app: FastAPI, client: TestClient, credential: str, body: str
) -> None:
    answerers(client, "starter", "sessions", f"token:{TOKEN}")
    run_id, entry = waiting(client, body.replace(")", ", people_only=True)"))
    assert entry["people_only"] is True
    response = answer(client, run_id, entry, as_agent(credential))
    refused(response)
    assert response.json()["people_only"] is True
    answered(answer(client, run_id, entry, {}, "b"))


def test_a_repeat_answers_its_first_answer_after_the_question_is_gone(
    app: FastAPI, client: TestClient, credential: str
) -> None:
    answerers(client, "sessions")
    run_id, entry = text_question(client)
    first = answer(client, run_id, entry, as_agent(credential))
    answered(first)
    until(client, run_id, lambda v: v["status"] == "succeeded")
    answerers(client)
    again = answer(client, run_id, entry, as_agent(credential))
    assert (again.status_code, again.json()) == (first.status_code, first.json())
    # Another request is a new answer, which the setting refuses now.
    refused(answer(client, run_id, entry, as_agent(credential), "b"))


def test_in_enforce_the_policy_decides_on_the_agents_credential(
    settings: Settings, pg_conninfo: str
) -> None:
    """`enforce` lets the agent's credential reach the routes, which are `api` routes now;
    the setting decides, as in the other modes. A forged author is refused before."""
    if settings.auth_mode != "off":
        pytest.skip("one run is enough")
    issuer = FakeIssuer()
    oidc = settings.model_copy(
        update={"auth_oidc_issuer": ISSUER, "auth_oidc_client_id": CLIENT_ID}
    )
    _configure(oidc, issuer)
    app = _app(oidc, "enforce", issuer.transport())
    with TestClient(app, base_url=PUBLIC, headers=HEADER) as client:
        _sign_in(client)
        alice = principal_for(ISSUER, "alice")
        with psycopg.connect(pg_conninfo, autocommit=True) as conn:
            parent_session(conn, MINE, alice, kind="browser")
        pool = getattr(app.state, STATE_ATTR).settings_store.pool
        token = ServiceCredentialStore(pool).issue("agent", "bootstrap", rate_limited=False).token
        agent = as_agent(token, MINE, alice)
        run_id, entry = text_question(client)
        # The agent sends no cookie: a client of its own.
        bare = TestClient(app, base_url=PUBLIC, headers=HEADER)
        refused(answer(bare, run_id, entry, agent))
        forged = answer(client, run_id, entry, as_agent(None, MINE, alice), "b")
        assert forged.status_code in (400, 401, 403), forged.text
        answerers(client, "sessions")
        answered(answer(bare, run_id, entry, agent, "c"))
        assert decision(app, run_id) == {"responder": f"session:{MINE}", "admitted_by": "setting"}
