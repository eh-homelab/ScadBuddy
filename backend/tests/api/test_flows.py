"""The flow routes (#1057, plan 2026-10-09-durable-phase-6-flows.md Task B5) on a real
Temporal and Postgres, with the app's in-process `projects` worker."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.gitrepo import git_env
from scadbuddy.core.secrets import seal_bytes
from scadbuddy.core.settings import Settings
from scadbuddy.flows.images import blob_context
from scadbuddy.flows.keys import payload_keys
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
    client: TestClient, settings: Settings, pg_conninfo: str
) -> None:
    definition = register(client, script("return 1"))
    # As if the host functions changed under a stored script: the version names a
    # commit whose script no longer checks.
    flows = settings.data_dir / "flows"
    (flows / "swap" / "flow.py").write_text(script("await gone()"))
    git = ["git", "-c", f"safe.directory={flows}"]
    subprocess.run([*git, "commit", "-qam", "Break"], cwd=flows, env=git_env(), check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], cwd=flows, env=git_env(), capture_output=True, text=True
    ).stdout.strip()
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE workflow_definitions SET commit = %s WHERE id = %s", (head, definition["id"])
        )
    assert start(client, definition["id"], "k").status_code == 422


def test_a_start_whose_script_is_missing_from_git_is_409(
    client: TestClient, pg_conninfo: str
) -> None:
    definition = register(client, script("return 1"))
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE workflow_definitions SET commit = %s WHERE id = %s",
            ("f" * 40, definition["id"]),
        )
    response = start(client, definition["id"], "k")
    assert response.status_code == 409
    assert response.json()["type"].endswith("/flow-script-missing")


def test_a_flows_history_source_diff_and_restore(client: TestClient) -> None:
    """#2503: a flow's versions are commits in `data/flows`, each authored by its
    registrar, and a run records the commit it ran."""
    one = register(client, script("return 1"))
    response = client.post(
        "/api/v1/workflows", json={"name": "swap", "script": script("return 2")}, headers=AGENT
    )
    assert response.status_code == 201, response.text
    two = response.json()
    assert (two["version"], two["created_by"]) == (
        2,
        {"kind": "agent", "principal": "token:p1", "session": "s1"},
    )

    base = "/api/v1/workflows/names/swap/versions"
    versions = client.get(base).json()
    assert [(v["version"], v["id"], v["commit"], v["created_by"]["kind"]) for v in versions] == [
        (2, two["id"], two["commit"], "agent"),
        (1, one["id"], one["commit"], "browser"),
    ]
    source = client.get(f"{base}/{one['commit'][:7]}/source")
    assert (source.status_code, source.text) == (200, script("return 1"))
    diff = client.get(f"{base}/{two['commit']}/diff").json()
    assert (diff["base"], diff["head"]) == (one["commit"], two["commit"])
    assert "+    return 2" in diff["patch"]

    restored = client.post(f"{base}/{one['commit']}/restore")
    assert restored.status_code == 201, restored.text
    assert (restored.json()["version"], restored.json()["script"]) == (3, script("return 1"))
    assert [d["version"] for d in client.get("/api/v1/workflows").json()] == [3]

    # A run of an older version runs that version's commit.
    run_id = start(client, one["id"], "k1").json()["id"]
    view = until(client, run_id, lambda v: v["status"] == "succeeded")
    assert (view["run"]["commit"], view["run"]["result"]) == (one["commit"], "result: 1")

    register(client, script("return 3"), name="other")
    other = client.get("/api/v1/workflows/names/other/versions").json()[0]["commit"]
    assert client.get("/api/v1/workflows/names/nothing/versions").status_code == 404
    assert client.get(f"{base}/{other}/source").status_code == 404


AGENT_ACTOR = {"kind": "agent", "principal": "token:p1", "session": "s1"}


def listed(client: TestClient, **query: str) -> list[tuple[str, int]]:
    return [(d["name"], d["version"]) for d in client.get("/api/v1/workflows", params=query).json()]


def test_an_archived_flow_is_hidden_and_never_starts_until_unarchived(
    client: TestClient,
) -> None:
    """#2485: archiving a flow archives every version, by name; it is reversible."""
    one = register(client, script("return 1"))
    two = register(client, script("return 2"))
    register(client, script("return 3"), name="other")
    archived = client.post("/api/v1/workflows/names/swap/archive", headers=AGENT)
    assert archived.status_code == 200, archived.text
    state = archived.json()
    assert (state["name"], state["archived_by"], state["deleted_at"]) == ("swap", AGENT_ACTOR, None)
    assert state["archived_at"] is not None

    assert listed(client) == [("other", 1)]
    shown = client.get("/api/v1/workflows", params={"archived": "true"}).json()
    assert [(d["name"], d["version"], d["archived_by"]) for d in shown] == [
        ("other", 1, None),
        ("swap", 2, AGENT_ACTOR),
    ]
    # Every version is archived, and its page says so.
    assert client.get(f"/api/v1/workflows/{one['id']}").json()["archived_by"] == AGENT_ACTOR
    for definition in (one, two):
        refused = start(client, definition["id"], f"k-{definition['version']}")
        assert refused.status_code == 409, refused.text
        assert refused.json()["type"].endswith("/flow-archived")
    assert client.get("/api/v1/workflow-runs").json() == []
    # Archiving again changes nothing; a flow that does not exist is 404.
    again = client.post("/api/v1/workflows/names/swap/archive").json()
    assert (again["archived_at"], again["archived_by"]) == (state["archived_at"], AGENT_ACTOR)
    assert client.post("/api/v1/workflows/names/nothing/archive").status_code == 404

    unarchived = client.post("/api/v1/workflows/names/swap/unarchive")
    assert unarchived.status_code == 200, unarchived.text
    assert (unarchived.json()["archived_at"], unarchived.json()["archived_by"]) == (None, None)
    assert listed(client) == [("other", 1), ("swap", 2)]
    run_id = start(client, two["id"], "after").json()["id"]
    until(client, run_id, lambda v: v["status"] == "succeeded")


