"""A visitor's renders on the CRUD side (#5282, spec 2026-11-02 §5.4, plan Task 7):
``POST …/storefront/renders`` checks the inputs against the listing, counts the render
against the visitor's and the client's limits (a shared render costs nothing) and renders
through `RenderService` with ``reuse_finished``; ``GET …/renders/{id}``, its preview and its
price answer only the visitor who asked for that render. The price is a slice's (#6153),
through the back office's quote path; the slicer here is a fake in `PreviewSlicer`'s place."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.storefront_slice import ProfileSpool
from scadbuddy.bambuddy.preview import PreviewStarted
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import SettingsPatch
from scadbuddy.orders import operations as order_operations
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.quote import SlicedPlate, SliceQuoteRequest
from scadbuddy.orders.storefront import renders as renders_module
from scadbuddy.orders.storefront.renders import RENDER_FAILED, store_render
from scadbuddy.orders.storefront.slice_profile import StorefrontSliceProfile
from scadbuddy.render.job_models import Job, QueueFullError, now
from scadbuddy.render.jobs import SnapshotPendingError, SnapshotUnavailableError
from scadbuddy.store.cache import materialize_result
from scadbuddy.store.content import StoreFullError
from scadbuddy.storefront.wire import CLIENT_HEADER, STOREFRONT_PROBLEMS, VISITOR_HEADER
from scadbuddy.workflows.commands import (
    CommandClosedError,
    CommandStillAcceptingError,
    TemporalUnavailableError,
    TemporalUnreachableError,
)
from tests.api.conftest import set_fake_env
from tests.api.test_storefront_listings import (  # noqa: F401  (fixtures)
    BOOK,
    LISTINGS,
    SOURCE,
    _state,
    client,
    create,
    kek_file,
    listing_body,
    publish,
    template,
)
from tests.api.test_storefront_public import CRUD, TOKEN_A, bearer
from tests.support.operations import press
from tests.support.sliced3mf import sliced_3mf

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
        "values",
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


def test_a_render_retried_after_it_finished_stays_counted(
    crud: CrudCaller, listing: dict[str, Any]
) -> None:
    """Review of #5758, Blocking 1: a finished job answers a second submit as reused, but
    a replayed key keeps the row as its first answer left it."""
    first = a_render(crud, TOKEN_A, listing, {"width": 43}, key="k-done")
    wait_done(crud, TOKEN_A, first["id"])
    again = a_render(crud, TOKEN_A, listing, {"width": 43}, key="k-done")
    assert again["id"] == first["id"]
    assert counted_rows(crud, TOKEN_A) == 1 and counted_rows(crud, TOKEN_A, counted=False) == 0


def test_a_shared_guess_retried_is_not_counted_again(
    crud: CrudCaller, listing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #5758, Blocking 1: a free row that became a run of its own was counted
    once; the same key sent again at the limit neither counts it again nor withdraws it."""
    for w in (30, 31):
        a_render(crud, TOKEN_A, listing, {"width": w})
    monkeypatch.setattr(renders_module, "_shared", lambda *_args: True)
    first = a_render(crud, TOKEN_A, listing, {"width": 44}, key="k-own")
    assert counted_rows(crud, TOKEN_A) == 3
    again = a_render(crud, TOKEN_A, listing, {"width": 44}, key="k-own")
    assert again["id"] == first["id"]
    assert counted_rows(crud, TOKEN_A) == 3 and counted_rows(crud, TOKEN_A, counted=False) == 0
    assert wait_done(crud, TOKEN_A, first["id"])["status"] == "done"


