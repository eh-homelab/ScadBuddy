"""Reading activity (spec 2026-10-09 §6, §7): the list, its facets and one row."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import psycopg
from fastapi.testclient import TestClient

from scadbuddy.activity.index import ActivityIndex, Count
from scadbuddy.api.activity import _subject_labels
from scadbuddy.library.outputs import OutputNotFoundError
from tests.api.conftest import AgentTables, SeedActivity, wait_for_job
from tests.api.test_send import make_output
from tests.support.operations import press

ADVANCED = {"workflow_id", "run_id", "traceparent", "source", "source_id", "facets"}


def ids(page: dict[str, object]) -> list[str]:
    items = page["items"]
    assert isinstance(items, list)
    return [r["id"] for r in items]


def test_lists_newest_first_with_filters(client: TestClient, seed_activity: SeedActivity) -> None:
    seed_activity(id="render:a", principal="browser", status="succeeded", kind="render")
    seed_activity(id="render:b", principal="token:t1", status="failed", kind="render")
    assert ids(client.get("/api/v1/activity").json()) == ["render:b", "render:a"]
    page = client.get("/api/v1/activity", params={"status": "failed"}).json()
    assert ids(page) == ["render:b"]
    page = client.get("/api/v1/activity", params={"principal": "browser"}).json()
    assert ids(page) == ["render:a"]


def test_filters_by_session_tool_subject_parent_and_title(
    client: TestClient, seed_activity: SeedActivity
) -> None:
    seed_activity(id="render:a", session="s-1", via_tool="render_model", subject="box")
    seed_activity(id="render:b", parent_id="render:a", title="Render 100% keychain")
    seed_activity(id="render:c", title="Render keychain")
    for params, expected in [
        ({"session": "s-1"}, ["render:a"]),
        ({"via_tool": "render_model"}, ["render:a"]),
        ({"subject": "box"}, ["render:a"]),
        ({"parent": "render:a"}, ["render:b"]),
        ({"q": "100%"}, ["render:b"]),
        ({"q": "KEYCHAIN"}, ["render:c", "render:b"]),
    ]:
        assert ids(client.get("/api/v1/activity", params=params).json()) == expected, params


def test_since_and_until(client: TestClient, seed_activity: SeedActivity) -> None:
    seed_activity(id="render:a")
    created = client.get("/api/v1/activity").json()["items"][0]["created_at"]
    assert ids(client.get("/api/v1/activity", params={"since": created}).json()) == ["render:a"]
    assert ids(client.get("/api/v1/activity", params={"until": created}).json()) == []
    bad = client.get("/api/v1/activity", params={"since": "yesterday"})
    assert bad.status_code == 422


def test_placeholders_are_never_listed(client: TestClient, activity_index: ActivityIndex) -> None:
    # A route's attribution that arrived before its source's first write.
    asyncio.run(
        activity_index.attribute("render:early", principal="browser", session=None, via_tool=None)
    )
    assert ids(client.get("/api/v1/activity").json()) == []
    assert client.get("/api/v1/activity/render:early").status_code == 404


def test_facet_filter(client: TestClient, seed_activity: SeedActivity) -> None:
    seed_activity(id="render:a", kind="render", facets={"slug": "keychain"})
    seed_activity(id="render:b", kind="render", facets={"slug": "box"})
    page = client.get("/api/v1/activity", params={"kind": "render", "facet.slug": "box"}).json()
    assert ids(page) == ["render:b"]


def test_unknown_param_is_400(client: TestClient) -> None:
    for params in (
        {"stauts": "failed"},
        {"kind": "render", "facet.nope": "x"},
        {"facet.slug": "x"},
        # Declared, but not filterable.
        {"kind": "render", "facet.model_version": "x"},
        {"cursor": "not-a-cursor"},
    ):
        response = client.get("/api/v1/activity", params=params)
        assert response.status_code == 400, params
        assert response.headers["content-type"].startswith("application/problem+json")
    for params in ({"limit": "0"}, {"limit": "201"}, {"status": "done"}):
        response = client.get("/api/v1/activity", params=params)
        assert response.status_code == 422, params
        assert response.headers["content-type"].startswith("application/problem+json")
    for path, params in (
        ("/api/v1/activity/facets", {"kind": "render"}),
        ("/api/v1/activity/render:a", {"advance": "1"}),
    ):
        response = client.get(path, params=params)
        assert response.status_code == 400, (path, params)


def test_facets_from_data(client: TestClient, seed_activity: SeedActivity) -> None:
    seed_activity(id="render:a", principal="token:t1", via_tool="render_model", subject="box")
    body = client.get("/api/v1/activity/facets").json()
    assert {"value": "token:t1", "count": 1, "label": None} in body["principals"]
    assert {"value": "render_model", "count": 1, "label": None} in body["tools"]
    assert {"value": "box", "count": 1, "label": None} in body["subjects"]
    render = next(k for k in body["kinds"] if k["key"] == "render")
    assert {"name": "slug", "type": "keyword", "label": "Model", "filterable": True} in render[
        "facets"
    ]
    assert body["statuses"] == ["queued", "running", "waiting", "succeeded", "failed", "cancelled"]
    later = client.get("/api/v1/activity/facets", params={"since": "2999-01-01T00:00:00Z"}).json()
    assert later["principals"] == []


def test_a_model_subject_is_labelled_with_its_name(
    client: TestClient, seed_activity: SeedActivity, model: str
) -> None:
    """#3856: a chip reads as the model's name; the value it filters on stays the slug.
    A subject ScadBuddy holds no name for (a library file, an output gone) has none."""
    seed_activity(id="render:a", subject=model)
    seed_activity(id="render:b", subject="library:296")
    seed_activity(id="render:c", subject="output:" + "0" * 32)
    subjects = {s["value"]: s for s in client.get("/api/v1/activity/facets").json()["subjects"]}
    assert subjects[model]["label"] == "Demo"
    assert subjects["library:296"]["label"] is None
    assert subjects["output:" + "0" * 32]["label"] is None


def test_an_output_subject_is_labelled_from_the_store(
    client: TestClient, seed_activity: SeedActivity, model: str
) -> None:
    """#3856 through the real store and catalogue: a named output reads as its name, an
    unnamed one as its model's."""
    named = make_output(client, model, name="Kai's tag")
    job_id = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}}).json()[
        "job_id"
    ]
    wait_for_job(client, job_id)
    unnamed = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": job_id}, headers=press()
    ).json()["id"]
    seed_activity(id="render:a", subject=f"output:{named}")
    seed_activity(id="render:b", subject=f"output:{unnamed}")
    subjects = {s["value"]: s for s in client.get("/api/v1/activity/facets").json()["subjects"]}
    assert subjects[f"output:{named}"]["label"] == "Kai's tag"
    assert subjects[f"output:{unnamed}"]["label"] == "Demo"


