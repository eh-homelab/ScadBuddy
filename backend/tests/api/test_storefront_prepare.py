"""Every colour combination of a listing, prepared from the back office (#6237):
``GET``/``POST …/storefront/listings/{id}/prepare``. A visitor's render of a prepared
combination reuses its job."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.core.settings import Settings
from scadbuddy.orders.storefront.prepare import RunState, TemporalPreparer
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS
from tests.api.conftest import set_fake_env
from tests.api.test_storefront_listings import (  # noqa: F401  (fixtures)
    BOOK,
    LISTINGS,
    _state,
    client,
    create,
    kek_file,
    listing_body,
    pla_red,
    publish,
    template,
)
from tests.api.test_storefront_public import TOKEN_A
from tests.api.test_storefront_renders import (  # noqa: F401  (fixtures)
    TOKEN_B,
    CrudCaller,
    a_render,
    counted_rows,
    crud,
)

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def pla_blue(client: TestClient) -> str:  # noqa: F811
    made = client.post(
        f"{BOOK}/filaments", json={"material": "PLA", "colour": "#0000FF", "cost_per_kg": "20"}
    )
    assert made.status_code in (200, 201), made.text
    filament_id: str = made.json()["id"]
    return filament_id


@pytest.fixture
def two_colours(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
    pla_red: str,  # noqa: F811
    pla_blue: str,
) -> dict[str, Any]:
    """A published listing whose colour a visitor picks from red and blue; its width is
    offered too, and stays at its default."""
    settings = client.put("/api/v1/settings", json={"storefront_render_concurrency": 20})
    assert settings.status_code == 200, settings.text
    body = listing_body(
        template,
        pla_red,
        colours=[pla_red, pla_blue],
        params={
            "width": {"kind": "number", "min": 20, "max": 80, "step": 1},
            "colour": {"kind": "choice", "choices": ["#ff0000", "#FF0000", "#0000FF"]},
        },
    )
    return publish(client, create(client, body)["id"])


def prepare_path(listing: dict[str, Any]) -> str:
    return f"{LISTINGS}/{listing['id']}/prepare"


def wait_ready(client: TestClient, listing: dict[str, Any], count: int) -> dict[str, Any]:  # noqa: F811
    deadline = time.monotonic() + 90
    while True:
        r = client.get(prepare_path(listing))
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        if body["ready"] + body["failed"] == count and not body["running"]:
            return body
        assert time.monotonic() < deadline, f"the prepared renders never settled: {body}"
        time.sleep(0.2)


def job_ids(client: TestClient, slug: str) -> set[str]:  # noqa: F811
    with _state(client).projection.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id FROM render_jobs WHERE slug = %s AND state = 'done'", (slug,)
        ).fetchall()
    return {row["id"] for row in rows}


def test_the_count_is_shown_before_the_run(
    client: TestClient,  # noqa: F811
    two_colours: dict[str, Any],
) -> None:
    r = client.get(prepare_path(two_colours))
    assert r.status_code == 200, r.text
    # Red twice in two cases is one colour.
    assert r.json() == {
        "count": 2,
        "cap": 256,
        "ready": 0,
        "rendering": 0,
        "failed": 0,
        "running": False,
        "failed_to_start": 0,
    }


def test_a_failed_run_read_is_not_an_error(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
    pla_red: str,  # noqa: F811
    two_colours: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#6299 review: while Temporal does not answer, the run reads as not running, and a
    listing with no colour to prepare never asks Temporal."""
    asked: list[str] = []

    async def down(self: TemporalPreparer, listing_id: str) -> RunState:
        asked.append(listing_id)
        raise RPCError("connection refused", RPCStatusCode.UNAVAILABLE, b"")

    monkeypatch.setattr(TemporalPreparer, "run_state", down)
    r = client.get(prepare_path(two_colours))
    assert r.status_code == 200, r.text
    assert (r.json()["count"], r.json()["running"]) == (2, False)
    assert asked == [two_colours["id"]]
    plain = publish(client, create(client, listing_body(template, pla_red))["id"])
    r = client.get(prepare_path(plain))
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0
    assert asked == [two_colours["id"]]