def test_a_deleted_flow_keeps_its_runs_scripts_and_restores_as_a_new_version(
    client: TestClient,
) -> None:
    """#2485: a delete is a commit removing `<name>/`; its versions stay in the history,
    a finished run still shows the script it ran, and a restore brings the flow back."""
    one = register(client, script("return 1"))
    finished = start(client, one["id"], "done").json()["id"]
    until(client, finished, lambda v: v["status"] == "succeeded")
    two = register(client, script("await sleep(3)", "return 2"))
    running = start(client, two["id"], "running").json()["id"]
    until(client, running, lambda v: v["status"] == "running")

    deleted = client.delete("/api/v1/workflows/names/swap", headers=AGENT)
    assert deleted.status_code == 200, deleted.text
    state = deleted.json()
    assert state["deleted_by"] == AGENT_ACTOR and state["deleted_commit"]
    assert listed(client) == []
    assert listed(client, archived="true") == [("swap", 2)]

    # The run in progress finishes on the version it started with.
    view = until(client, running, lambda v: v["status"] == "succeeded")
    assert (view["run"]["commit"], view["run"]["result"]) == (two["commit"], "result: 2")
    # A finished run's script is still read from git at its commit.
    ran = client.get(f"/api/v1/workflows/{one['id']}")
    assert ran.status_code == 200
    assert (ran.json()["script"], ran.json()["deleted_by"]) == (script("return 1"), AGENT_ACTOR)
    versions = client.get("/api/v1/workflows/names/swap/versions").json()
    assert [v["version"] for v in versions] == [2, 1]
    # Nothing new starts; deleting again is the same delete.
    refused = start(client, two["id"], "after-delete")
    assert (refused.status_code, refused.json()["type"].rsplit("/", 1)[1]) == (409, "flow-deleted")
    again = client.delete("/api/v1/workflows/names/swap").json()
    assert (again["deleted_commit"], again["deleted_by"]) == (state["deleted_commit"], AGENT_ACTOR)
    assert client.delete("/api/v1/workflows/names/nothing").status_code == 404

    base = "/api/v1/workflows/names/swap/versions"
    restored = client.post(f"{base}/{one['commit']}/restore")
    assert restored.status_code == 201, restored.text
    back = restored.json()
    assert (back["version"], back["script"], back["deleted_at"]) == (3, script("return 1"), None)
    assert listed(client) == [("swap", 3)]
    run_id = start(client, back["id"], "restored").json()["id"]
    until(client, run_id, lambda v: v["status"] == "succeeded")