def test_an_output_subject_that_is_no_id_or_unreadable_stays_unlabelled() -> None:
    """A malformed subject never reaches the store's paths, and a corrupt meta.json
    leaves its chip unnamed rather than failing the facets."""
    asked: list[str] = []

    def get(output_id: str) -> tuple[str | None, str]:
        asked.append(output_id)
        if output_id == "d" * 32:
            raise ValueError("meta.json does not validate")
        raise FileNotFoundError(output_id)  # deleted between the lookup and the read

    catalogue = SimpleNamespace(slugs=lambda: [], list_models=lambda slugs: [])
    subjects = [
        Count(value, 1) for value in ("output:*/../x", "output:" + "d" * 32, "output:" + "e" * 32)
    ]
    labels = _subject_labels(subjects, catalogue, SimpleNamespace(name_and_slug=get))  # type: ignore[arg-type]
    assert labels == {}
    assert sorted(asked) == ["d" * 32, "e" * 32]


def test_subjects_with_no_slug_never_read_the_catalogue() -> None:
    """#3856: the Activity page refetches facets often; with only library, project and
    gone output subjects there is no model to name."""

    def unused(*_: object) -> list[str]:
        raise AssertionError("the catalogue was read")

    def gone(output_id: str) -> tuple[str | None, str]:
        raise OutputNotFoundError(output_id)

    catalogue = SimpleNamespace(slugs=unused, list_models=unused)
    subjects = [Count(v, 1) for v in ("library:12", "project:5", "output:" + "a" * 32)]
    labels = _subject_labels(subjects, catalogue, SimpleNamespace(name_and_slug=gone))  # type: ignore[arg-type]
    assert labels == {}


def test_a_failing_catalogue_leaves_the_facets_their_output_names() -> None:
    """#3856: the facets are the activity index's; a catalogue that fails costs only the
    model names, not the filter bar."""

    def broken() -> list[str]:
        raise OSError("catalogue unreadable")

    catalogue = SimpleNamespace(slugs=broken, list_models=lambda slugs: [])
    outputs = SimpleNamespace(name_and_slug=lambda output_id: ("Mine", "demo"))
    subjects = [Count("demo", 2), Count("output:" + "a" * 32, 1)]
    labels = _subject_labels(subjects, catalogue, outputs)  # type: ignore[arg-type]
    assert labels == {"output:" + "a" * 32: "Mine"}


def test_facets_without_agent_tables(client: TestClient) -> None:
    assert client.get("/api/v1/activity/facets").status_code == 200