def test_a_visitors_render_reuses_the_prepared_job(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,  # noqa: F811
    two_colours: dict[str, Any],
) -> None:
    r = client.post(prepare_path(two_colours))
    assert r.status_code == 200, r.text
    assert r.json() == {"count": 2, "started": True}
    settled = wait_ready(client, two_colours, 2)
    assert (settled["ready"], settled["failed_to_start"]) == (2, 0)
    prepared = job_ids(client, two_colours["slug"])
    assert len(prepared) == 2
    # The page's first load sends nothing; a pick sends the colour the page shows.
    first = a_render(crud, TOKEN_A, two_colours, {})
    picked = a_render(crud, TOKEN_B, two_colours, {"colour": "#0000FF"})
    assert {first["id"], picked["id"]} == prepared
    assert first["status"] == picked["status"] == "done"
    # A shared render costs the visitor nothing.
    assert counted_rows(crud, TOKEN_A) == counted_rows(crud, TOKEN_B) == 0
    # A second prepare, after the first run ended, reuses both and renders nothing.
    again = client.post(prepare_path(two_colours))
    assert again.json() == {"count": 2, "started": True}
    wait_ready(client, two_colours, 2)
    assert job_ids(client, two_colours["slug"]) == prepared


def test_prepare_again_renders_a_failed_combination_again(
    client: TestClient,  # noqa: F811
    two_colours: dict[str, Any],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """#6299 review: each run makes its own claim, so a render that failed in one run is
    rendered again by the next, not answered with the first run's failure."""
    failing = tmp_path / "fail-blue"
    failing.touch()
    set_fake_env(
        Path(settings.openscad).parent, "FAKE_FAIL_WHILE", {"arg": "0000FF", "file": str(failing)}
    )
    assert client.post(prepare_path(two_colours)).status_code == 200
    first = wait_ready(client, two_colours, 2)
    assert (first["ready"], first["failed"]) == (1, 1)
    failing.unlink()
    again = client.post(prepare_path(two_colours))
    assert again.json() == {"count": 2, "started": True}
    second = wait_ready(client, two_colours, 2)
    assert (second["ready"], second["failed"]) == (2, 0)


def test_a_visitors_render_during_the_run_has_the_default_priority(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,  # noqa: F811
    two_colours: dict[str, Any],
) -> None:
    """#6299 review: a visitor's render of a prepared combination joins its execution, so
    the prepared run must not be below the default priority, or the visitor waits behind
    the batch."""
    assert client.post(prepare_path(two_colours)).status_code == 200
    picked = a_render(crud, TOKEN_A, two_colours, {"colour": "#0000FF"})
    state = _state(client)
    with state.projection.pool.connection() as conn:
        row = conn.execute(
            "SELECT workflow_id FROM render_jobs WHERE id = %s", (picked["id"],)
        ).fetchone()
    assert row is not None
    handle = state.render.client.get_workflow_handle(row["workflow_id"])
    assert client.portal is not None
    described = client.portal.call(handle.describe)
    info = described.raw_description.workflow_execution_info
    # 0 is Temporal's default key: the run set none.
    assert info.priority.priority_key == 0
    wait_ready(client, two_colours, 2)


def test_over_the_cap_is_refused_with_the_count(
    client: TestClient,  # noqa: F811
    two_colours: dict[str, Any],
) -> None:
    assert client.put("/api/v1/settings", json={"storefront_prepare_cap": 1}).status_code == 200
    shown = client.get(prepare_path(two_colours)).json()
    assert shown == {
        "count": 2,
        "cap": 1,
        "ready": 0,
        "rendering": 0,
        "failed": 0,
        "running": False,
        "failed_to_start": 0,
    }
    r = client.post(prepare_path(two_colours))
    assert r.status_code == 422, r.text
    problem = r.json()
    assert problem["type"] == STOREFRONT_PROBLEMS + "listing_combinations_over_cap"
    assert (problem["count"], problem["cap"]) == (2, 1)
    assert job_ids(client, two_colours["slug"]) == set()


def test_a_listing_with_no_colour_parameter_is_refused(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
    pla_red: str,  # noqa: F811
) -> None:
    listing = publish(client, create(client, listing_body(template, pla_red))["id"])
    assert client.get(prepare_path(listing)).json()["count"] == 0
    r = client.post(prepare_path(listing))
    assert r.status_code == 422, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "listing_no_colours"


def test_an_unpublished_listing_is_409(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
    pla_red: str,  # noqa: F811
) -> None:
    listing = create(client, listing_body(template, pla_red))
    for r in (client.get(prepare_path(listing)), client.post(prepare_path(listing))):
        assert r.status_code == 409, r.text
        assert r.json()["type"] == STOREFRONT_PROBLEMS + "listing_not_published"
    assert client.post(f"{LISTINGS}/nope/prepare").status_code == 404
