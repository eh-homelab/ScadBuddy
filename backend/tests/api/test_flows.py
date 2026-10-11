"""The flow routes (#1057, plan 2026-10-09-durable-phase-6-flows.md Task B5) on a real
Temporal and Postgres, with the app's in-process `projects` worker."""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

MIGRATIONS = Path(__file__).parent.parent / "fixtures" / "agent-migrations"
AGENT = {"X-ScadBuddy-Agent-Author": "token:p1", "X-ScadBuddy-Agent-Author-Session": "s1"}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path, pg_conninfo: str) -> Settings:
    """Flows on: a KEK, and the agent's payload-key tables in the test's schema."""
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(b"\x07" * 32).decode())
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        for sql in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(sql.read_text().encode())
    return settings.model_copy(update={"secret_key_file": key})


def script(*body: str) -> str:
    lines = ["import asyncio", "async def main():", *(f"    {b}" for b in body)]
    return "\n".join([*lines, "asyncio.run(main())"])


def register(client: TestClient, body: str, name: str = "swap", **extra: Any) -> dict[str, Any]:
    response = client.post("/api/v1/workflows", json={"name": name, "script": body, **extra})
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def start(client: TestClient, definition_id: str, key: str, **body: Any) -> Any:
    return client.post(
        f"/api/v1/workflows/{definition_id}/runs",
        headers={"Idempotency-Key": key},
        json=body or None,
    )


def until(client: TestClient, run_id: str, check: Any, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(f"/api/v1/workflow-runs/{run_id}")
        if response.status_code == 200 and check(view := response.json()):
            found: dict[str, Any] = view
            return found
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out: {response.status_code} {response.text}")
        time.sleep(0.1)


def test_a_bad_script_is_refused_by_line_and_nothing_is_written(client: TestClient) -> None:
    response = client.post(
        "/api/v1/workflows", json={"name": "bad", "script": script("await sleep('x')")}
    )
    assert response.status_code == 422
    assert [p["line"] for p in response.json()["problems"]] == [3]
    assert client.get("/api/v1/workflows").json() == []


def test_an_oversized_script_is_413(client: TestClient) -> None:
    response = client.post("/api/v1/workflows", json={"name": "big", "script": "#" * 65537})
    assert response.status_code == 413


def test_versions_are_listed_at_the_newest(client: TestClient) -> None:
    one = register(client, script("await sleep(0.1)"))
    two = register(client, script("await sleep(0.2)"))
    assert (one["version"], two["version"]) == (1, 2)
    listed = client.get("/api/v1/workflows").json()
    assert [(d["name"], d["version"]) for d in listed] == [("swap", 2)]
    assert "script" not in listed[0]
    assert client.get(f"/api/v1/workflows/{one['id']}").json()["script"] == script(
        "await sleep(0.1)"
    )


def test_a_start_needs_an_idempotency_key(client: TestClient) -> None:
    definition = register(client, script("return 1"))
    assert client.post(f"/api/v1/workflows/{definition['id']}/runs").status_code == 428


def test_a_run_is_answered_at_acceptance_and_succeeds(client: TestClient) -> None:
    definition = register(client, script("await sleep(0.1)", "return 7"))
    response = start(client, definition["id"], "k1")
    assert response.status_code == 202
    answer = response.json()
    assert (answer["status"], answer["repeated"]) == ("starting", False)
    view = until(client, answer["id"], lambda v: v["status"] == "succeeded")
    assert view["run"]["result"] == "result: 7"
    assert view["run"]["started_by"] == {"kind": "browser"}


def test_a_resent_start_reaches_the_same_run_and_runs_the_script_once(
    client: TestClient,
) -> None:
    definition = register(client, script("await sleep(3)", "return 1"))
    first = start(client, definition["id"], "same")
    assert first.status_code == 202
    run_id = first.json()["id"]
    soon = start(client, definition["id"], "same")
    assert soon.status_code == 200
    assert (soon.json()["id"], soon.json()["repeated"]) == (run_id, True)
    until(client, run_id, lambda v: v["status"] == "running")
    running = start(client, definition["id"], "same")
    assert (running.status_code, running.json()["id"]) == (200, run_id)
    view = until(client, run_id, lambda v: v["status"] == "succeeded")
    after = start(client, definition["id"], "same")
    assert (after.status_code, after.json()) == (
        200,
        {"id": run_id, "status": "succeeded", "repeated": True},
    )
    assert len(view["run"]["steps"]) == 1
    assert len(client.get("/api/v1/workflow-runs").json()) == 1


def test_a_waiting_run_lists_what_it_waits_on(client: TestClient) -> None:
    definition = register(client, script("await wait_for_human('Swap to pink?', 600)"))
    run_id = start(client, definition["id"], "w").json()["id"]
    view = until(client, run_id, lambda v: v["status"] == "waiting" and v["pending"])
    assert view["live"] is True
    [entry] = view["pending"]
    assert (entry["kind"], entry["fn"], entry["prompt"]) == (
        "answer",
        "wait_for_human",
        "Swap to pink?",
    )


def test_a_definition_no_longer_type_checking_is_refused_at_start(
    client: TestClient, pg_conninfo: str
) -> None:
    definition = register(client, script("return 1"))
    # As if the host functions changed under a stored script.
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE workflow_definitions SET script = %s WHERE id = %s",
            (script("await gone()"), definition["id"]),
        )
    assert start(client, definition["id"], "k").status_code == 422


