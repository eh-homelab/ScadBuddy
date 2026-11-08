"""A visitor's renders on the CRUD side (#5282, spec 2026-11-02 §5.4, plan Task 7):
``POST …/storefront/renders`` checks the inputs against the listing, counts the render
against the visitor's and the client's limits (a shared render costs nothing) and renders
through `RenderService` with ``reuse_finished``; ``GET …/renders/{id}``, its preview and its
rough estimate answer only the visitor who asked for that render."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.orders.storefront.renders import RENDER_FAILED, store_render
from scadbuddy.render.job_models import Job, QueueFullError, now
from scadbuddy.storefront.wire import CLIENT_HEADER, STOREFRONT_PROBLEMS, VISITOR_HEADER
from tests.api.conftest import set_fake_env
from tests.api.test_storefront_listings import (  # noqa: F401  (fixtures)
    BOOK,
    _state,
    client,
    create,
    kek_file,
    listing_body,
    publish,
    template,
)
from tests.api.test_storefront_public import CRUD, TOKEN_A, bearer

pytestmark = pytest.mark.requires_postgres

TOKEN_B = "B" * 43
TOKEN_C = "C" * 43
GLB = "model/gltf-binary"


class CrudCaller:
    """The edge's calls: its credential, a visitor id and the browser's address."""

    def __init__(self, api: TestClient) -> None:
        self.api = api
        self.headers = bearer(api)

    def _headers(self, token: str, address: str, key: str | None) -> dict[str, str]:
        sent = {**self.headers, VISITOR_HEADER: token, CLIENT_HEADER: address}
        if key is not None:
            sent["Idempotency-Key"] = key
        return sent

    def get(self, path: str, *, token: str = TOKEN_A, address: str = "203.0.113.1") -> Any:
        return self.api.get(CRUD + path, headers=self._headers(token, address, None))

    def post(
        self,
        path: str,
        *,
        token: str = TOKEN_A,
        json: Any = None,
        content: str | None = None,
        key: str | None = None,
        address: str = "203.0.113.1",
    ) -> Any:
        headers = self._headers(token, address, key)
        if content is not None:
            headers["Content-Type"] = "application/json"
            return self.api.post(CRUD + path, content=content, headers=headers)
        return self.api.post(CRUD + path, json=json, headers=headers)


@pytest.fixture
def crud(client: TestClient) -> CrudCaller:  # noqa: F811
    return CrudCaller(client)


@pytest.fixture
def dense_red(client: TestClient) -> str:  # noqa: F811
    made = client.post(
        f"{BOOK}/filaments",
        json={
            "material": "PLA",
            "colour": "#FF0000",
            "cost_per_kg": "20",
            "density_g_cm3": "1.24",
        },
    )
    assert made.status_code in (200, 201), made.text
    filament_id: str = made.json()["id"]
    return filament_id


@pytest.fixture
def listing(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
    dense_red: str,
) -> dict[str, Any]:
    """A published listing whose width a visitor picks; every render may run at once,
    and a plate costs a setup fee."""
    settings = client.put(
        "/api/v1/settings",
        json={"storefront_render_concurrency": 20, "pricing_setup_fee": "1.50"},
    )
    assert settings.status_code == 200, settings.text
    body = listing_body(
        template, dense_red, params={"width": {"kind": "number", "min": 20, "max": 80, "step": 1}}
    )
    return publish(client, create(client, body)["id"])


def render_body(
    listing: dict[str, Any], values: dict[str, Any], colours: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "listing_id": listing["id"],
        "listing_updated_at": listing["updated_at"],
        "values": values,
        "colours": colours or {},
    }


def a_render(
    crud: CrudCaller,
    token: str,
    listing: dict[str, Any],
    values: dict[str, Any],
    *,
    key: str | None = None,
    address: str = "203.0.113.1",
) -> dict[str, Any]:
    r = crud.post(
        "/renders",
        token=token,
        json=render_body(listing, values),
        key=key or uuid.uuid4().hex,
        address=address,
    )
    assert r.status_code == 202, r.text
    job: dict[str, Any] = r.json()
    return job


def wait_done(crud: CrudCaller, token: str, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while True:
        r = crud.get(f"/renders/{job_id}", token=token)
        assert r.status_code == 200, r.text
        job: dict[str, Any] = r.json()
        if job["status"] not in ("pending", "running"):
            return job
        assert time.monotonic() < deadline, "the render never settled"
        time.sleep(0.1)


def render_until_done(
    crud: CrudCaller, token: str, listing: dict[str, Any], values: dict[str, Any]
) -> dict[str, Any]:
    job = wait_done(crud, token, a_render(crud, token, listing, values)["id"])
    assert job["status"] == "done", job
    return job


def counted_rows(crud: CrudCaller, token: str, *, counted: bool = True) -> int:
    digest = hashlib.sha256(token.encode()).hexdigest()
    with _state(crud.api).projection.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM storefront_renders r"
            " JOIN storefront_visitors v ON v.id = r.visitor_id"
            " WHERE v.token_sha256 = %s AND r.counted = %s",
            (digest, counted),
        ).fetchone()
    assert row is not None
    n: int = row["n"]
    return n


def test_a_render_answers_what_the_preview_reads(crud: CrudCaller, listing: dict[str, Any]) -> None:
    job = render_until_done(crud, TOKEN_A, listing, {"width": 40})
    assert set(job) == {
        "id",
        "status",
        "stage",
        "plates",
        "bbox_mm",
        "colors",
        "preview_url",
        "error",
    }
    assert job["preview_url"] == f"/api/store/v1/renders/{job['id']}/preview.glb"
    assert job["colors"] == ["#FF0000"] and job["error"] is None
    assert counted_rows(crud, TOKEN_A) == 1


def test_the_preview_streams_the_glb(crud: CrudCaller, listing: dict[str, Any]) -> None:
    job = render_until_done(crud, TOKEN_A, listing, {"width": 40})
    r = crud.get(f"/renders/{job['id']}/preview.glb")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == GLB
    assert r.content[:4] == b"glTF"


def test_two_visitors_with_the_same_inputs_share_one_render(
    crud: CrudCaller, listing: dict[str, Any]
) -> None:
    ja = render_until_done(crud, TOKEN_A, listing, {"width": 40})
    jb = a_render(crud, TOKEN_B, listing, {"width": 40})
    assert jb["id"] == ja["id"] and counted_rows(crud, TOKEN_B) == 0
    assert counted_rows(crud, TOKEN_B, counted=False) == 1


def test_joining_an_open_render_is_not_counted(
    crud: CrudCaller, listing: dict[str, Any], settings: Settings, tmp_path: Path
) -> None:
    """Review Focus 3: the fake openscad holds A's render, so B joins it open."""
    started, release = tmp_path / "started", tmp_path / "release"
    set_fake_env(
        Path(settings.openscad).parent,
        "FAKE_HOLD",
        {"started": str(started), "release": str(release)},
    )
    try:
        ja = a_render(crud, TOKEN_A, listing, {"width": 41})
        deadline = time.monotonic() + 30
        while not started.exists():
            assert time.monotonic() < deadline, "the render never started"
            time.sleep(0.05)
        jb = a_render(crud, TOKEN_B, listing, {"width": 41})
        assert jb["id"] == ja["id"] and jb["status"] in ("pending", "running")
        assert counted_rows(crud, TOKEN_B) == 0
        assert crud.get(f"/renders/{jb['id']}", token=TOKEN_B).status_code == 200
    finally:
        release.touch()
    assert wait_done(crud, TOKEN_B, jb["id"])["status"] == "done"


