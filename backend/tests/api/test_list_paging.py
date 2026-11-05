"""#843 -- `GET /models` and `GET /models/{slug}/outputs` page in the backend.

The agent's list tools paged over the whole collection, so walking N pages read every
model (or built every output's detail) N times. With `limit` and `after` the backend
builds only the page asked for; without them each answers the whole list, as the
frontend reads it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import scadbuddy.api.outputs as outputs_api
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.outputs import META_NAME, OutputMeta
from scadbuddy.render.glb import BoundingBox
from tests.support.operations import press

SOURCE = b"width = 10;\ncube(width);\n"


def _create(
    client: TestClient, slug: str, *, name: str | None = None, tags: str = "", description: str = ""
) -> None:
    data = {"tags": tags, "description": description} | ({"name": name} if name else {})
    response = client.post(
        "/api/v1/models",
        files={"file": (f"{slug}.scad", SOURCE, "application/octet-stream")},
        data=data,
        headers=press(),
    )
    assert response.status_code == 201, response.text


def _seed_outputs(paths: DataPaths, slug: str, count: int) -> list[str]:
    """``count`` output records, newest first, as the listing orders them."""
    start = datetime(2026, 10, 1, tzinfo=UTC)
    ids: list[str] = []
    for n in range(count):
        output_id = f"{n:032x}"
        meta = OutputMeta(
            id=output_id,
            slug=slug,
            job_id="j",
            created_at=start + timedelta(minutes=n),
            bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        )
        directory = paths.outputs / slug / output_id
        directory.mkdir(parents=True)
        (directory / META_NAME).write_text(meta.model_dump_json(), encoding="utf-8")
        ids.append(output_id)
    return ids[::-1]


def test_models_page_with_limit_and_after(client: TestClient) -> None:
    for slug in ("alpha", "bravo", "charlie", "delta"):
        _create(client, slug)
    whole = client.get("/api/v1/models")
    assert whole.status_code == 200
    every = [row["slug"] for row in whole.json()]
    assert len(every) >= 4

    first = client.get("/api/v1/models", params={"limit": 2})
    assert first.status_code == 200
    assert [row["slug"] for row in first.json()] == every[:2]
    assert first.headers["x-total-count"] == str(len(every))

    rest = client.get("/api/v1/models", params={"limit": 100, "after": every[1]})
    assert [row["slug"] for row in rest.json()] == every[2:]
    assert rest.headers["x-total-count"] == str(len(every))


def test_models_page_builds_only_its_own_records(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for slug in ("alpha", "bravo", "charlie"):
        _create(client, slug)
    catalogue = client.app.state.scadbuddy.catalogue  # type: ignore[attr-defined]
    built: list[str] = []
    record = catalogue._record

    def counting(slug: str, *args: object) -> object:
        built.append(slug)
        return record(slug, *args)

    monkeypatch.setattr(catalogue, "_record", counting)
    page = client.get("/api/v1/models", params={"limit": 1, "after": "alpha"})
    assert [row["slug"] for row in page.json()] == ["bravo"]
    assert built == ["bravo"]


def test_a_model_left_out_does_not_shorten_its_page(client: TestClient, paths: DataPaths) -> None:
    """A model whose ``model.json`` cannot be read is left out of the listing; the page
    is filled past it, so a short page still means the end of the list."""
    for slug in ("alpha", "bravo", "charlie", "delta"):
        _create(client, slug)
    every = [row["slug"] for row in client.get("/api/v1/models").json()]
    broken = every[1]
    paths.model_source(broken).with_name("model.json").write_text("{", encoding="utf-8")
    listed = [slug for slug in every if slug != broken]

    first = client.get("/api/v1/models", params={"limit": 2})
    assert [row["slug"] for row in first.json()] == listed[:2]
    assert first.headers["x-total-count"] == str(len(every))
    rest = client.get("/api/v1/models", params={"limit": 2, "after": listed[1]})
    assert [row["slug"] for row in rest.json()] == listed[2:4]


def test_models_after_an_unknown_slug_is_a_conflict(client: TestClient) -> None:
    _create(client, "alpha")
    response = client.get("/api/v1/models", params={"after": "gone"})
    assert response.status_code == 409
    assert "gone" in response.json()["detail"]


def _seed_catalogue(client: TestClient) -> None:
    _create(client, "alpha", name="Crème Box", tags="box,kitchen", description="A lidded box")
    _create(client, "bravo", name="Bolt", tags="hardware", description="An M3 bolt")
    _create(client, "charlie", name="Spice box", tags="Box,Kitchen,spice")
    _create(client, "delta", name="Hook", tags="hardware,kitchen")


def _slugs(response: object) -> list[str]:
    return [row["slug"] for row in response.json()]  # type: ignore[attr-defined]


def test_models_search_every_word_in_name_description_or_tags(client: TestClient) -> None:
    """#2290 -- the Models page pages on the server, so its search runs there: every
    word must appear somewhere, case and accents folded, as the page's own did."""
    _seed_catalogue(client)
    found = client.get("/api/v1/models", params={"q": "creme LIDDED", "origin": "mine"})
    assert found.status_code == 200
    assert _slugs(found) == ["alpha"]
    assert found.headers["x-total-count"] == "1"
    spice = client.get("/api/v1/models", params={"q": "box spice", "origin": "mine"})
    assert _slugs(spice) == ["charlie"]