def test_the_approval_timeout_resolves_run_then_flow_then_setting(client: TestClient) -> None:
    inherits = register(client, script("return 1"), name="a")
    never = register(client, script("return 1"), name="b", approval_timeout="never")
    timed = register(client, script("return 1"), name="c", approval_timeout=600)
    assert (inherits["approval_timeout_s"], never["approval_timeout_s"]) == (None, 0)
    runs = {
        "inherits": start(client, inherits["id"], "1").json()["id"],
        "never": start(client, never["id"], "1").json()["id"],
        "timed": start(client, timed["id"], "1").json()["id"],
        "override": start(client, timed["id"], "2", approval_timeout=30).json()["id"],
    }
    got = {
        name: until(client, run_id, lambda v: v["run"] is not None)["run"]["approval_timeout_s"]
        for name, run_id in runs.items()
    }
    assert got == {"inherits": 0, "never": 0, "timed": 600, "override": 30}


def test_an_approval_timeout_out_of_range_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/v1/workflows",
        json={"name": "x", "script": script("return 1"), "approval_timeout": 5},
    )
    assert response.status_code == 422


def test_an_agent_start_is_recorded_as_its_session(client: TestClient) -> None:
    definition = register(client, script("return 1"))
    response = client.post(
        f"/api/v1/workflows/{definition['id']}/runs",
        headers={"Idempotency-Key": "a", **AGENT},
    )
    run_id = response.json()["id"]
    until(client, run_id, lambda v: v["status"] == "succeeded")
    assert [r["id"] for r in client.get("/api/v1/workflow-runs?session=s1").json()] == [run_id]


def test_delete_forgets_the_run(client: TestClient, pg_conninfo: str) -> None:
    definition = register(client, script("await sleep(60)"))
    run_id = start(client, definition["id"], "d").json()["id"]
    until(client, run_id, lambda v: v["status"] == "running")
    assert client.delete(f"/api/v1/workflow-runs/{run_id}", headers=AGENT).status_code == 403
    assert client.delete(f"/api/v1/workflow-runs/{run_id}").status_code == 204
    assert client.get(f"/api/v1/workflow-runs/{run_id}").status_code == 404
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM ai_payload_keys").fetchone() == (0,)
        assert conn.execute("SELECT subject FROM ai_forgotten_subjects").fetchall() == [
            (f"flow-{run_id}",)
        ]
    assert client.delete(f"/api/v1/workflow-runs/{uuid.uuid4()}").status_code == 404


class TestWithoutAKey:
    @pytest.fixture
    def settings(self, settings: Settings) -> Iterator[Settings]:
        yield settings.model_copy(update={"secret_key_file": None})

    def test_a_start_is_503(self, settings: Settings) -> None:
        with TestClient(create_app(settings)) as client:
            definition = register(client, script("return 1"))
            response = start(client, definition["id"], "k")
        assert response.status_code == 503
        assert response.json()["type"].endswith("/flows-unavailable")