def test_a_retried_render_is_counted_once(crud: CrudCaller, listing: dict[str, Any]) -> None:
    """Review Focus 4: the edge sends a lost request again with the same key."""
    first = a_render(crud, TOKEN_A, listing, {"width": 42}, key="k-1")
    again = a_render(crud, TOKEN_A, listing, {"width": 42}, key="k-1")
    assert again["id"] == first["id"] and counted_rows(crud, TOKEN_A) == 1


def test_over_the_limit_is_429_with_retry_after(crud: CrudCaller, listing: dict[str, Any]) -> None:
    for w in (30, 31, 32):
        a_render(crud, TOKEN_A, listing, {"width": w})
    r = crud.post("/renders", token=TOKEN_A, json=render_body(listing, {"width": 33}), key="k-4")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "render_limited"
    assert r.json()["limit"] == "per_minute"
    # Another cookie on the same address is the same client.
    r = crud.post("/renders", token=TOKEN_C, json=render_body(listing, {"width": 34}), key="k-5")
    assert r.status_code == 429


def test_a_render_needs_an_idempotency_key(crud: CrudCaller, listing: dict[str, Any]) -> None:
    r = crud.post("/renders", json=render_body(listing, {"width": 40}))
    assert r.status_code == 428
    assert counted_rows(crud, TOKEN_A) == 0