def test_detail_simple_and_advanced(client: TestClient, seed_activity: SeedActivity) -> None:
    seed_activity(
        id="render:a",
        workflow_id="render-x",
        traceparent="00-abc-def-01",
        source_id="a",
        detail={"request": {"w": 1}, "stage": "split"},
    )
    simple = client.get("/api/v1/activity/render:a").json()
    for hidden in ("workflow_id", "run_id", "traceparent", "source", "source_id", "facets"):
        assert hidden not in simple["row"], hidden
    assert simple["detail"] == {"stage": "split"}
    assert simple["links"] == [{"rel": "job", "label": "Render", "href": "/api/v1/jobs/a"}]
    advanced = client.get("/api/v1/activity/render:a", params={"advanced": "1"}).json()
    assert advanced["row"]["workflow_id"] == "render-x"
    assert advanced["detail"] == {"request": {"w": 1}, "stage": "split"}


def test_paging_is_stable(client: TestClient, seed_activity: SeedActivity) -> None:
    for i in range(5):
        seed_activity(id=f"render:{i}")
    seen: list[str] = []
    cursor = None
    for _ in range(3):
        params: dict[str, str | int] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = client.get("/api/v1/activity", params=params).json()
        seen += ids(page)
        cursor = page["next_cursor"]
    assert seen == [f"render:{i}" for i in reversed(range(5))]
    assert cursor is None


def test_an_agent_operation_is_its_authors(client: TestClient) -> None:
    headers = {"X-ScadBuddy-Agent-Author": "token:t1", "X-ScadBuddy-Agent-Author-Session": "s1"}
    created = client.post(
        "/api/v1/models",
        json={"name": "Widget", "source": "width = 10;\n"},
        headers={**press(), **headers},
    )
    assert created.status_code == 201, created.text
    page = client.get("/api/v1/activity").json()
    operations = [r for r in page["items"] if r["id"].startswith("operation:")]
    assert operations, page
    assert all(r["principal"] == "token:t1" for r in operations)
    assert all(r["session"] == "s1" for r in operations)


def test_a_nul_byte_is_a_400_problem(client: TestClient, seed_activity: SeedActivity) -> None:
    seed_activity(id="render:a", facets={"slug": "box"})
    nul_cursor = base64.urlsafe_b64encode(b"2026-10-10T00:00:00+00:00|render:\x00").decode()
    for path, params in (
        ("/api/v1/activity", {"q": "a\x00b"}),
        ("/api/v1/activity", {"principal": "\x00"}),
        ("/api/v1/activity", {"kind": "render", "facet.slug": "b\x00x"}),
        ("/api/v1/activity", {"cursor": nul_cursor}),
        ("/api/v1/activity/render:%00", {}),
    ):
        response = client.get(path, params=params)
        assert response.status_code == 400, (path, params, response.text)
        assert response.headers["content-type"].startswith("application/problem+json")


def test_advanced_fields_only_with_advanced(
    client: TestClient, seed_activity: SeedActivity
) -> None:
    seed_activity(id="render:a", workflow_id="render-x", detail={"request": {"w": 1}})
    seed_activity(id="render:b", parent_id="render:a", workflow_id="render-y")
    for item in client.get("/api/v1/activity").json()["items"]:
        assert not ADVANCED & item.keys(), item
        assert "request" not in item["detail"]
    children = client.get("/api/v1/activity/render:a").json()["children"]
    assert [c["id"] for c in children] == ["render:b"]
    assert not ADVANCED & children[0].keys()
    shown = client.get("/api/v1/activity", params={"advanced": "1"}).json()["items"]
    assert {r["workflow_id"] for r in shown} == {"render-x", "render-y"}
    advanced = client.get("/api/v1/activity/render:a", params={"advanced": "1"}).json()
    assert advanced["children"][0]["workflow_id"] == "render-y"


def test_a_bad_facet_value_is_a_400_problem(client: TestClient, agent_tables: AgentTables) -> None:
    with psycopg.connect(agent_tables.conninfo, autocommit=True) as conn:
        facets = [
            {"name": "n", "type": "number", "label": "N"},
            {"name": "b", "type": "bool", "label": "B"},
        ]
        conn.execute(
            "INSERT INTO ai_activity_kinds VALUES ('agent.x', 'X', %s, NULL)",
            (json.dumps(facets),),
        )
    for params in (
        {"facet.n": "many"},
        {"facet.n": "nan"},
        {"facet.n": "inf"},
        {"facet.n": "-Infinity"},
        {"facet.b": "maybe"},
    ):
        response = client.get("/api/v1/activity", params={"kind": "agent.x", **params})
        assert response.status_code == 400, (params, response.text)
        assert response.json()["type"] == "https://scadbuddy.dev/problems/bad-parameter-value"
    ok = client.get(
        "/api/v1/activity", params={"kind": "agent.x", "facet.n": "2.5", "facet.b": "true"}
    )
    assert ok.status_code == 200, ok.text
