"""Whose author headers stand (#4975 phase 5, spec 2026-11-01 §7.3), without a database:
the rule, the run's token and what the projects worker's host calls send."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from scadbuddy.auth.authorship_rule import AuthorshipRule, starter_matches
from scadbuddy.auth.gate import CALLER_SCOPE_KEY
from scadbuddy.auth.models import ANONYMOUS, AuthSession, Caller, Person, Service
from scadbuddy.auth.run_tokens import mint_run_token, run_of_token
from scadbuddy.core.authorship import (
    AUTHOR_REFUSAL_KEY,
    AgentAuthor,
    AgentAuthorship,
    browser_principal,
    by_a_person,
    current_author,
    requester_record,
)
from scadbuddy.workflows.flow_routes import route_author_of

KEY = os.urandom(32)
RUN = "7d1c1a0e-8a1b-4c55-9b7e-3f2f3f2f3f2f"
PERSON = "user:https://idp.test#alice"


def _session(kind: Any = "login") -> AuthSession:
    now = datetime.now(UTC)
    return AuthSession(
        id_hash=b"x",
        principal=PERSON,
        issuer="https://idp.test",
        subject="alice",
        name="Alice",
        email=None,
        kind=kind,
        created_at=now,
        last_seen_at=now,
        idle_expires_at=now + timedelta(days=1),
        absolute_expires_at=now + timedelta(days=2),
    )


class _Rule(AuthorshipRule):
    def __init__(self, mode: str, runs: dict[str, dict[str, Any]]) -> None:
        super().__init__(mode=lambda: mode, run_key=lambda: KEY, pool=lambda: None)  # type: ignore[arg-type, return-value]
        self.runs = runs
        self.reads = 0

    def _started_by(self, run_id: str) -> dict[str, Any] | None:
        self.reads += 1
        return self.runs.get(run_id)


class _Caller:
    """Stands in for ``AuthGate``: puts a fixed caller on the scope."""

    def __init__(self, app: ASGIApp, caller: Caller) -> None:
        self.app = app
        self.caller = caller

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope[CALLER_SCOPE_KEY] = self.caller
        await self.app(scope, receive, send)


async def _seen(request: Request) -> JSONResponse:
    author = current_author()
    return JSONResponse(
        {
            "author": None if author is None else author.principal,
            "browser": browser_principal(),
            "record": requester_record(),
            "person": by_a_person(),
            "refusal": request.scope.get(AUTHOR_REFUSAL_KEY),
        }
    )


def _client(caller: Caller, rule: AuthorshipRule) -> TestClient:
    app = Starlette(routes=[Route("/seen", _seen)])
    return TestClient(_Caller(AgentAuthorship(app, rule=rule), caller))


AUTHOR = {"X-ScadBuddy-Agent-Author": "token:abc"}
AGENT = Service("agent", frozenset({"api"}))
PROJECTS = Service("projects", frozenset({"api"}))
PRINT = Service("print", frozenset({"internal.outputs"}))


def test_a_person_is_the_session_in_every_mode() -> None:
    for mode in ("off", "observe"):
        body = _client(Person(_session()), _Rule(mode, {})).get("/seen").json()
        assert body["browser"] == PERSON
        assert body["record"] == {"kind": "browser", "principal": PERSON}
        assert body["person"] is True and body["author"] is None


def test_off_believes_every_header_and_counts_nothing() -> None:
    body = _client(Person(_session()), _Rule("off", {})).get("/seen", headers=AUTHOR).json()
    assert body["author"] == "token:abc" and body["refusal"] is None
    assert body["person"] is False


def test_signed_out_is_the_browser_user() -> None:
    body = _client(ANONYMOUS, _Rule("observe", {})).get("/seen").json()
    assert body["browser"] == "browser"
    assert body["record"] == {"kind": "browser"}
    assert body["person"] is True


@pytest.mark.parametrize(
    ("caller", "refusal"),
    [
        (Person(_session()), "author_header"),
        (PRINT, "author_header"),
        (ANONYMOUS, "author_header"),
        (AGENT, None),
        (PROJECTS, "wrong_author"),
    ],
)
def test_observe_names_each_refusal(caller: Caller, refusal: str | None) -> None:
    body = _client(caller, _Rule("observe", {})).get("/seen", headers=AUTHOR).json()
    assert body["refusal"] == refusal
    # Served, and believed, as before.
    assert body["author"] == "token:abc"


def test_a_service_or_delegated_session_is_no_person() -> None:
    for caller in (AGENT, PROJECTS, Person(_session("delegated"))):
        assert _client(caller, _Rule("observe", {})).get("/seen").json()["person"] is False


def test_the_projects_worker_names_only_the_run_s_starter() -> None:
    rule = _Rule("observe", {RUN: {"kind": "agent", "principal": "token:abc", "session": "s1"}})
    client = _client(PROJECTS, rule)
    token = {"X-ScadBuddy-Run-Token": mint_run_token(KEY, RUN)}
    own = {**AUTHOR, **token, "X-ScadBuddy-Agent-Author-Session": "s1"}
    assert client.get("/seen", headers=own).json()["refusal"] is None
    # Another session of the same principal, or another principal, is not the starter.
    other = {**own, "X-ScadBuddy-Agent-Author-Session": "s2"}
    assert client.get("/seen", headers=other).json()["refusal"] == "wrong_author"
    someone = {**own, "X-ScadBuddy-Agent-Author": "token:xyz"}
    assert client.get("/seen", headers=someone).json()["refusal"] == "wrong_author"
    # A token for another run, or under another key, names no run.
    forged = {**own, "X-ScadBuddy-Run-Token": mint_run_token(os.urandom(32), RUN)}
    reads = rule.reads
    assert client.get("/seen", headers=forged).json()["refusal"] == "wrong_author"
    assert rule.reads == reads


def test_enforce_refuses() -> None:
    assert (
        _client(Person(_session()), _Rule("enforce", {})).get("/seen", headers=AUTHOR).status_code
        == 400
    )
    assert _client(PROJECTS, _Rule("enforce", {})).get("/seen", headers=AUTHOR).status_code == 403
    assert _client(AGENT, _Rule("enforce", {})).get("/seen", headers=AUTHOR).status_code == 200


def test_starter_matches() -> None:
    person = AgentAuthor(PERSON, None)
    assert starter_matches({"kind": "browser", "principal": PERSON}, person)
    assert not starter_matches({"kind": "browser"}, person)
    assert not starter_matches({"kind": "browser", "principal": PERSON}, AgentAuthor(PERSON, "s"))
    assert not starter_matches(None, person)
    assert not starter_matches({"kind": "agent", "principal": PERSON, "session": "s"}, person)


def test_the_run_token() -> None:
    token = mint_run_token(KEY, RUN)
    assert run_of_token(KEY, token) == RUN
    assert run_of_token(os.urandom(32), token) is None
    assert run_of_token(KEY, "other-run" + token[len(RUN) :]) is None
    assert run_of_token(KEY, "no-dot") is None
    assert run_of_token(KEY, "ünïcode.AAAA") is None


def test_a_host_call_names_the_starter_only_with_a_token() -> None:
    agent = {"kind": "agent", "principal": "token:abc", "session": "s1"}
    author = route_author_of(agent, "t")
    assert author is not None
    assert author.headers() == {
        "X-ScadBuddy-Run-Token": "t",
        "X-ScadBuddy-Agent-Author": "token:abc",
        "X-ScadBuddy-Agent-Author-Session": "s1",
    }
    # A run from before the token, or one the anonymous browser user started, names none.
    assert route_author_of(agent, "") is None
    assert route_author_of({"kind": "browser"}, "t") is None
    person = route_author_of({"kind": "browser", "principal": PERSON, "session": "x"}, "t")
    assert person is not None and person.session is None
    assert route_author_of({"kind": "browser", "principal": "has space"}, "t") is None
