"""The shared vectors of the person check (#8038): `tests/fixtures/auth-vectors.json`.

Python's own functions make the vectors here: `AuthGate` resolves the caller, the
`SessionStore`, `PersonalTokenStore` and `ServiceCredentialStore` look the rows up,
`would_refuse` and `refusal` decide, and `auth_check` applies the mode. Go core's
`internal/auth` reads the same file (`tools/go/core/internal/auth/vectors_test.go`) and must
give the same answers.

The test fails when the file is not what Python makes. Set `UPDATE_AUTH_VECTORS=1` to write
it again. The tokens in the file are test values: no deployment holds them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from scadbuddy.auth import check
from scadbuddy.auth.check import auth_check, refusal
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import SESSION_COOKIE, AuthGate
from scadbuddy.auth.models import (
    ANONYMOUS,
    IDLE_TIMEOUT,
    AuthSession,
    Caller,
    Person,
    PersonalToken,
    Service,
)
from scadbuddy.auth.observe import WouldRefuse
from scadbuddy.auth.personal_tokens import PersonalTokenStore
from scadbuddy.auth.routes import would_refuse
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.auth.store import SessionStore
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.core.settings import Settings
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS, PgPool

pytestmark = pytest.mark.requires_postgres

FIXTURE = Path(__file__).parents[1] / "fixtures" / "auth-vectors.json"
UPDATE_ENV = "UPDATE_AUTH_VECTORS"
ISSUER = "https://idp.example"
DAY = 86400


def _session_token(label: str) -> str:
    """A session token: 43 characters of base64url, as `auth/tokens.py` makes."""
    return (label + "-" * 43)[:43]


def _bearer(prefix: str, number: int, label: str) -> str:
    """A `sbpat_` or `sbsvc_` token: 16 hex digits, a dot and 43 characters."""
    return f"{prefix}{number:016x}." + (label + "_" * 43)[:43]


#: The `auth_sessions` rows, with times in seconds from the moment the rows are written.
SESSIONS: list[dict[str, Any]] = [
    {
        "name": "live",
        "subject": "alice",
        "kind": "login",
        "last_seen": -30,
        "idle": 14 * DAY - 30,
        "absolute": 29 * DAY,
    },
    {
        "name": "slides",
        "subject": "alice",
        "kind": "login",
        "last_seen": -120,
        "idle": 14 * DAY - 120,
        "absolute": 29 * DAY,
    },
    {
        "name": "capped",
        "subject": "alice",
        "kind": "login",
        "last_seen": -120,
        "idle": 3600,
        "absolute": 3600,
    },
    {
        "name": "idle_expired",
        "subject": "alice",
        "kind": "login",
        "last_seen": -14 * DAY,
        "idle": -1,
        "absolute": 16 * DAY,
    },
    {
        "name": "absolute_expired",
        "subject": "alice",
        "kind": "login",
        "last_seen": -60,
        "idle": -1,
        "absolute": -1,
    },
    {
        "name": "revoked",
        "subject": "alice",
        "kind": "login",
        "last_seen": -30,
        "idle": 14 * DAY,
        "absolute": 29 * DAY,
        "revoked": -10,
    },
    {
        "name": "delegated",
        "subject": "alice",
        "kind": "delegated",
        "last_seen": -30,
        "idle": 3600,
        "absolute": 3600,
    },
    {
        "name": "break_glass",
        "subject": "root",
        "kind": "break_glass",
        "last_seen": -30,
        "idle": 3600,
        "absolute": 3600,
    },
]
#: The `personal_access_tokens` rows.
TOKENS: list[dict[str, Any]] = [
    {"name": "write", "number": 1, "scope": "write", "expires": 90 * DAY, "last_used": None},
    {"name": "read", "number": 2, "scope": "read", "expires": 90 * DAY, "last_used": -3600},
    {"name": "fresh", "number": 3, "scope": "read", "expires": 90 * DAY, "last_used": -10},
    {
        "name": "revoked",
        "number": 4,
        "scope": "write",
        "expires": 90 * DAY,
        "last_used": None,
        "revoked": -10,
    },
    {"name": "expired", "number": 5, "scope": "write", "expires": -1, "last_used": None},
]
#: The `service_credentials` rows; `suspended` callers are in `service_caller_suspensions`.
CREDENTIALS: list[dict[str, Any]] = [
    {
        "name": "agent",
        "number": 1,
        "caller": "agent",
        "scopes": ["api", "crud.agent"],
        "expires": 900,
        "last_used": None,
    },
    {
        "name": "render",
        "number": 2,
        "caller": "render",
        "scopes": ["crud.render"],
        "expires": 900,
        "last_used": -10,
    },
    {
        "name": "projects",
        "number": 3,
        "caller": "projects",
        "scopes": ["crud.projects", "flow.host"],
        "expires": 900,
        "last_used": None,
    },
    {
        "name": "revoked",
        "number": 4,
        "caller": "agent",
        "scopes": ["api"],
        "expires": 900,
        "last_used": None,
        "revoked": -10,
    },
    {
        "name": "expired",
        "number": 5,
        "caller": "agent",
        "scopes": ["api"],
        "expires": -1,
        "last_used": None,
    },
    {
        "name": "suspended",
        "number": 6,
        "caller": "storefront",
        "scopes": ["api", "crud.storefront"],
        "expires": 900,
        "last_used": None,
    },
]
SUSPENDED = ["storefront"]

for _row in SESSIONS:
    _row["token"] = _session_token(_row["name"])
for _row in TOKENS:
    _row["token"] = _bearer("sbpat_", _row["number"], _row["name"])
    _row["id"] = f"{_row['number']:016x}"
    _row["principal"] = f"user:{ISSUER}#alice"
for _row in CREDENTIALS:
    _row["token"] = _bearer("sbsvc_", _row["number"], _row["name"])
    _row["id"] = f"{_row['number']:016x}"

S = {row["name"]: row["token"] for row in SESSIONS}
P = {row["name"]: row["token"] for row in TOKENS}
C = {row["name"]: row["token"] for row in CREDENTIALS}
COOKIE = SESSION_COOKIE

#: Request headers, and the caller `AuthGate` makes of them.
CALLER_CASES: list[tuple[str, dict[str, str]]] = [
    ("no headers", {}),
    ("a live session", {"cookie": f"{COOKIE}={S['live']}"}),
    ("among other cookies", {"cookie": f"a=b; {COOKIE}={S['live']}; c=d"}),
    ("a quoted value", {"cookie": f'{COOKIE}="{S["live"]}"'}),
    ("an escape in a quoted value", {"cookie": f'{COOKIE}="\\{S["live"]}"'}),
    ("an octal escape", {"cookie": f'{COOKIE}="\\154{S["live"][1:]}"'}),
    ("spaces around the name and value", {"cookie": f" {COOKIE} = {S['live']} "}),
    ("the last of two wins: revoked", {"cookie": f"{COOKIE}={S['live']}; {COOKIE}={S['revoked']}"}),
    ("the last of two wins: live", {"cookie": f"{COOKIE}={S['revoked']}; {COOKIE}={S['live']}"}),
    ("the name in another case", {"cookie": f"{COOKIE.lower()}={S['live']}"}),
    ("an empty value", {"cookie": f"{COOKIE}="}),
    ("a chunk with no name", {"cookie": S["live"]}),
    ("42 characters", {"cookie": f"{COOKIE}={S['live'][:42]}"}),
    ("44 characters", {"cookie": f"{COOKIE}={S['live']}x"}),
    ("a character outside base64url", {"cookie": f"{COOKIE}={S['live'][:42]}!"}),
    ("an unknown token", {"cookie": f"{COOKIE}={_session_token('unknown')}"}),
    ("past the idle deadline", {"cookie": f"{COOKIE}={S['idle_expired']}"}),
    ("past the absolute deadline", {"cookie": f"{COOKIE}={S['absolute_expired']}"}),
    ("a revoked session", {"cookie": f"{COOKIE}={S['revoked']}"}),
    ("the idle deadline slides", {"cookie": f"{COOKIE}={S['slides']}"}),
    ("the slide stops at the absolute deadline", {"cookie": f"{COOKIE}={S['capped']}"}),
    ("a delegated session", {"cookie": f"{COOKIE}={S['delegated']}"}),
    ("a break-glass session", {"cookie": f"{COOKIE}={S['break_glass']}"}),
    ("a write token", {"authorization": f"Bearer {P['write']}"}),
    ("a read token", {"authorization": f"Bearer {P['read']}"}),
    ("a token used a moment ago", {"authorization": f"Bearer {P['fresh']}"}),
    ("a revoked token", {"authorization": f"Bearer {P['revoked']}"}),
    ("an expired token", {"authorization": f"Bearer {P['expired']}"}),
    ("an unknown token", {"authorization": f"Bearer {_bearer('sbpat_', 9, 'unknown')}"}),
    (
        "a malformed token hides the cookie",
        {"authorization": f"Bearer {P['write'][:-1]}", "cookie": f"{COOKIE}={S['live']}"},
    ),
    (
        "a token beside a cookie",
        {"authorization": f"Bearer {P['read']}", "cookie": f"{COOKIE}={S['delegated']}"},
    ),
    ("the scheme in lower case", {"authorization": f"bearer {P['write']}"}),
    ("the scheme in upper case", {"authorization": f"BEARER {P['write']}"}),
    ("a space after the token", {"authorization": f"Bearer {P['write']} "}),
    ("a tab after the token", {"authorization": f"Bearer {P['write']}\t"}),
    (
        "two spaces before the token",
        {"authorization": f"Bearer  {P['write']}", "cookie": f"{COOKIE}={S['live']}"},
    ),
    (
        "another scheme",
        {"authorization": f"Basic {P['write']}", "cookie": f"{COOKIE}={S['live']}"},
    ),
    (
        "another prefix",
        {"authorization": "Bearer sbxyz_" + P["write"][6:], "cookie": f"{COOKIE}={S['live']}"},
    ),
    ("a bare scheme", {"authorization": "Bearer", "cookie": f"{COOKIE}={S['live']}"}),
    ("a service credential", {"authorization": f"Bearer {C['agent']}"}),
    ("a service credential used a moment ago", {"authorization": f"Bearer {C['render']}"}),
    ("a revoked credential", {"authorization": f"Bearer {C['revoked']}"}),
    ("an expired credential", {"authorization": f"Bearer {C['expired']}"}),
    ("a suspended caller", {"authorization": f"Bearer {C['suspended']}"}),
    (
        "a credential beside a cookie",
        {"authorization": f"Bearer {C['projects']}", "cookie": f"{COOKIE}={S['live']}"},
    ),
    (
        "a malformed credential hides the cookie",
        {"authorization": f"Bearer {C['agent']}=", "cookie": f"{COOKIE}={S['live']}"},
    ),
]

#: The callers of the mode cases, as request headers.
MODE_CALLERS: dict[str, dict[str, str]] = {
    "anonymous": {},
    "person": {"cookie": f"{COOKIE}={S['live']}"},
    "delegated": {"cookie": f"{COOKIE}={S['delegated']}"},
    "token-read": {"authorization": f"Bearer {P['read']}"},
    "token-write": {"authorization": f"Bearer {P['write']}"},
    "service-agent": {"authorization": f"Bearer {C['agent']}"},
    "service-render": {"authorization": f"Bearer {C['render']}"},
    "service-projects": {"authorization": f"Bearer {C['projects']}"},
    "dead-token": {"authorization": f"Bearer {P['revoked']}"},
}
MODE_CLASSES = [
    "public",
    "api",
    "person",
    "flow.host",
    "internal.outputs",
    "crud.agent",
    "crud.render",
    "transfer",
    "exchange",
]
SOCKET_CLASSES = ["api", "person", "crud.agent"]

#: Abstract callers for `would_refuse`'s table.
REFUSE_CALLERS: dict[str, Caller] = {}


def _session(kind: str) -> AuthSession:
    at = datetime(2026, 11, 1, tzinfo=UTC)
    return AuthSession(
        id_hash=b"\0" * 32,
        principal=f"user:{ISSUER}#alice",
        issuer=ISSUER,
        subject="alice",
        name=None,
        email=None,
        kind=kind,  # type: ignore[arg-type]
        created_at=at,
        last_seen_at=at,
        idle_expires_at=at,
        absolute_expires_at=at,
    )


REFUSE_CALLERS = {
    "anonymous": ANONYMOUS,
    "person-login": Person(_session("login")),
    "person-delegated": Person(_session("delegated")),
    "person-break_glass": Person(_session("break_glass")),
    "token-read": PersonalToken("0000000000000001", f"user:{ISSUER}#alice", "read"),
    "token-write": PersonalToken("0000000000000002", f"user:{ISSUER}#alice", "write"),
    "service-agent-api": Service("agent", frozenset({"api", "crud.agent"})),
    "service-agent-crud": Service("agent", frozenset({"crud.agent"})),
    "service-render": Service("render", frozenset({"crud.render"})),
    "service-projects": Service("projects", frozenset({"crud.projects", "flow.host"})),
    "service-internal": Service("agent", frozenset({"internal.outputs", "transfer"})),
}
ALL_CLASSES = [
    "public",
    "api",
    "person",
    "internal.outputs",
    "internal.library",
    "internal.workspace",
    "transfer",
    "exchange",
    "crud.projects",
    "crud.render",
    "crud.print",
    "crud.storefront",
    "crud.agent",
    "crud.intake",
    "crud.profile-fetch",
    "crud.camera",
    "crud.camera-relay",
    "crud.fleet",
    "flow.host",
]
REASONS = [
    "signed_out",
    "not_a_person",
    "no_credential",
    "out_of_scope",
    "wrong_caller",
    "no_request_header",
    "author_header",
    "wrong_author",
]


def describe(caller: Caller) -> dict[str, Any]:
    match caller:
        case Person(session=session):
            return {
                "kind": "person",
                "principal": session.principal,
                "session_kind": session.kind,
            }
        case PersonalToken(token_id=token_id, principal=principal, scope=scope):
            return {"kind": "token", "principal": principal, "scope": scope, "token_id": token_id}
        case Service(caller=name, scopes=scopes):
            return {"kind": "service", "caller": name, "scopes": sorted(scopes)}
    return {"kind": "anonymous"}


def _seed(pool: PgPool) -> datetime:
    now = datetime.now(UTC)

    def at(seconds: int | None) -> datetime | None:
        return None if seconds is None else now + timedelta(seconds=seconds)

    with pool.connection() as conn:
        for row in SESSIONS:
            conn.execute(
                "INSERT INTO auth_sessions (id_hash, principal, issuer, subject, kind,"
                " created_at, last_seen_at, idle_expires_at, absolute_expires_at, revoked_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    hashlib.sha256(row["token"].encode()).digest(),
                    f"user:{ISSUER}#{row['subject']}",
                    ISSUER,
                    row["subject"],
                    row["kind"],
                    at(-DAY),
                    at(row["last_seen"]),
                    at(row["idle"]),
                    at(row["absolute"]),
                    at(row.get("revoked")),
                ),
            )
        for row in TOKENS:
            conn.execute(
                "INSERT INTO personal_access_tokens (id, principal, name, scope, token_hash,"
                " created_at, expires_at, last_used_at, revoked_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row["id"],
                    row["principal"],
                    row["name"],
                    row["scope"],
                    hashlib.sha256(row["token"].encode()).digest(),
                    at(-DAY),
                    at(row["expires"]),
                    at(row["last_used"]),
                    at(row.get("revoked")),
                ),
            )
        for row in CREDENTIALS:
            conn.execute(
                "INSERT INTO service_credentials (id, caller, scopes, secret_hash, created_at,"
                " expires_at, last_used_at, revoked_at, proof)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'inprocess')",
                (
                    row["id"],
                    row["caller"],
                    row["scopes"],
                    hashlib.sha256(row["token"].encode()).digest(),
                    at(-60),
                    at(row["expires"]),
                    at(row["last_used"]),
                    at(row.get("revoked")),
                ),
            )
        for caller in SUSPENDED:
            conn.execute(
                "INSERT INTO service_caller_suspensions (caller, suspended_at, suspended_by)"
                " VALUES (%s, %s, 'test')",
                (caller, now),
            )
    return now


def _state(pool: PgPool) -> dict[str, dict[str, Any]]:
    """Each row's times that a lookup may move, by token."""
    with pool.connection() as conn:
        sessions = conn.execute(
            "SELECT id_hash, last_seen_at, idle_expires_at, absolute_expires_at FROM auth_sessions"
        ).fetchall()
        tokens = conn.execute("SELECT id, last_used_at FROM personal_access_tokens").fetchall()
        credentials = conn.execute("SELECT id, last_used_at FROM service_credentials").fetchall()
    by_hash = {hashlib.sha256(r["token"].encode()).digest(): r["token"] for r in SESSIONS}
    state: dict[str, dict[str, Any]] = {}
    for row in sessions:
        state[by_hash[bytes(row["id_hash"])]] = dict(row)
    for rows, table in ((tokens, TOKENS), (credentials, CREDENTIALS)):
        ids = {r["id"]: r["token"] for r in table}
        for row in rows:
            state[ids[row["id"]]] = dict(row)
    return state