def test_registering_a_deleted_name_brings_it_back(client: TestClient) -> None:
    register(client, script("return 1"))
    # Archived, then deleted: it comes back unarchived, ready to run.
    assert client.post("/api/v1/workflows/names/swap/archive").status_code == 200
    assert client.delete("/api/v1/workflows/names/swap").status_code == 200
    again = register(client, script("return 2"))
    assert (again["version"], again["deleted_at"], again["archived_at"]) == (2, None, None)
    assert listed(client) == [("swap", 2)]
    # One only archived stays archived when a version is added.
    assert client.post("/api/v1/workflows/names/swap/archive").status_code == 200
    assert register(client, script("return 3"))["archived_at"] is not None


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


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def test_a_runs_images_are_listed_served_from_their_blobs_and_forgotten_with_it(
    client: TestClient, settings: Settings, pg_conninfo: str
) -> None:
    """#2477: the agent keeps a flow call's images sealed under the run's key; the run
    lists them as pointers and serves each by name, until the run is forgotten."""
    definition = register(client, script("return 1"))
    run_id = start(client, definition["id"], "img").json()["id"]
    until(client, run_id, lambda v: v["status"] == "succeeded")
    subject = f"flow-{run_id}"
    name = hashlib.sha256(PNG).hexdigest() + ".png"
    keys = payload_keys(settings)
    assert keys is not None
    key = asyncio.run(keys[0].key_for(subject, False))
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO ai_flow_blobs (subject, name, media_type, size, data)"
            " VALUES (%s, %s, 'image/png', %s, %s)",
            (subject, name, len(PNG), seal_bytes(key, PNG, blob_context(subject, name))),
        )
    view = client.get(f"/api/v1/workflow-runs/{run_id}").json()
    assert view["images"] == [{"name": name, "mediaType": "image/png", "bytes": len(PNG)}]
    # The run's view carries the pointer only.
    assert base64.b64encode(PNG).decode()[:16] not in json.dumps(view)
    served = client.get(f"/api/v1/workflow-runs/{run_id}/images/{name}")
    assert served.status_code == 200
    assert (served.content, served.headers["content-type"]) == (PNG, "image/png")
    assert "immutable" in served.headers["cache-control"]
    other = "0" * 64 + ".png"
    for missing in (other, "x.png", "..%2Fx.png"):
        assert client.get(f"/api/v1/workflow-runs/{run_id}/images/{missing}").status_code == 404
    assert client.get(f"/api/v1/workflow-runs/{uuid.uuid4()}/images/{name}").status_code == 404
    assert client.delete(f"/api/v1/workflow-runs/{run_id}").status_code == 204
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM ai_flow_blobs").fetchone() == (0,)
    assert client.get(f"/api/v1/workflow-runs/{run_id}/images/{name}").status_code == 404


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


def test_an_answer_reaches_the_run_once(client: TestClient) -> None:
    definition = register(
        client, script("a = await wait_for_human('Swap to pink?', 600)", "return a['answer']")
    )
    run_id = start(client, definition["id"], "w").json()["id"]
    view = until(client, run_id, lambda v: v["status"] == "waiting" and v["pending"])
    call_id = view["pending"][0]["call_id"]
    url = f"/api/v1/workflow-runs/{run_id}/answer"
    body = {"call_id": call_id, "answer": "yes"}
    assert client.post(url, json=body).status_code == 428
    refused = client.post(url, json=body, headers={"Idempotency-Key": "a0", **AGENT})
    assert refused.status_code == 403
    assert refused.json()["type"].endswith("/flow-entry-browser-only")
    answered = client.post(url, json=body, headers={"Idempotency-Key": "a1"})
    assert answered.status_code == 200, answered.text
    assert answered.json()["outcome"] == "answered"
    done = until(client, run_id, lambda v: v["status"] == "succeeded")
    assert done["run"]["result"] == "result: 'yes'"
    again = client.post(url, json=body, headers={"Idempotency-Key": "a2"})
    assert again.status_code == 409