def test_a_replay_whose_job_is_gone_is_410_and_starts_nothing(
    crud: CrudCaller, listing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security review of 52079204d: a replayed key whose job was pruned never submits
    again, so a shared (free) row cannot start an uncounted run."""
    first = a_render(crud, TOKEN_A, listing, {"width": 45}, key="k-gone")
    wait_done(crud, TOKEN_A, first["id"])
    state = _state(crud.api)
    state.projection.delete(first["id"])
    render = state.render

    async def never(*_args: Any, **_kwargs: Any) -> tuple[Job, bool]:
        raise AssertionError("a replay of a gone job submitted again")

    monkeypatch.setattr(render, "submit_answer", never)
    r = crud.post("/renders", json=render_body(listing, {"width": 45}), key="k-gone")
    assert r.status_code == 410, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "render_expired"


def lost_before_attach(crud: CrudCaller, key: str) -> None:
    """The row of ``key`` as a first answer lost before `attach` left it, guessed free."""
    with _state(crud.api).projection.pool.connection() as conn:
        conn.execute(
            "UPDATE storefront_renders SET job_id = NULL, counted = false"
            " WHERE id = (SELECT (body->>'row')::bigint FROM crud_requests WHERE key = %s)",
            (key,),
        )


def own_runs(monkeypatch: pytest.MonkeyPatch, crud: CrudCaller) -> list[Job]:
    """Every submit answers as a run of its own (not coalesced)."""
    render = _state(crud.api).render
    real = render.submit_answer
    seen: list[Job] = []

    async def own(*args: Any, **kwargs: Any) -> tuple[Job, bool]:
        job, _ = await real(*args, **kwargs)
        seen.append(job)
        return job, False

    monkeypatch.setattr(render, "submit_answer", own)
    return seen


def test_a_free_replay_that_runs_on_its_own_is_counted(
    crud: CrudCaller, listing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security review of 52079204d: a free row replayed before `attach` whose submit is
    a run of its own counts as any run does."""
    first = a_render(crud, TOKEN_A, listing, {"width": 46}, key="k-lost")
    lost_before_attach(crud, "k-lost")
    assert counted_rows(crud, TOKEN_A) == 0
    own_runs(monkeypatch, crud)
    again = a_render(crud, TOKEN_A, listing, {"width": 46}, key="k-lost")
    assert again["id"] == first["id"]
    assert counted_rows(crud, TOKEN_A) == 1 and counted_rows(crud, TOKEN_A, counted=False) == 0
    wait_done(crud, TOKEN_A, first["id"])


def test_a_free_replay_over_the_limit_is_429_and_withdrawn(
    crud: CrudCaller, listing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    a_render(crud, TOKEN_A, listing, {"width": 47}, key="k-lost-2")
    lost_before_attach(crud, "k-lost-2")
    for w in (30, 31, 32):
        a_render(crud, TOKEN_A, listing, {"width": w})
    withdrawn: list[str] = []
    render = _state(crud.api).render

    async def withdraw(job_id: str, **_kwargs: Any) -> None:
        withdrawn.append(job_id)

    monkeypatch.setattr(render, "withdraw", withdraw)
    seen = own_runs(monkeypatch, crud)
    r = crud.post("/renders", json=render_body(listing, {"width": 47}), key="k-lost-2")
    assert r.status_code == 429, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "render_limited"
    assert withdrawn == [seen[0].id] and ledger_rows(crud, "k-lost-2") == 0
    assert counted_rows(crud, TOKEN_A) == 3 and counted_rows(crud, TOKEN_A, counted=False) == 0


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


# -- the slice price (#6153) ---------------------------------------------------------------


class FakeSlicer:
    """`PreviewSlicer` (`orders/operations.py`) for the storefront's operation: one plate,
    its sliced file ``file``, and every start kept."""

    file: ClassVar[bytes] = sliced_3mf(plates={1: (3600, [10.0])}, gcode={1: b"M620 S0A\n"})
    started: ClassVar[list[tuple[str, SliceQuoteRequest, str]]] = []

    def __init__(self, *_args: Any) -> None:
        pass

    async def start(
        self, output_id: str, request: SliceQuoteRequest, subject: str
    ) -> list[PreviewStarted]:
        FakeSlicer.started.append((output_id, request, subject))
        return [PreviewStarted(job_id=1, plate_id=1)]

    async def poll(self, started: PreviewStarted, subject: str) -> SlicedPlate | None:
        return SlicedPlate(plate_id=started.plate_id, slicer="bambuddy", file_id=1)

    async def fetch(self, sliced: SlicedPlate) -> bytes:
        return FakeSlicer.file


@pytest.fixture
def sliced(crud: CrudCaller, monkeypatch: pytest.MonkeyPatch) -> type[FakeSlicer]:
    """The shop slice profile set (printer ``p-1``, spool 9 for PLA and 5 for PETG), a
    fake slicer in `PreviewSlicer`'s place, and the printer offering both."""
    import scadbuddy.api.storefront_slice as storefront_slice

    monkeypatch.setattr(FakeSlicer, "started", [])
    monkeypatch.setattr(order_operations, "PreviewSlicer", FakeSlicer)

    async def offered(*_args: Any) -> list[ProfileSpool]:
        return [
            ProfileSpool(spool_id=9, material="PLA", label="PLA"),
            ProfileSpool(spool_id=5, material="PETG", label="PETG"),
        ]

    monkeypatch.setattr(storefront_slice, "offered_spools", offered)
    profile = StorefrontSliceProfile(printer="p-1", spools={"pla": 9, "petg": 5})
    _state(crud.api).settings_store.save(SettingsPatch(storefront_slice_profile=profile))
    return FakeSlicer


def priced(crud: CrudCaller, job_id: str, *, token: str = TOKEN_A) -> dict[str, Any]:
    """The render's price once it is no longer being worked out."""
    deadline = time.monotonic() + 60
    while True:
        r = crud.get(f"/renders/{job_id}/estimate", token=token)
        assert r.status_code == 200, r.text
        answer: dict[str, Any] = r.json()
        if answer["source"] != "pending":
            return answer
        assert answer["retry_after"] >= 1
        assert time.monotonic() < deadline, "the price was never worked out"
        time.sleep(0.2)


def test_the_price_is_a_slice_through_the_quote_path(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    job = render_until_done(crud, TOKEN_A, listing, {"width": 40})
    first = crud.get(f"/renders/{job['id']}/estimate")
    assert first.status_code == 200, first.text
    assert first.json()["source"] == "pending" and first.json()["price_minor"] is None
    est = priced(crud, job["id"])
    assert est["source"] == "slice" and est["price_minor"] > 0
    assert (est["currency"], est["minor_exponent"]) == ("USD", 2)
    # The shop's details stay on the CRUD side; the edge never sends them on.
    assert est["grams"] == {"0": "10.0"} and est["print_seconds"] == 3600
    assert len(est["price_key"]) == 64
    assert "per kg" not in str(est) and "Markup" not in str(est)
    [(output_id, request, subject)] = sliced.started
    assert subject == f"storefront_price:{est['price_key']}"
    # The profile gives the printer and the spool; the template gives supports.
    assert request.printer == "p-1" and request.supports is None
    assert [(s.slot_id, s.spool_id) for s in request.filament_plan.slots] == [(1, 9)]
    with _state(crud.api).projection.pool.connection() as conn:
        row = conn.execute(
            "SELECT status, total_minor FROM storefront_slice_prices WHERE price_key = %s",
            (est["price_key"],),
        ).fetchone()
    assert row is not None and (row["status"], row["total_minor"]) == ("done", est["price_minor"])
    # Another visitor of the same render reads the same price, and nothing is sliced again.
    a_render(crud, TOKEN_B, listing, {"width": 40})
    assert crud.get(f"/renders/{job['id']}/estimate", token=TOKEN_B).json() == est
    assert len(sliced.started) == 1
    assert output_id


def test_a_render_of_the_listing_before_it_moved_has_no_price(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    """#6370 review: the shop moves the listing to another commit after the render. The
    price would be of a design the visitor does not see, so there is none, and no slice
    starts: the page renders again."""
    job = render_until_done(crud, TOKEN_A, listing, {"width": 63})
    with _state(crud.api).projection.pool.connection() as conn:
        conn.execute(
            "UPDATE storefront_listings SET commit = %s WHERE id = %s", ("f" * 40, listing["id"])
        )
    est = crud.get(f"/renders/{job['id']}/estimate")
    assert est.status_code == 200, est.text
    assert est.json()["source"] == "none" and est.json()["price_minor"] is None
    assert sliced.started == []


def test_two_reads_at_once_slice_once(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    job = render_until_done(crud, TOKEN_A, listing, {"width": 62})
    with ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(lambda _n: crud.get(f"/renders/{job['id']}/estimate"), range(2)))
    assert [a.status_code for a in answers] == [200, 200], [a.text for a in answers]
    assert priced(crud, job["id"])["source"] == "slice"
    assert len(sliced.started) == 1


def test_a_second_variant_waits_for_the_one_slice_at_a_time(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    """`storefront_slices_concurrent` 1: while one slice runs, another variant's read
    starts nothing and answers pending; once it is done, the next read starts it. A render
    of other values whose 3MF has the same content (the fake openscad draws one box for
    every width) is the same variant, so its price is read with no slice (#6153)."""
    one = render_until_done(crud, TOKEN_A, listing, {"width": 63})
    two = render_until_done(crud, TOKEN_A, listing, {"width": 64})
    prices = _state(crud.api).components.get(ORDERS).slice_prices
    busy = prices.claim("f" * 64, "0" * 64, limit=1)
    assert busy.claimed
    r = crud.get(f"/renders/{one['id']}/estimate")
    assert r.json()["source"] == "pending"
    assert sliced.started == []
    with _state(crud.api).projection.pool.connection() as conn:
        conn.execute("DELETE FROM storefront_slice_prices WHERE price_key = %s", ("f" * 64,))
    assert priced(crud, one["id"])["source"] == "slice"
    assert priced(crud, two["id"]) == priced(crud, one["id"])
    assert len(sliced.started) == 1


def test_with_no_slice_profile_there_is_no_price(crud: CrudCaller, listing: dict[str, Any]) -> None:
    """If unsure, no price (owner, 2026-11-14): never a volume estimate."""
    job = render_until_done(crud, TOKEN_A, listing, {"width": 41})
    r = crud.get(f"/renders/{job['id']}/estimate")
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "none" and r.json()["price_minor"] is None
    assert "grams" not in r.json() or r.json()["grams"] is None


def test_a_slice_that_cannot_be_priced_gives_no_price(
    crud: CrudCaller,
    listing: dict[str, Any],
    sliced: type[FakeSlicer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sliced, "file", sliced_3mf(plates={1: (3600, [0.0])}, gcode={1: b"M620 S0A\n"})
    )
    job = render_until_done(crud, TOKEN_A, listing, {"width": 42})
    assert priced(crud, job["id"])["source"] == "none"
    # Read again, nothing is sliced again.
    assert priced(crud, job["id"])["source"] == "none"
    assert len(sliced.started) == 1


def test_a_profile_spool_no_longer_offered_gives_no_price(
    crud: CrudCaller,
    listing: dict[str, Any],
    sliced: type[FakeSlicer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#6255 review: the tray's negative id moved, so the profile is checked first."""
    import scadbuddy.api.storefront_slice as storefront_slice

    async def swapped(*_args: Any) -> list[ProfileSpool]:
        return [ProfileSpool(spool_id=-3, material="PLA", label="PLA")]

    monkeypatch.setattr(storefront_slice, "offered_spools", swapped)
    job = render_until_done(crud, TOKEN_A, listing, {"width": 43})
    assert priced(crud, job["id"])["source"] == "none"
    assert sliced.started == []


def test_a_slow_fetch_answers_pending_at_once(
    crud: CrudCaller, listing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6120: no price read waits on a done render's files past `READY_WAIT`."""
    job = render_until_done(crud, TOKEN_A, listing, {"width": 44})
    monkeypatch.setattr(renders_module, "READY_WAIT", 0.1)
    monkeypatch.setattr(renders_module, "_ready", OrderedDict())
    real = materialize_result

    async def slow(blobs: Any, result: Any) -> None:
        await asyncio.sleep(0.6)
        await real(blobs, result)

    monkeypatch.setattr(renders_module, "materialize_result", slow)
    started = time.monotonic()
    r = crud.get(f"/renders/{job['id']}/estimate")
    assert time.monotonic() - started < 0.5
    assert r.status_code == 200 and r.json()["source"] == "pending", r.text


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


@pytest.mark.parametrize(
    "body",
    ["[]", '{"listing_id": 1}', "not json", "[" * 60000],
    ids=["array", "wrong-type", "not-json", "deep"],
)
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

    monkeypatch.setattr(_state(crud.api).render, "submit_answer", full)
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


def ledger_rows(crud: CrudCaller, key: str) -> int:
    with _state(crud.api).projection.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM crud_requests WHERE caller = 'storefront' AND key = %s",
            (key,),
        ).fetchone()
    assert row is not None
    n: int = row["n"]
    return n


def unreachable() -> Exception:
    """Temporal's first connect failed: `start_command` raises this with the lazy
    client's `RuntimeError` as its cause, and nothing was sent."""
    error = TemporalUnavailableError("down")
    error.__cause__ = RuntimeError("no connection")
    return error


@pytest.mark.parametrize(
    "failure",
    [
        unreachable,
        lambda: TemporalUnreachableError("down"),
        lambda: SnapshotUnavailableError("down"),
        lambda: SnapshotPendingError("down", retry_after=3),
        lambda: StoreFullError("down"),
    ],
)
def test_a_submit_that_never_started_costs_no_render(
    crud: CrudCaller,
    listing: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: Any,
) -> None:
    """A render that never started is not charged, and the same key sent again is a new
    try: the ledger holds no reply naming the deleted row."""
    render = _state(crud.api).render
    real = render.submit_answer

    async def fails(*_args: Any, **_kwargs: Any) -> Job:
        raise failure()

    monkeypatch.setattr(render, "submit_answer", fails)
    body = render_body(listing, {"width": 71})
    r = crud.post("/renders", json=body, key="k-failed")
    assert r.status_code == 503, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "render_unavailable"
    assert "down" not in r.text
    assert counted_rows(crud, TOKEN_A) == 0 and counted_rows(crud, TOKEN_A, counted=False) == 0
    assert ledger_rows(crud, "k-failed") == 0
    monkeypatch.setattr(render, "submit_answer", real)
    again = crud.post("/renders", json=body, key="k-failed")
    assert again.status_code == 202, again.text
    assert counted_rows(crud, TOKEN_A) == 1
    assert wait_done(crud, TOKEN_A, again.json()["id"])["status"] == "done"


@pytest.mark.parametrize(
    "failure",
    [
        lambda: TemporalUnavailableError("down"),
        lambda: CommandStillAcceptingError("down"),
        lambda: CommandClosedError("down"),
        lambda: RuntimeError("down"),
    ],
)
def test_a_submit_that_may_have_started_keeps_its_row(
    crud: CrudCaller,
    listing: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: Any,
) -> None:
    """The job may exist: the counted row and the saved reply stay, so the same key sent
    again joins the job and is counted once."""
    render = _state(crud.api).render
    real = render.submit_answer

    async def fails(*_args: Any, **_kwargs: Any) -> Job:
        raise failure()

    monkeypatch.setattr(render, "submit_answer", fails)
    body = render_body(listing, {"width": 72})
    r = crud.post("/renders", json=body, key="k-maybe")
    assert r.status_code == 503, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "render_unavailable"
    assert counted_rows(crud, TOKEN_A) == 1 and ledger_rows(crud, "k-maybe") == 1
    monkeypatch.setattr(render, "submit_answer", real)
    again = crud.post("/renders", json=body, key="k-maybe")
    assert again.status_code == 202, again.text
    assert counted_rows(crud, TOKEN_A) == 1
    assert crud.get(f"/renders/{again.json()['id']}").status_code == 200
    wait_done(crud, TOKEN_A, again.json()["id"])


def test_a_shared_guess_that_starts_a_new_run_is_counted(
    crud: CrudCaller, listing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open job settled between the read and the submit: the submit started a run of
    its own, so the row that was free is counted (review Important 1)."""
    monkeypatch.setattr(renders_module, "_shared", lambda *_args: True)
    job = a_render(crud, TOKEN_A, listing, {"width": 73})
    assert counted_rows(crud, TOKEN_A) == 1 and counted_rows(crud, TOKEN_A, counted=False) == 0
    wait_done(crud, TOKEN_A, job["id"])


def test_a_counted_guess_that_joins_a_job_costs_nothing(
    crud: CrudCaller, listing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished render of the key answered: a reused render costs nothing."""
    done = render_until_done(crud, TOKEN_A, listing, {"width": 74})
    monkeypatch.setattr(renders_module, "_shared", lambda *_args: False)
    jb = a_render(crud, TOKEN_B, listing, {"width": 74})
    assert jb["id"] == done["id"]
    assert counted_rows(crud, TOKEN_B) == 0 and counted_rows(crud, TOKEN_B, counted=False) == 1


def test_a_negative_colour_slot_is_refused(crud: CrudCaller, listing: dict[str, Any]) -> None:
    r = crud.post(
        "/renders",
        json=render_body(listing, {"width": 40}, {"-1": listing["colours"][0]}),
        key=uuid.uuid4().hex,
    )
    assert r.status_code == 422, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "render_body_invalid"


def test_a_full_queue_leaves_no_reply_to_replay(
    crud: CrudCaller, listing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Minor 3: the row and its reply go together, as a start that never ran."""

    async def full(*_args: Any, **_kwargs: Any) -> Any:
        raise QueueFullError(9, retry_after=7)

    monkeypatch.setattr(_state(crud.api).render, "submit_answer", full)
    r = crud.post("/renders", json=render_body(listing, {"width": 75}), key="k-full")
    assert r.status_code == 429, r.text
    assert ledger_rows(crud, "k-full") == 0
    assert counted_rows(crud, TOKEN_A) == 0 and counted_rows(crud, TOKEN_A, counted=False) == 0


def test_a_done_render_names_its_preview_once_its_files_are_here(
    crud: CrudCaller, listing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6120: a done render's status names no preview while its files are fetched, so the
    page never asks for a preview the API would hold open; a later poll names it."""
    job = render_until_done(crud, TOKEN_A, listing, {"width": 69})
    monkeypatch.setattr(renders_module, "READY_WAIT", 0.1)
    # As in a process that never fetched this job's files.
    monkeypatch.setattr(renders_module, "_ready", OrderedDict())
    real = materialize_result
    fetches = 0

    async def slow(blobs: Any, result: Any) -> None:
        nonlocal fetches
        fetches += 1
        await asyncio.sleep(0.6)
        await real(blobs, result)

    monkeypatch.setattr(renders_module, "materialize_result", slow)
    first = crud.get(f"/renders/{job['id']}")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "done" and first.json()["preview_url"] is None
    deadline = time.monotonic() + 10
    while (r := crud.get(f"/renders/{job['id']}")).json()["preview_url"] is None:
        assert time.monotonic() < deadline, r.text
        time.sleep(0.1)
    assert r.json()["preview_url"].endswith(f"/renders/{job['id']}/preview.glb")
    assert fetches == 1


def test_a_render_whose_files_are_lost_names_its_preview_after_a_few_tries(
    crud: CrudCaller, listing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6120 review: a lost blob never leaves the page polling for its whole time limit;
    after `FETCH_TRIES` failed fetches the preview is named, and the estimate says it
    has no price (#6153)."""
    job = render_until_done(crud, TOKEN_A, listing, {"width": 70})
    monkeypatch.setattr(renders_module, "_ready", OrderedDict())
    monkeypatch.setattr(renders_module, "_failed", OrderedDict())

    async def lost(_blobs: Any, _result: Any) -> None:
        raise FileNotFoundError("the blob is gone")

    monkeypatch.setattr(renders_module, "materialize_result", lost)
    tries = renders_module.FETCH_TRIES
    seen = [crud.get(f"/renders/{job['id']}").json()["preview_url"] for _ in range(tries)]
    # The poll whose fetch fails for the last time names it.
    assert seen[: tries - 1] == [None] * (tries - 1)
    assert seen[-1] is not None
    # The preview's read fetches on its own (`get_job_preview`): a blob truly lost is
    # its 404 there; here only this module's fetch fails.
    # The estimate says so too (#6153): no price, and nothing sliced.
    r = crud.get(f"/renders/{job['id']}/estimate")
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "none" and r.json()["price_minor"] is None


def test_the_estimate_fetches_files_the_cache_evicted_after_the_preview_was_named(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    """#6161 review: a job the status took as ready may have lost its cached files since;
    the estimate fetches them again before it hashes the 3MF (#6153)."""
    job = render_until_done(crud, TOKEN_A, listing, {"width": 71})
    assert crud.get(f"/renders/{job['id']}").json()["preview_url"] is not None
    state = _state(crud.api)
    found = state.render.store.read(job["id"])
    assert found.result is not None
    piece = (
        state.paths.root
        / Path(found.result.model_3mf).parts[0]
        / Path(found.result.model_3mf).parts[1]
    )
    shutil.rmtree(piece)
    assert priced(crud, job["id"])["source"] == "slice"


def test_a_shared_guess_over_the_limit_is_refused_and_withdrawn(
    crud: CrudCaller,
    listing: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Re-review 1: a render guessed "shared" that starts a run of its own passes the
    same limits as any other. Over them it is refused and its run withdrawn, so a
    visitor racing a fast-failing render cannot exceed the per-client limit."""
    for w in (30, 31, 32):
        a_render(crud, TOKEN_A, listing, {"width": w})
    started, release = tmp_path / "started", tmp_path / "release"
    set_fake_env(
        Path(settings.openscad).parent,
        "FAKE_HOLD",
        {"started": str(started), "release": str(release)},
    )
    monkeypatch.setattr(renders_module, "_shared", lambda *_args: True)
    seen: list[Job] = []
    render = _state(crud.api).render
    real = render.submit_answer

    async def kept(*args: Any, **kwargs: Any) -> tuple[Job, bool]:
        answer = await real(*args, **kwargs)
        seen.append(answer[0])
        return answer

    monkeypatch.setattr(render, "submit_answer", kept)
    try:
        r = crud.post("/renders", json=render_body(listing, {"width": 76}), key="k-race")
        assert r.status_code == 429, r.text
        assert r.json()["type"] == STOREFRONT_PROBLEMS + "render_limited"
        assert counted_rows(crud, TOKEN_A) == 3 and counted_rows(crud, TOKEN_A, counted=False) == 0
        assert ledger_rows(crud, "k-race") == 0
        [job] = seen
        assert render.store.read(job.id).state == "cancelled"
    finally:
        release.touch()


# -- one material, picked by colour (#6152, #6258) ---------------------------------------


def _filament(client: TestClient, colour: str, cost: str, material: str = "PLA") -> str:  # noqa: F811
    """A filament of the book; its brand is its cost, so two of one colour may stand."""
    made = client.post(
        f"{BOOK}/filaments",
        json={
            "material": material,
            "brand": f"Brand {cost}",
            "colour": colour,
            "cost_per_kg": cost,
            "density_g_cm3": "1.24",
        },
    )
    assert made.status_code in (200, 201), made.text
    filament_id: str = made.json()["id"]
    return filament_id


def _red_listing(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
    colours: list[str],
    **fields: Any,
) -> dict[str, Any]:
    body = listing_body(
        template,
        colours[0],
        params={
            "width": {"kind": "number", "min": 20, "max": 80, "step": 1},
            "colour": {"kind": "choice", "choices": ["#FF0000"]},
        },
        colours=colours,
        **fields,
    )
    return publish(client, create(client, body)["id"])


@pytest.fixture
def two_reds(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
) -> tuple[dict[str, Any], str, str]:
    """A published listing whose colour a visitor picks, with a cheap PLA red and a dear
    PETG red, and no materials of its own: the shop's default puts PETG first. Also its
    two filament ids."""
    cheap = _filament(client, "#FF0000", "20")
    dear = _filament(client, "#FF0000", "400", "PETG")
    settings = client.put("/api/v1/settings", json={"storefront_render_concurrency": 20})
    assert settings.status_code == 200, settings.text
    return _red_listing(client, template, [cheap, dear]), cheap, dear


def _two_colour_listing(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
    colours: list[str],
) -> dict[str, Any]:
    """A published listing of a template with two colour parameters, ``colour`` and
    ``trim`` (default red), each offered red or blue."""
    choices = {"kind": "choice", "choices": ["#FF0000", "#0000FF"]}
    body = _two_colour_body(client, template, colours, params={"colour": choices, "trim": choices})
    return publish(client, create(client, body)["id"])


def _two_colour_body(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
    colours: list[str],
    **fields: Any,
) -> dict[str, Any]:
    """The body of a listing of a template with two colour parameters, ``colour`` and
    ``trim`` (default red)."""
    source = SOURCE.replace(
        "// %%LISTING%%\n", '// %%LISTING%% %%TWO_COLOURS%%\ntrim = "#FF0000"; // color\n'
    )
    made = client.post("/api/v1/models", json={"name": "Two", "source": source}, headers=press())
    assert made.status_code == 201, made.text
    slug = made.json()["slug"]
    commit = client.get(f"/api/v1/models/{slug}").json()["version"]
    return listing_body(
        {"slug": slug, "commit": commit, "media_id": template["media_id"]},
        colours[0],
        colours=colours,
        media_ids=[],
        **fields,
    )


def test_each_visitor_is_priced_at_its_listings_material(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,
    template: dict[str, Any],  # noqa: F811
    two_reds: tuple[dict[str, Any], str, str],
    sliced: type[FakeSlicer],
) -> None:
    """Two listings of one design share one render of the same values. One takes the
    shop's order (PETG first), the other puts PLA first: each row keeps the material its
    colours resolved to, and the slice prices the red slot at that material's red: each
    is a variant of its own (#6153), so one slice and one price each."""
    petg_first, cheap, dear = two_reds
    pla_first = _red_listing(client, template, [cheap, dear], materials=["PLA", "PETG"])
    values = {"width": 40, "colour": "#FF0000"}
    first = crud.post("/renders", json=render_body(petg_first, values), key=uuid.uuid4().hex)
    assert first.status_code == 202, first.text
    job = wait_done(crud, TOKEN_A, first.json()["id"])
    assert job["status"] == "done", job
    second = crud.post(
        "/renders", token=TOKEN_B, json=render_body(pla_first, values), key=uuid.uuid4().hex
    )
    assert second.status_code == 202, second.text
    assert second.json()["id"] == job["id"]
    petg_price = priced(crud, job["id"])["price_minor"]
    pla_price = priced(crud, job["id"], token=TOKEN_B)["price_minor"]
    assert petg_price > pla_price
    assert len(sliced.started) == 2


def test_a_page_from_before_the_deploy_still_renders_and_adds(
    crud: CrudCaller, two_reds: tuple[dict[str, Any], str, str]
) -> None:
    """#6302 review: the page built before #6258 always sends ``picks`` (``{}`` when
    nothing is picked). For one release the body takes it and ignores it, so an open tab
    still renders and adds to the order."""
    listing, cheap, _ = two_reds
    for picks in ({}, {"colour": cheap}):
        body = {**render_body(listing, {"width": 40, "colour": "#FF0000"}), "picks": picks}
        rendered = crud.post("/renders", json=body, key=uuid.uuid4().hex)
        assert rendered.status_code == 202, rendered.text
        job = wait_done(crud, TOKEN_A, rendered.json()["id"])
        assert job["status"] == "done", job
        added = crud.post("/draft/items", json=body, key=uuid.uuid4().hex)
        assert added.status_code == 200, added.text


def test_add_to_order_fills_the_slots_from_the_resolved_material(
    crud: CrudCaller, two_reds: tuple[dict[str, Any], str, str]
) -> None:
    """The page sends no slots: the CRUD side reads the visitor's render and gives each
    slot the filament of its colour in the material the colours resolved to (PETG, the
    first of the shop's order that has red)."""
    listing, _, dear = two_reds
    body = render_body(listing, {"width": 40, "colour": "#FF0000"})
    rendered = crud.post("/renders", json=body, key=uuid.uuid4().hex)
    assert rendered.status_code == 202, rendered.text
    job = wait_done(crud, TOKEN_A, rendered.json()["id"])
    assert job["colors"], job
    added = crud.post("/draft/items", json=body, key=uuid.uuid4().hex)
    assert added.status_code == 200, added.text
    orders: OrdersComponent = _state(crud.api).components.get(ORDERS)
    [stored] = orders.store.items(added.json()["id"])
    assert stored.colours
    assert [(c.slot, c.filament_id) for c in stored.colours] == [
        (slot, dear) for slot, colour in enumerate(job["colors"]) if colour.upper() == "#FF0000"
    ]


def test_add_to_order_fills_an_unpicked_slot_from_the_first_material(
    crud: CrudCaller, two_reds: tuple[dict[str, Any], str, str]
) -> None:
    """A colour left at the shop's choice still names a filament on the order line: the
    filament of the slot's colour in the first material of the shop's order (#6258)."""
    listing, _, dear = two_reds
    body = render_body(listing, {"width": 40})
    rendered = crud.post("/renders", json=body, key=uuid.uuid4().hex)
    assert rendered.status_code == 202, rendered.text
    job = wait_done(crud, TOKEN_A, rendered.json()["id"])
    assert job["status"] == "done", job
    added = crud.post("/draft/items", json=body, key=uuid.uuid4().hex)
    assert added.status_code == 200, added.text
    orders: OrdersComponent = _state(crud.api).components.get(ORDERS)
    [stored] = orders.store.items(added.json()["id"])
    assert stored.colours
    assert [(c.slot, c.filament_id) for c in stored.colours] == [
        (slot, dear) for slot, colour in enumerate(job["colors"]) if colour.upper() == "#FF0000"
    ]


def test_colours_of_no_one_material_are_refused_with_the_parts(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,
    template: dict[str, Any],  # noqa: F811
    dense_red: str,
) -> None:
    """Red is PLA only and blue PETG only: a visitor who gives both is refused 422
    ``listing_colours_clash`` naming both parts, at the render and at the add. With the
    trim left alone it moves to PETG's closest colour, and the render runs."""
    petg_blue = _filament(client, "#0000FF", "30", "PETG")
    listing = _two_colour_listing(client, template, [dense_red, petg_blue])
    both = render_body(listing, {"colour": "#0000FF", "trim": "#FF0000"})
    for path in ("/renders", "/draft/items"):
        r = crud.post(path, json=both, key=uuid.uuid4().hex)
        assert r.status_code == 422, r.text
        assert r.json()["type"] == STOREFRONT_PROBLEMS + "listing_colours_clash"
        assert r.json()["parts"] == ["colour", "trim"]
    alone = render_body(listing, {"colour": "#0000FF"})
    assert crud.post("/renders", json=alone, key=uuid.uuid4().hex).status_code == 202


def test_a_render_answers_the_colours_it_settled(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,
    template: dict[str, Any],  # noqa: F811
    dense_red: str,
) -> None:
    """#6302 review: the trim's default red is PLA only, and the visitor picks a blue that
    is PETG only. The render is PETG, so the trim moves to PETG's blue, and the answer
    says so (``values``), so the page shows what is printed. Never the material."""
    petg_blue = _filament(client, "#0000FF", "30", "PETG")
    listing = _two_colour_listing(client, template, [dense_red, petg_blue])
    started = crud.post(
        "/renders", json=render_body(listing, {"colour": "#0000FF"}), key=uuid.uuid4().hex
    )
    assert started.status_code == 202, started.text
    assert started.json()["values"] == {"colour": "#0000FF", "trim": "#0000FF"}
    assert "material" not in started.json()
    # A listing the visitor does not touch renders its defaults.
    bare = crud.post("/renders", json=render_body(listing, {}), key=uuid.uuid4().hex)
    assert bare.status_code == 202, bare.text
    assert bare.json()["values"] == {"colour": "#FF0000", "trim": "#FF0000"}


def test_a_default_whose_material_the_shop_drops_still_renders(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,
    template: dict[str, Any],  # noqa: F811
    dense_red: str,
) -> None:
    """#6302 review: the defaults are a PLA red, and ``storefront_materials`` then drops
    PLA. A render that does not send those colours is 202, not 422: they move to the
    closest PETG colour."""
    petg_blue = _filament(client, "#0000FF", "30", "PETG")
    listing = _two_colour_listing(client, template, [dense_red, petg_blue])
    settings = client.put("/api/v1/settings", json={"storefront_materials": ["PETG"]})
    assert settings.status_code == 200, settings.text
    r = crud.post("/renders", json=render_body(listing, {}), key=uuid.uuid4().hex)
    assert r.status_code == 202, r.text
    assert r.json()["values"] == {"colour": "#0000FF", "trim": "#0000FF"}


def test_publish_refuses_locked_colours_of_no_one_material(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
    dense_red: str,
) -> None:
    """#6302 review: the shop locks the colour red (PLA only) and the trim blue (PETG
    only). No print of one material has both, so publish refuses it and names both."""
    petg_blue = _filament(client, "#0000FF", "30", "PETG")
    body = _two_colour_body(
        client,
        template,
        [dense_red, petg_blue],
        params={"width": {"kind": "number", "min": 20, "max": 80, "step": 1}},
        fixed={"trim": "#0000FF"},
    )
    listing = create(client, body)
    refused = client.post(f"{LISTINGS}/{listing['id']}/publish")
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"] == STOREFRONT_PROBLEMS + "listing_locked_colours_unprintable"
    assert refused.json()["names"] == ["colour", "trim"]


def test_a_listing_whose_locks_clash_since_is_unavailable(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,
    template: dict[str, Any],  # noqa: F811
    dense_red: str,
) -> None:
    """#6302 review: the colour is locked to a PLA red, and ``storefront_materials`` then
    drops PLA (as a listing published before publish checked its locks). No visitor's
    colour can make it printable, so a render is 409 ``listing_unavailable`` and names
    no colour."""
    petg_blue = _filament(client, "#0000FF", "30", "PETG")
    listing = publish(
        client,
        create(client, listing_body(template, dense_red, colours=[dense_red, petg_blue]))["id"],
    )
    settings = client.put("/api/v1/settings", json={"storefront_materials": ["PETG"]})
    assert settings.status_code == 200, settings.text
    r = crud.post("/renders", json=render_body(listing, {}), key=uuid.uuid4().hex)
    assert r.status_code == 409, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "listing_unavailable"
    assert "parts" not in r.json()
    # Its card and page say so too: the same rule, from the live materials.
    assert crud.get("/listings").json()["listings"][0]["available"] is False
    assert crud.get(f"/listings/{listing['id']}").json()["available"] is False


def test_publish_refuses_a_lock_of_one_material_and_choices_of_another(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
) -> None:
    """#6302 review: the colour is locked to black, which only PLA has; the trim is
    offered only in PETG's white. No material prints both, so publish refuses it."""
    pla_black = _filament(client, "#000000", "20")
    petg_white = _filament(client, "#FFFFFF", "30", "PETG")
    body = _two_colour_body(
        client,
        template,
        [pla_black, petg_white],
        params={"trim": {"kind": "choice", "choices": ["#FFFFFF"], "default": "#FFFFFF"}},
        fixed={"colour": "#000000"},
    )
    listing = create(client, body)
    refused = client.post(f"{LISTINGS}/{listing['id']}/publish")
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"] == STOREFRONT_PROBLEMS + "listing_locked_colours_unprintable"
    assert refused.json()["names"] == ["colour"]


def test_a_lock_and_choices_of_two_materials_since_are_unavailable(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,
    template: dict[str, Any],  # noqa: F811
    dense_red: str,
) -> None:
    """#6302 review: the colour is locked to red (PLA and PETG); the trim is offered only
    in PETG's blue. ``storefront_materials`` then drops PETG: PLA holds the lock but no
    trim choice. Every render is 409 ``listing_unavailable``, never a clash that names
    the trim, and the card and page say it cannot be ordered."""
    petg_red = _filament(client, "#FF0000", "31", "PETG")
    petg_blue = _filament(client, "#0000FF", "30", "PETG")
    body = _two_colour_body(
        client,
        template,
        [dense_red, petg_red, petg_blue],
        params={"trim": {"kind": "choice", "choices": ["#0000FF"], "default": "#0000FF"}},
    )
    listing = publish(client, create(client, body)["id"])
    assert crud.get("/listings").json()["listings"][0]["available"] is True
    settings = client.put("/api/v1/settings", json={"storefront_materials": ["PLA"]})
    assert settings.status_code == 200, settings.text
    r = crud.post("/renders", json=render_body(listing, {}), key=uuid.uuid4().hex)
    assert r.status_code == 409, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "listing_unavailable"
    assert "parts" not in r.json()
    assert crud.get("/listings").json()["listings"][0]["available"] is False
    page = crud.get(f"/listings/{listing['id']}").json()
    assert page["available"] is False
    assert page["materials"] == ["PLA"] and page["colours"] == []


def test_add_to_order_refuses_a_slot_of_another_material(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,
    template: dict[str, Any],  # noqa: F811
    dense_red: str,
) -> None:
    """The red the visitor gives is PLA only, but the add names a PETG slot filament: the
    print would mix materials, so the add is 422 ``listing_colours_clash`` (#6152,
    #6258)."""
    petg_blue = _filament(client, "#0000FF", "30", "PETG")
    body = listing_body(
        template,
        dense_red,
        params={"colour": {"kind": "choice", "choices": ["#FF0000", "#0000FF"]}},
        colours=[dense_red, petg_blue],
    )
    listing = publish(client, create(client, body)["id"])
    mixed = render_body(listing, {"colour": "#FF0000"}, {"0": petg_blue})
    added = crud.post("/draft/items", json=mixed, key=uuid.uuid4().hex)
    assert added.status_code == 422, added.text
    assert added.json()["type"] == STOREFRONT_PROBLEMS + "listing_colours_clash"
    assert added.json()["parts"] == ["colour", "colours.0"]


def test_add_to_order_with_a_colour_waits_for_the_render(
    crud: CrudCaller, two_reds: tuple[dict[str, Any], str, str]
) -> None:
    """With no done render the slots are unknown, and the visitor's colour would be
    lost: 409 ``render_pending`` (#6152)."""
    listing, _, _ = two_reds
    body = render_body(listing, {"colour": "#FF0000"})
    added = crud.post("/draft/items", json=body, key=uuid.uuid4().hex)
    assert added.status_code == 409, added.text
    assert added.json()["type"] == STOREFRONT_PROBLEMS + "render_pending"


def test_a_listing_never_offers_support(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,
    template: dict[str, Any],  # noqa: F811
    dense_red: str,
) -> None:
    """A support filament is never shown, and its colour is never the customer's
    (#6152). The listing shows the shop's materials (#6258)."""
    support = _filament(client, "#00FF00", "30", "PVA")
    body = listing_body(
        template,
        dense_red,
        params={"colour": {"kind": "choice", "choices": ["#FF0000"]}},
        colours=[dense_red, support],
    )
    listing = publish(client, create(client, body)["id"])
    shown = crud.get(f"/listings/{listing['id']}")
    assert shown.status_code == 200, shown.text
    assert shown.json()["materials"] == ["PETG", "PLA"]
    assert support not in [f["id"] for f in shown.json()["colours"]]
    r = crud.post(
        "/renders", json=render_body(listing, {"colour": "#00FF00"}), key=uuid.uuid4().hex
    )
    assert r.status_code == 422, r.text
    assert (r.json()["name"], r.json()["reason"]) == ("colour", "colour")


def test_publish_refuses_a_filament_of_a_material_not_allowed(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
    dense_red: str,
) -> None:
    """#6258: a listing that allows PLA only cannot offer a PETG filament."""
    petg_red = _filament(client, "#FF0000", "30", "PETG")
    body = listing_body(template, dense_red, colours=[dense_red, petg_red], materials=["PLA"])
    made = create(client, body)
    refused = client.post(f"/api/v1/storefront/listings/{made['id']}/publish")
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"] == STOREFRONT_PROBLEMS + "listing_material_not_allowed"
    assert refused.json()["name"] == petg_red
    # With PETG allowed too, it publishes.
    allowed = {**body, "materials": ["PLA", "PETG"]}
    edited = client.put(
        f"/api/v1/storefront/listings/{made['id']}",
        json={"listing": allowed, "updated_at": made["updated_at"]},
    )
    assert edited.status_code == 200, edited.text
    publish(client, made["id"])


BACKFILL = (
    Path(__file__).parents[2] / "scadbuddy/migrations/20261115T0346Z_listing_materials_backfill.sql"
)


def test_the_backfill_gives_an_old_listing_the_materials_it_offers(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,
    template: dict[str, Any],  # noqa: F811
    dense_red: str,
) -> None:
    """#6302 review: a listing from before #6258 has no materials of its own. The
    migration gives it the materials of its filaments, support left out, PETG and PLA
    first, so no colour goes on deploy. Of two spellings of one material, the first
    filament's in ``colours`` is kept, as the agent's ``draftMaterials`` keeps it. A listing
    with materials is left alone."""
    abs_blue = _filament(client, "#0000FF", "31", "ABS")
    pla_green = _filament(client, "#00FF00", "34", "pla")
    petg_red = _filament(client, "#FF0000", "32", "petg")
    petg_green = _filament(client, "#00FF00", "35", "PETG")
    pva = _filament(client, "#FFFFFF", "33", "PVA")
    colours = [abs_blue, pla_green, dense_red, petg_red, petg_green, pva]
    old = create(client, listing_body(template, dense_red, colours=colours, materials=["ABS"]))
    kept = create(
        client, listing_body(template, dense_red, colours=colours, materials=["ABS", "PETG"])
    )
    with _state(crud.api).projection.pool.connection() as conn:
        conn.execute("UPDATE storefront_listings SET materials = '{}' WHERE id = %s", (old["id"],))
        conn.execute(BACKFILL.read_text(encoding="utf-8").encode())
        rows = conn.execute(
            "SELECT id, materials FROM storefront_listings WHERE id = ANY(%s)",
            ([old["id"], kept["id"]],),
        ).fetchall()
    found = {row["id"]: row["materials"] for row in rows}
    # `min` would keep "PETG" and "PLA"; the first spellings are "petg" and "pla".
    assert found == {old["id"]: ["petg", "pla", "ABS"], kept["id"]: ["ABS", "PETG"]}
