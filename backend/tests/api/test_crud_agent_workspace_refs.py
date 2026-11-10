"""The holds of the agent's workspace files (#5299, in place of its trigger on ``blob_refs``):
under ``/api/v1/internal/crud/agent/workspace-blob-refs``, only for the ``agent`` service
credential, they hold, release and list ``(ai_session_file, <workspace>/<path>)`` rows."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from scadbuddy.store.refs import BlobRefs
from tests.api.test_crud_agent_cost import token

BASE = "/api/v1/internal/crud/agent/workspace-blob-refs"
KEY = "workspace-" + "a" * 64
OTHER = "workspace-" + "b" * 64
HOLDER = "session:0b0e3c55-6f1e-4a39-9d0e-8a1c2b3d4e5f/notes/a.txt"


def _refs(client: TestClient) -> BlobRefs:
    app: Any = client.app
    refs: BlobRefs = app.state.scadbuddy.refs
    return refs


def _rows(client: TestClient) -> list[tuple[str, str, str]]:
    with _refs(client).pool.connection() as conn:
        rows = conn.execute(
            "SELECT key, holder_kind, holder_id FROM blob_refs ORDER BY holder_id, key"
        ).fetchall()
    return [(r["key"], r["holder_kind"], r["holder_id"]) for r in rows]


def _age(client: TestClient, key: str, seconds: int) -> None:
    with _refs(client).pool.connection() as conn:
        conn.execute(
            "UPDATE blob_refs SET created_at = now() - make_interval(secs => %s) WHERE key = %s",
            (seconds, key),
        )


def test_a_hold_sent_twice_writes_one_row_and_a_release_drops_it(client: TestClient) -> None:
    agent = token(client)
    body = {"key": KEY, "holder_id": HOLDER}
    assert client.post(BASE, json=body, headers=agent).status_code == 204
    _age(client, KEY, 600)
    first = client.post(BASE, json=body, headers=agent)
    assert first.status_code == 204, first.text
    assert first.headers["x-scadbuddy-crud"] == "1"
    assert _rows(client) == [(KEY, "ai_session_file", HOLDER)]
    # The second hold refreshed `created_at`: it is not older than a minute any more.
    young = client.post(f"{BASE}/release", json={**body, "older_than_seconds": 60}, headers=agent)
    assert young.json() == {"released": False}
    assert _rows(client) == [(KEY, "ai_session_file", HOLDER)]

    released = client.post(f"{BASE}/release", json=body, headers=agent)
    assert released.status_code == 200, released.text
    assert released.json() == {"released": True}
    assert _rows(client) == []
    # A release of a hold already gone is a success.
    again = client.post(f"{BASE}/release", json=body, headers=agent)
    assert (again.status_code, again.json()) == (200, {"released": False})


def test_a_release_with_a_grace_drops_only_an_older_hold(client: TestClient) -> None:
    agent = token(client)
    client.post(BASE, json={"key": KEY, "holder_id": HOLDER}, headers=agent)
    _age(client, KEY, 7200)
    answer = client.post(
        f"{BASE}/release",
        json={"key": KEY, "holder_id": HOLDER, "older_than_seconds": 3600},
        headers=agent,
    )
    assert answer.json() == {"released": True}
    assert _rows(client) == []


def test_the_list_pages_the_holds_older_than_asked(client: TestClient) -> None:
    agent = token(client)
    holders = [f"principal:bearer:token:t1/f{n}" for n in range(3)]
    for holder in holders:
        client.post(BASE, json={"key": KEY, "holder_id": holder}, headers=agent)
    client.post(BASE, json={"key": OTHER, "holder_id": holders[0]}, headers=agent)
    _age(client, KEY, 7200)
    # Another holder kind is never listed.
    _refs(client).add(KEY, "job", "job-1")

    every = client.get(BASE, headers=agent)
    assert every.status_code == 200, every.text
    assert [(h["holder_id"], h["key"]) for h in every.json()["holds"]] == [
        (holders[0], KEY),
        (holders[0], OTHER),
        (holders[1], KEY),
        (holders[2], KEY),
    ]
    assert every.json()["next"] is None

    first = client.get(BASE, params={"older_than_seconds": 3600, "limit": 2}, headers=agent)
    page = first.json()
    assert [h["holder_id"] for h in page["holds"]] == holders[:2]
    assert page["next"] == {"key": KEY, "holder_id": holders[1]}
    rest = client.get(
        BASE,
        params={
            "older_than_seconds": 3600,
            "limit": 2,
            "after_holder_id": page["next"]["holder_id"],
            "after_key": page["next"]["key"],
        },
        headers=agent,
    ).json()
    assert rest == {"holds": [{"key": KEY, "holder_id": holders[2]}], "next": None}


def test_only_a_workspace_key_and_holder_are_taken(client: TestClient) -> None:
    agent = token(client)
    for body in (
        {"key": "piece-" + "a" * 64, "holder_id": HOLDER},
        {"key": KEY, "holder_id": "job-1"},
        {"key": KEY, "holder_id": "session:x"},
        {"key": KEY, "holder_id": HOLDER, "holder_kind": "job"},
    ):
        assert client.post(BASE, json=body, headers=agent).status_code == 422, body
        assert client.post(f"{BASE}/release", json=body, headers=agent).status_code == 422
    assert _rows(client) == []


def test_an_age_past_a_year_is_refused_not_an_overflow(client: TestClient) -> None:
    """Review of 610034b0b: ``older_than_seconds`` past ``timedelta``'s range raised
    ``OverflowError``, a 500; it is bounded at a year, so a larger value is a 422."""
    agent = token(client)
    client.post(BASE, json={"key": KEY, "holder_id": HOLDER}, headers=agent)
    year = 31_536_000
    for seconds in (year + 1, 10**14):
        body = {"key": KEY, "holder_id": HOLDER, "older_than_seconds": seconds}
        assert client.post(f"{BASE}/release", json=body, headers=agent).status_code == 422
        listed = client.get(BASE, params={"older_than_seconds": seconds}, headers=agent)
        assert listed.status_code == 422
    body = {"key": KEY, "holder_id": HOLDER, "older_than_seconds": year}
    assert client.post(f"{BASE}/release", json=body, headers=agent).json() == {"released": False}
    assert client.get(BASE, params={"older_than_seconds": year}, headers=agent).status_code == 200
    assert _rows(client) == [(KEY, "ai_session_file", HOLDER)]


def test_only_the_agent_credential_reaches_them(client: TestClient) -> None:
    body = {"key": KEY, "holder_id": HOLDER}
    # The browser's own session (the test client) and another service are refused.
    assert client.post(BASE, json=body).status_code == 401
    assert client.post(BASE, json=body, headers=token(client, "print")).status_code == 403
    assert client.post(f"{BASE}/release", json=body).status_code == 401
    assert client.get(BASE, headers=token(client, "print")).status_code == 403
    assert _rows(client) == []