def _scope(headers: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "http",
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
    }


def _caller_vectors(pool: PgPool) -> list[dict[str, Any]]:
    gate = AuthGate(
        lambda *_: None,  # type: ignore[arg-type]
        sessions=lambda: SessionStore(pool),
        credentials=lambda: ServiceCredentialStore(pool),
        tokens=lambda: PersonalTokenStore(pool),
    )
    cases = []
    for name, headers in CALLER_CASES:
        before = _state(pool)
        caller = asyncio.run(gate._caller(_scope(headers)))
        after = _state(pool)
        case: dict[str, Any] = {"name": name, "headers": headers, "caller": describe(caller)}
        moved = []
        for token, row in after.items():
            if row != before[token]:
                moved.append(token)
                if "idle_expires_at" in row:
                    # The slide: the idle deadline is the new last-seen time plus the idle
                    # timeout, and never past the absolute deadline.
                    assert row["idle_expires_at"] == min(
                        row["last_seen_at"] + IDLE_TIMEOUT, row["absolute_expires_at"]
                    )
        # Which rows the lookup wrote: a session's slide, a token's or credential's last use.
        case["writes"] = moved
        cases.append(case)
    return cases


def _samples(metrics: Metrics) -> dict[tuple[str, str], float]:
    out = {}
    for family in metrics.auth_would_refuse.collect():
        for sample in family.samples:
            if sample.name.endswith("_total"):
                out[(sample.labels["class"], sample.labels["reason"])] = sample.value
    return out