def test_models_tags_are_all_wanted_and_folded(client: TestClient) -> None:
    _seed_catalogue(client)
    both = client.get(
        "/api/v1/models", params=[("tag", "kitchen"), ("tag", "BOX"), ("sort", "name")]
    )
    assert _slugs(both) == ["alpha", "charlie"]
    assert both.headers["x-total-count"] == "2"


def test_models_origin_filter(client: TestClient) -> None:
    _seed_catalogue(client)
    every = client.get("/api/v1/models").json()
    mine = client.get("/api/v1/models", params={"origin": "mine"})
    assert sorted(_slugs(mine)) == sorted(r["slug"] for r in every if r["origin"] == "mine")
    builtin = client.get("/api/v1/models", params={"origin": "builtin"})
    assert all(row["origin"] == "builtin" for row in builtin.json())
    assert builtin.headers["x-total-count"] == str(
        sum(1 for r in every if r["origin"] == "builtin")
    )


def test_models_sorted_by_name_or_newest_update(client: TestClient) -> None:
    _seed_catalogue(client)
    by_name = client.get("/api/v1/models", params={"origin": "mine", "sort": "name"})
    assert _slugs(by_name) == ["bravo", "alpha", "delta", "charlie"]
    newest = client.get("/api/v1/models", params={"origin": "mine", "sort": "updated"})
    rows = newest.json()
    stamps = [(row["updated_at"], row["name"].casefold()) for row in rows]
    assert [s for s, _ in stamps] == sorted((s for s, _ in stamps), reverse=True)


def test_filtered_models_page_with_limit_and_after(client: TestClient) -> None:
    _seed_catalogue(client)
    params = {"tag": "kitchen", "sort": "name"}
    first = client.get("/api/v1/models", params={**params, "limit": 2})
    assert _slugs(first) == ["alpha", "delta"]
    assert first.headers["x-total-count"] == "3"
    rest = client.get("/api/v1/models", params={**params, "limit": 2, "after": "delta"})
    assert _slugs(rest) == ["charlie"]
    gone = client.get("/api/v1/models", params={**params, "after": "bravo"})
    assert gone.status_code == 409


def test_model_facets_count_tags_over_what_the_filters_select(client: TestClient) -> None:
    _seed_catalogue(client)
    every = len(client.get("/api/v1/models").json())
    facets = client.get(
        "/api/v1/model-facets", params=[("origin", "mine"), ("tag", "kitchen"), ("tag", "gone")]
    )
    assert facets.status_code == 200, facets.text
    body = facets.json()
    assert body["total"] == every
    assert body["matched"] == 0
    assert body["tags"] == [{"tag": "gone", "count": 0}, {"tag": "kitchen", "count": 0}]

    kitchen = client.get("/api/v1/model-facets", params={"origin": "mine", "tag": "kitchen"}).json()
    assert kitchen["matched"] == 3
    # Most used first, then by name; a tag's spelling is the first model's that carries it.
    assert kitchen["tags"] == [
        {"tag": "kitchen", "count": 3},
        {"tag": "box", "count": 2},
        {"tag": "hardware", "count": 1},
        {"tag": "spice", "count": 1},
    ]


def test_outputs_page_with_limit_and_after(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create(client, "widget")
    newest_first = _seed_outputs(paths, "widget", 5)

    whole = client.get("/api/v1/models/widget/outputs")
    assert [row["id"] for row in whole.json()] == newest_first
    assert whole.headers["x-total-count"] == "5"

    detailed: list[str] = []
    real = outputs_api.detail

    def counting(store: object, meta: OutputMeta, *args: object, **kwargs: object) -> object:
        detailed.append(meta.id)
        return real(store, meta, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(outputs_api, "detail", counting)
    page = client.get(
        "/api/v1/models/widget/outputs", params={"limit": 2, "after": newest_first[0]}
    )
    assert page.status_code == 200
    assert [row["id"] for row in page.json()] == newest_first[1:3]
    assert page.headers["x-total-count"] == "5"
    # Only the page's own outputs were built into details.
    assert detailed == newest_first[1:3]


def test_outputs_after_an_unknown_id_is_a_conflict(client: TestClient, paths: DataPaths) -> None:
    _create(client, "widget")
    _seed_outputs(paths, "widget", 1)
    response = client.get("/api/v1/models/widget/outputs", params={"after": "f" * 32})
    assert response.status_code == 409


@pytest.mark.parametrize("limit", [0, 501])
def test_a_limit_out_of_range_is_refused(client: TestClient, limit: int) -> None:
    assert client.get("/api/v1/models", params={"limit": limit}).status_code == 422