def test_another_visitors_render_is_404(crud: CrudCaller, listing: dict[str, Any]) -> None:
    job = render_until_done(crud, TOKEN_A, listing, {"width": 50})
    for path in ("", "/preview.glb", "/estimate"):
        r = crud.get(f"/renders/{job['id']}{path}", token=TOKEN_B)
        assert r.status_code == 404, path
        assert r.json()["type"] == STOREFRONT_PROBLEMS + "render_not_found"
    assert crud.get(f"/renders/{'0' * 32}", token=TOKEN_A).status_code == 404


def test_the_estimate_is_a_price_without_machine_time(
    crud: CrudCaller, listing: dict[str, Any]
) -> None:
    job = render_until_done(crud, TOKEN_A, listing, {"width": 40})
    r = crud.get(f"/renders/{job['id']}/estimate")
    assert r.status_code == 200, r.text
    est = r.json()
    assert est["estimate"] is True and est["price_minor"] > est["filament_minor"] > 0
    assert "machine_time" not in [line["kind"] for line in est["lines"]]
    # Analysed once, for every visitor of the render.
    with _state(crud.api).projection.pool.connection() as conn:
        rows = conn.execute(
            "SELECT count(*) AS n FROM storefront_job_parts WHERE job_id = %s", (job["id"],)
        ).fetchone()
    assert rows is not None and rows["n"] == 1
    a_render(crud, TOKEN_B, listing, {"width": 40})
    assert crud.get(f"/renders/{job['id']}/estimate", token=TOKEN_B).json() == est


@pytest.fixture
def corrupt_done_job(crud: CrudCaller, listing: dict[str, Any]) -> str:
    """A done render whose 3MF in this process's cache is not a 3MF."""
    job = render_until_done(crud, TOKEN_A, listing, {"width": 60})
    state = _state(crud.api)
    found = state.render.store.read(job["id"])
    assert found.result is not None and crud.api.portal is not None
    from scadbuddy.store.cache import materialize_result

    crud.api.portal.call(materialize_result, state.store.blobs, found.result)
    (state.paths.root / found.result.model_3mf).write_bytes(b"not a 3mf")
    job_id: str = job["id"]
    return job_id


def test_a_3mf_that_cannot_be_analysed_is_422(crud: CrudCaller, corrupt_done_job: str) -> None:
    r = crud.get(f"/renders/{corrupt_done_job}/estimate", token=TOKEN_A)
    assert r.status_code == 422, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "estimate_unavailable"
    assert "not a 3mf" not in r.text and "blobs/" not in r.text


def test_an_open_render_has_no_estimate(
    crud: CrudCaller, listing: dict[str, Any], settings: Settings, tmp_path: Path
) -> None:
    started, release = tmp_path / "started", tmp_path / "release"
    set_fake_env(
        Path(settings.openscad).parent,
        "FAKE_HOLD",
        {"started": str(started), "release": str(release)},
    )
    try:
        job = a_render(crud, TOKEN_A, listing, {"width": 61})
        r = crud.get(f"/renders/{job['id']}/estimate")
        assert r.status_code == 422, r.text
        assert r.json()["type"] == STOREFRONT_PROBLEMS + "estimate_unavailable"
    finally:
        release.touch()
    wait_done(crud, TOKEN_A, job["id"])


