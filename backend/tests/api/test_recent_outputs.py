"""`GET /outputs` (#4777, order operations spec 2026-11-07 §3.1): every model's saved
outputs, newest first, filtered by time, model, tag, text and whether an order names
them, paged as `GET /models/{slug}/outputs`; and the index's repair at start."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from tests.api.conftest import wait_for_job
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import add_item
from tests.api.test_versions import SLUG, upload
from tests.support.operations import press

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

PLATE = 'width = 10;\nlabel = "hi";\ncube(width);\n'


def state_of(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def saved(client: TestClient, slug: str, params: dict[str, Any], name: str | None) -> str:
    job_id: str = client.post(f"/api/v1/models/{slug}/render", json={"params": params}).json()[
        "job_id"
    ]
    wait_for_job(client, job_id)
    body: dict[str, Any] = {"job_id": job_id}
    if name is not None:
        body["name"] = name
    created = client.post(f"/api/v1/models/{slug}/outputs", json=body, headers=press())
    assert created.status_code == 201, created.text
    output_id: str = created.json()["id"]
    return output_id


def listed(client: TestClient, **query: Any) -> list[str]:
    response = client.get("/api/v1/outputs", params=query)
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()]


@pytest.fixture
def three(client: TestClient) -> tuple[str, str, str]:
    """Two keychains and a plate, saved in that order."""
    upload(client)
    plate = client.post("/api/v1/models", json={"name": "Plate", "source": PLATE}, headers=press())
    assert plate.status_code == 201, plate.text
    tagged = client.patch(
        f"/api/v1/models/{plate.json()['slug']}", json={"tags": ["Kitchen"]}, headers=press()
    )
    assert tagged.status_code == 200, tagged.text
    first = saved(client, SLUG, {"width": 12}, "Door sign")
    second = saved(client, SLUG, {"width": 14}, None)
    third = saved(client, plate.json()["slug"], {"width": 7}, "Coaster")
    return first, second, third


def test_every_models_outputs_are_listed_newest_first(
    client: TestClient, three: tuple[str, str, str]
) -> None:
    first, second, third = three
    response = client.get("/api/v1/outputs")
    assert response.status_code == 200, response.text
    assert response.headers["X-Total-Count"] == "3"
    rows = response.json()
    assert [row["id"] for row in rows] == [third, second, first]
    # The rows are `OutputDetail`s, as the per-model list's.
    assert rows[2]["params"] == {"width": 12, "label": "hi"}
    assert rows[2]["name"] == "Door sign" and rows[2]["slug"] == SLUG
    page = client.get("/api/v1/outputs", params={"limit": 2})
    assert [row["id"] for row in page.json()] == [third, second]
    assert listed(client, limit=2, after=second) == [first]
    gone = client.get("/api/v1/outputs", params={"after": "f" * 32})
    assert gone.status_code == 409, gone.text


def test_the_filters_select_by_model_tag_text_and_time(
    client: TestClient, three: tuple[str, str, str]
) -> None:
    first, second, third = three
    assert listed(client, slug=SLUG) == [second, first]
    plate = client.get(f"/api/v1/outputs/{third}").json()["slug"]
    assert listed(client, slug=[SLUG, plate]) == [third, second, first]
    assert listed(client, tag="kitchen") == [third]
    assert listed(client, tag="garden") == []
    # The name, or the parameters that differ from the template's defaults: `label`
    # is at its default, so it is not text the search finds.
    assert listed(client, q="door") == [first]
    assert listed(client, q="width=14") == [second]
    assert listed(client, q="label") == []
    now = datetime.now(UTC)
    assert listed(client, **{"from": (now - timedelta(hours=1)).isoformat()}) == [
        third,
        second,
        first,
    ]
    assert listed(client, **{"from": (now + timedelta(hours=1)).isoformat()}) == []
    assert listed(client, to=(now - timedelta(hours=1)).isoformat()) == []


def test_ordered_tells_outputs_an_order_names(
    client: TestClient, three: tuple[str, str, str]
) -> None:
    first, second, third = three
    order = create_order(client, create_customer(client)["id"])
    add_item(client, order["id"], output_id=second, quantity=1)
    assert listed(client, ordered="yes") == [second]
    assert listed(client, ordered="no") == [third, first]
    assert client.get("/api/v1/outputs", params={"ordered": "maybe"}).status_code == 422


def test_renames_deletes_and_the_start_keep_the_index_in_step(
    client: TestClient, three: tuple[str, str, str]
) -> None:
    first, second, third = three
    renamed = client.patch(f"/api/v1/outputs/{second}", json={"name": "Gate tag"}, headers=press())
    assert renamed.status_code == 200, renamed.text
    assert listed(client, q="gate") == [second]
    deleted = client.delete(f"/api/v1/outputs/{third}", headers=press())
    assert deleted.status_code in (200, 204), deleted.text
    assert listed(client) == [second, first]
    # An output with no row (saved before the index, or its write failed) is added by
    # the start's sync; a row whose output went with its model's directory is dropped,
    # and never listed meanwhile.
    state = state_of(client)
    assert state.outputs.index is not None
    state.outputs.index.forget(first)
    assert listed(client) == [second]
    assert state.outputs.index.sync(state.outputs) == (1, 0)
    assert listed(client) == [second, first]