def test_an_answer_is_sealed_before_the_operation_records_it(
    client: TestClient, pg_conninfo: str
) -> None:
    """#2221: the operation's input (its history) and its `operations` row carry the
    answer sealed under the run's key, never its text."""
    definition = register(
        client, script("a = await wait_for_human('Swap to pink?', 600)", "return a['answer']")
    )
    run_id = start(client, definition["id"], "w").json()["id"]
    view = until(client, run_id, lambda v: v["status"] == "waiting" and v["pending"])
    secret = "pink-but-only-the-lid-7f3e"
    answered = client.post(
        f"/api/v1/workflow-runs/{run_id}/answer",
        json={"call_id": view["pending"][0]["call_id"], "answer": secret},
        headers={"Idempotency-Key": "a1"},
    )
    assert answered.status_code == 200, answered.text
    done = until(client, run_id, lambda v: v["status"] == "succeeded")
    assert done["run"]["result"] == f"result: '{secret}'"
    with psycopg.connect(pg_conninfo) as conn:
        [(request,)] = conn.execute(
            "SELECT request::text FROM operations WHERE kind = 'flow_answer'"
        ).fetchall()
    assert "answer_sealed" in request
    assert secret not in request


def test_an_answer_to_a_run_with_no_key_is_404(client: TestClient) -> None:
    response = client.post(
        f"/api/v1/workflow-runs/{uuid.uuid4()}/answer",
        json={"call_id": "c", "answer": "x"},
        headers={"Idempotency-Key": "n"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("blank", ["", "  \n\t"])
def test_a_blank_answer_is_422(client: TestClient, blank: str) -> None:
    """#2379: a click on an empty box is not an answer."""
    response = client.post(
        f"/api/v1/workflow-runs/{uuid.uuid4()}/answer",
        json={"call_id": "c", "answer": blank},
        headers={"Idempotency-Key": "b"},
    )
    assert response.status_code == 422


def test_an_answer_to_a_call_not_waiting_is_stale(client: TestClient) -> None:
    definition = register(client, script("await wait_for_human('q?', 600)"))
    run_id = start(client, definition["id"], "w").json()["id"]
    until(client, run_id, lambda v: v["status"] == "waiting")
    response = client.post(
        f"/api/v1/workflow-runs/{run_id}/answer",
        json={"call_id": "not-a-call", "answer": "x"},
        headers={"Idempotency-Key": "s"},
    )
    assert response.status_code == 409
    assert response.json()["type"].endswith("/stale-entry")


def test_a_denial_reaches_the_parked_print(client: TestClient, pg_conninfo: str) -> None:
    definition = register(
        client,
        script(
            "try:",
            "    await queue_print({'output_id': 'o1'}, {'choices': {}})",
            "except Exception as e:",
            "    return str(e)",
        ),
    )
    run_id = start(client, definition["id"], "p").json()["id"]
    view = until(client, run_id, lambda v: v["status"] == "waiting" and v["pending"])
    [entry] = view["pending"]
    assert (entry["kind"], entry["fn"]) == ("approval", "queue_print")
    decided = client.post(
        f"/api/v1/workflow-runs/{run_id}/decide",
        json={"call_id": entry["call_id"], "approved": False, "reason": "not today"},
        headers={"Idempotency-Key": "d1"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["outcome"] == "denied"
    done = until(client, run_id, lambda v: v["status"] == "succeeded")
    assert "ToolApprovalDenied" in done["run"]["result"]
    assert "not today" in done["run"]["result"]
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT outcome FROM workflow_run_decisions").fetchall() == [
            ("denied",)
        ]