@pytest.mark.parametrize(
    "number",
    ["1" + "0" * 400, "-1" + "0" * 400, "1" + "0" * 399, "NaN", "Infinity", "-Infinity"],
)
def test_a_hostile_number_is_outside_the_listing(
    crud: CrudCaller, listing: dict[str, Any], number: str
) -> None:
    body = json.dumps(render_body(listing, {})).replace(
        '"values": {}', f'"values": {{"width": {number}}}'
    )
    r = crud.post("/renders", content=body, key=uuid.uuid4().hex)
    assert r.status_code == 422, r.text
    problem = r.json()
    assert problem["type"] == STOREFRONT_PROBLEMS + "input_outside_listing"
    assert (problem["name"], problem["reason"]) == ("width", "range")
    assert counted_rows(crud, TOKEN_A) == 0


@pytest.mark.parametrize(
    ("values", "colours", "name", "reason"),
    [
        ({"width": 81}, {}, "width", "range"),
        ({"height": 9}, {}, "height", "locked"),
        ({"depth": 9}, {}, "depth", "unknown"),
        ({}, {"0": "no-such-filament"}, "colours.0", "colour"),
    ],
)
def test_a_value_the_listing_does_not_allow_is_422(
    crud: CrudCaller,
    listing: dict[str, Any],
    values: dict[str, Any],
    colours: dict[str, str],
    name: str,
    reason: str,
) -> None:
    r = crud.post("/renders", json=render_body(listing, values, colours), key=uuid.uuid4().hex)
    assert r.status_code == 422, r.text
    problem = r.json()
    assert problem["type"] == STOREFRONT_PROBLEMS + "input_outside_listing"
    assert (problem["name"], problem["reason"]) == (name, reason)


@pytest.mark.parametrize("body", ["[]", '{"listing_id": 1}', "not json"])
def test_a_body_that_is_not_a_render_is_422(
    crud: CrudCaller, listing: dict[str, Any], body: str
) -> None:
    r = crud.post("/renders", content=body, key=uuid.uuid4().hex)
    assert r.status_code == 422, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "render_body_invalid"


def test_a_changed_listing_is_409(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,
    listing: dict[str, Any],
) -> None:
    stale = {**listing, "updated_at": "2020-01-01T00:00:00Z"}
    r = crud.post("/renders", json=render_body(stale, {"width": 40}), key=uuid.uuid4().hex)
    assert r.status_code == 409, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "listing_changed"
    assert client.post(f"/api/v1/storefront/listings/{listing['id']}/unpublish").status_code == 200
    r = crud.post("/renders", json=render_body(listing, {"width": 40}), key=uuid.uuid4().hex)
    assert r.status_code == 404
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "listing_not_found"


def test_a_full_queue_is_429_and_costs_nothing(
    crud: CrudCaller, listing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def full(*_args: Any, **_kwargs: Any) -> Job:
        raise QueueFullError(9, retry_after=7)

    monkeypatch.setattr(_state(crud.api).render, "submit", full)
    r = crud.post("/renders", json=render_body(listing, {"width": 70}), key=uuid.uuid4().hex)
    assert r.status_code == 429, r.text
    assert r.headers["retry-after"] == "7"
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "render_queue_full"
    assert counted_rows(crud, TOKEN_A) == 0


def test_a_failed_render_says_a_fixed_text() -> None:
    failed = Job(
        id="j",
        slug="demo",
        state="failed",
        created_at=now(),
        error="openscad exited 1: /data/models/demo/model.scad line 3",
        log_tail=["ERROR: /data/models/demo/model.scad, line 3", "Traceback (most recent call)"],
    )
    shown = store_render(failed)
    assert shown.error == RENDER_FAILED
    assert "/data" not in shown.model_dump_json()


def test_the_crud_side_lists_the_area(crud: CrudCaller) -> None:
    assert "renders" in crud.get("/areas").json()["areas"]