def _mode_vectors(pool: PgPool, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    # The route's class and whether it is a scriptless GET come from its path here, so one
    # small app has a route of every class.
    monkeypatch.setattr(check, "route_class", lambda route: route.path.split("/")[2])
    monkeypatch.setattr(check, "scriptless", lambda route: route.path.endswith("/img"))
    metrics = Metrics()
    parts: dict[Any, Any] = {AUTH: SimpleNamespace(would_refuse=WouldRefuse(metrics))}
    settings = SimpleNamespace(auth_mode="off")
    app = FastAPI(dependencies=[Depends(auth_check)])
    app.state.scadbuddy = SimpleNamespace(
        settings=settings, components=SimpleNamespace(get=lambda key: parts[key])
    )

    async def answer() -> dict[str, bool]:
        return {"ok": True}

    async def socket(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_json({"ok": True})
        await ws.close()

    for cls in MODE_CLASSES:
        app.add_api_route(f"/v/{cls}", answer, methods=["GET", "POST"])
        app.add_api_route(f"/v/{cls}/img", answer, methods=["GET"])
    for cls in SOCKET_CLASSES:
        app.add_api_websocket_route(f"/ws/{cls}", socket)
    install_problem_handlers(app)
    app.add_middleware(
        AuthGate,
        sessions=lambda: SessionStore(pool),
        credentials=lambda: ServiceCredentialStore(pool),
        tokens=lambda: PersonalTokenStore(pool),
    )
    client = TestClient(app)

    requests: list[tuple[str, str, str, str, bool]] = []
    for mode in ("off", "observe", "enforce"):
        for caller in MODE_CALLERS:
            for cls in MODE_CLASSES:
                for method in ("GET", "POST"):
                    for header in (False, True):
                        if mode == "off" and (method, header) != ("GET", False):
                            continue
                        requests.append((mode, caller, method, f"/v/{cls}", header))
            for cls in ("api", "person"):
                requests.append((mode, caller, "GET", f"/v/{cls}/img", False))
            for cls in SOCKET_CLASSES:
                requests.append((mode, caller, "SOCKET", f"/ws/{cls}", False))

    cases = []
    for mode, caller, method, path, header in requests:
        settings.auth_mode = mode
        headers = dict(MODE_CALLERS[caller])
        if header:
            headers["x-scadbuddy-request"] = "1"
        before = _samples(metrics)
        case: dict[str, Any] = {
            "mode": mode,
            "caller": caller,
            "method": method,
            "path": path,
            "request_header": header,
        }
        if method == "SOCKET":
            try:
                with client.websocket_connect(path, headers=headers) as ws:
                    assert ws.receive_json() == {"ok": True}
                case["accepted"] = True
            except WebSocketDisconnect as closed:
                assert closed.code == 1008
                case["accepted"] = False
        else:
            response = client.request(method, path, headers=headers)
            case["status"] = response.status_code
            case["body"] = response.json()
            case["www_authenticate"] = response.headers.get("www-authenticate")
        after = _samples(metrics)
        case["counted"] = sorted(
            [c, r] for (c, r), n in after.items() if n != before.get((c, r), 0.0)
        )
        cases.append(case)
    return cases


def _would_refuse_vectors() -> dict[str, Any]:
    rows = []
    for cls in ALL_CLASSES:
        for name, caller in REFUSE_CALLERS.items():
            for method in ("GET", "options", "post"):
                for request_header in (False, True):
                    for header_needed in (False, True):
                        reason = would_refuse(
                            cls,  # type: ignore[arg-type]
                            caller,
                            request_header=request_header,
                            header_needed=header_needed,
                            method=method,
                        )
                        rows.append([cls, name, method, request_header, header_needed, reason])
    return {
        "callers": {name: describe(c) for name, c in REFUSE_CALLERS.items()},
        "columns": ["class", "caller", "method", "request_header", "header_needed", "reason"],
        "rows": rows,
    }


def _refusal_vectors() -> list[dict[str, Any]]:
    cases = []
    for reason in REASONS:
        for cls in ("api", "person", "internal.outputs", "crud.agent", "flow.host"):
            for bearer in (False, True):
                error = refusal(cls, reason, bearer=bearer)  # type: ignore[arg-type]
                cases.append(
                    {
                        "class": cls,
                        "reason": reason,
                        "bearer": bearer,
                        "status": error.status,
                        "type": error.type,
                        "title": error.title,
                        "detail": error.detail,
                        "extensions": error.extensions,
                        "www_authenticate": error.headers.get("WWW-Authenticate"),
                    }
                )
    return cases


def _mode_settings(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    cases = []
    for value in (None, "", "off", "observe", "enforce", "Enforce", " enforce", "on"):
        if value is None:
            monkeypatch.delenv("SCADBUDDY_AUTH_MODE", raising=False)
        else:
            monkeypatch.setenv("SCADBUDDY_AUTH_MODE", value)
        try:
            mode = Settings(
                database_url=UNUSED_DATABASE_URL, temporal_address=UNUSED_TEMPORAL_ADDRESS
            ).auth_mode
        except ValidationError:
            mode = None
        cases.append({"env": value, "mode": mode})
    monkeypatch.delenv("SCADBUDDY_AUTH_MODE", raising=False)
    return cases


def _metric() -> dict[str, Any]:
    counter = Metrics().auth_would_refuse
    counter.labels("api", "signed_out").inc()
    (family,) = counter.collect()
    (sample,) = [s for s in family.samples if s.name.endswith("_total")]
    return {"name": sample.name, "labels": sorted(sample.labels)}


def _dump(doc: dict[str, Any]) -> str:
    """One case to a line, so a diff of the file shows the cases that changed."""

    def items(value: Any, indent: str) -> str:
        if isinstance(value, dict) and any(isinstance(v, list | dict) for v in value.values()):
            inner = indent + "  "
            body = ",\n".join(
                f"{inner}{json.dumps(k)}: {items(v, inner)}" for k, v in value.items()
            )
            return "{\n" + body + "\n" + indent + "}"
        if isinstance(value, list) and value and isinstance(value[0], dict | list):
            inner = indent + "  "
            body = ",\n".join(inner + json.dumps(v, ensure_ascii=False) for v in value)
            return "[\n" + body + "\n" + indent + "]"
        return json.dumps(value, ensure_ascii=False)

    return items(doc, "") + "\n"


def test_the_shared_vectors(pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(pg_pool)
    doc = {
        "about": (
            "The person check's shared vectors (#8038): written by"
            " backend/tests/auth/test_auth_vectors.py (UPDATE_AUTH_VECTORS=1), read by"
            " tools/go/core/internal/auth/vectors_test.go. Times are seconds from the moment"
            " the rows are written. The tokens are test values."
        ),
        "session_cookie": SESSION_COOKIE,
        "idle_timeout_seconds": int(IDLE_TIMEOUT.total_seconds()),
        "issuer": ISSUER,
        "sessions": SESSIONS,
        "tokens": TOKENS,
        "credentials": CREDENTIALS,
        "suspended": SUSPENDED,
        "callers": _caller_vectors(pg_pool),
        "mode_callers": MODE_CALLERS,
        "modes": _mode_vectors(pg_pool, monkeypatch),
        "would_refuse": _would_refuse_vectors(),
        "refusals": _refusal_vectors(),
        "mode_settings": _mode_settings(monkeypatch),
        "metric": _metric(),
    }
    text = _dump(doc)
    assert json.loads(text) == json.loads(json.dumps(doc))
    if os.environ.get(UPDATE_ENV):
        FIXTURE.write_text(text)
    assert FIXTURE.exists(), f"run with {UPDATE_ENV}=1 to write {FIXTURE}"
    assert json.loads(FIXTURE.read_text()) == json.loads(text), (
        f"{FIXTURE.name} is not what Python makes; run with {UPDATE_ENV}=1"
    )
