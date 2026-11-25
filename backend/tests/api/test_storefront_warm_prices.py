# ruff: noqa: F811  (pytest fixtures imported by name, then taken as arguments)
"""The shop warms its listings' prices (#6922): publish renders the default variant and
starts its storefront slice price, and Prepare colours starts the price of each prepared
variant, both through the visitor's own path (`slice_price.py` `slice_estimate`), so the
instant read (``POST …/listings/{id}/variant``) has a firm price before any visitor asks.
A variant that has a ``done`` price starts no slice, and `storefront_slices_concurrent`
holds: a variant with no room waits for one."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import trimesh
from fastapi.testclient import TestClient

import scadbuddy.main as main_module
from scadbuddy.core.settings import Settings
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.storefront import prepare
from tests.api.conftest import set_fake_env
from tests.api.test_storefront_listings import (  # noqa: F401  (fixtures)
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
from tests.api.test_storefront_prepare import pla_blue, prepare_path  # noqa: F401
from tests.api.test_storefront_renders import (  # noqa: F401  (fixtures)
    GLB,
    CrudCaller,
    FakeSlicer,
    crud,
    dense_red,
    render_body,
    sliced,
)
from tests.conftest import write_openscad_3mf

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The tests' app warms no price (`tests/conftest.py`); these tests turn it on."""
    return settings.model_copy(update={"storefront_warm_prices": True})


@pytest.fixture(autouse=True)
def app_warm_sweep_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app's own listing-price sweep (#7665) does nothing: the prune Schedule's boot
    run calls it at a time the test does not choose, as `app_payment_sweep_off` says, and
    with prices warmed it starts the run the test's own pass then finds open. These tests
    call `prepare.warm_unpriced` themselves."""

    async def off(_state: object) -> None:
        return None

    monkeypatch.setattr(main_module, "_warm_listings_logged", off)


def rows(crud: CrudCaller, sql: str, *args: Any) -> list[dict[str, Any]]:
    with _state(crud.api).projection.pool.connection() as conn:
        return list(conn.execute(sql, args).fetchall())


def instant(crud: CrudCaller, listing: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    r = crud.post(f"/listings/{listing['id']}/variant", json=render_body(listing, values))
    assert r.status_code == 200, r.text
    price: dict[str, Any] = r.json()["price"]
    return price


def firm(crud: CrudCaller, listing: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """The instant read's price once the warmed slice is done: no visitor renders."""
    deadline = time.monotonic() + 90
    while True:
        price = instant(crud, listing, values)
        if price["source"] == "slice":
            return price
        assert time.monotonic() < deadline, f"the price was never warmed: {price}"
        time.sleep(0.3)


def blue_renders_blue(settings: Settings) -> None:
    """The fake openscad draws a blue box for a blue colour, so the blue variant is
    another 3MF, another slot colour and another price."""
    directory = Path(settings.openscad).parent
    drawn = write_openscad_3mf(
        directory / "drawn-blue.3mf",
        [("Color 1", "#0000FF00", trimesh.creation.box(extents=(10, 10, 5)))],
    )
    set_fake_env(directory, "FAKE_3MF_IF", {"arg": "#0000FF", "file": str(drawn)})


def two_colour_listing(
    client: TestClient, template: dict[str, Any], red: str, blue: str
) -> dict[str, Any]:
    body = listing_body(
        template,
        red,
        colours=[red, blue],
        params={"colour": {"kind": "choice", "choices": ["#FF0000", "#0000FF"]}},
    )
    return publish(client, create(client, body)["id"])


def wait_run_ended(client: TestClient, listing: dict[str, Any]) -> None:
    deadline = time.monotonic() + 90
    while client.get(prepare_path(listing)).json()["running"]:
        assert time.monotonic() < deadline, "the prepare run never ended"
        time.sleep(0.3)


def test_publish_prices_the_default_variant(
    client: TestClient,
    crud: CrudCaller,
    template: dict[str, Any],
    dense_red: str,
    sliced: type[FakeSlicer],
) -> None:
    listing = publish(client, create(client, listing_body(template, dense_red))["id"])
    price = firm(crud, listing, {})
    assert price["price_minor"] > 0
    assert len(sliced.started) == 1
    # No visitor rendered it: the shop did, as a visitor's first load would.
    assert rows(crud, "SELECT 1 FROM storefront_renders") == []
    # The warmed row serves the instant read and the cart add as a visitor's would: it
    # records the variant, its listing, its slots and the output the slice saved.
    [row] = rows(
        crud,
        "SELECT variant_key, listing_id, slot_filaments, slot_colours, output_id"
        " FROM storefront_slice_prices",
    )
    assert row["listing_id"] == listing["id"] and row["output_id"]
    assert row["slot_filaments"] == [dense_red] and row["slot_colours"]
    r = crud.post(f"/listings/{listing['id']}/variant", json=render_body(listing, {}))
    assert r.status_code == 200, r.text
    answer = r.json()
    assert answer["price"]["source"] == "slice"
    assert answer["preview"]["url"] == f"/api/store/v1/variants/{row['variant_key']}/preview.glb"
    glb = crud.get(f"/variants/{row['variant_key']}/preview.glb")
    assert glb.status_code == 200, glb.text
    assert glb.headers["content-type"] == GLB


def test_prepare_prices_each_variant_and_slices_a_priced_one_never_again(
    client: TestClient,
    crud: CrudCaller,
    settings: Settings,
    template: dict[str, Any],
    pla_red: str,
    pla_blue: str,
    sliced: type[FakeSlicer],
) -> None:
    blue_renders_blue(settings)
    listing = two_colour_listing(client, template, pla_red, pla_blue)
    red = firm(crud, listing, {})
    assert len(sliced.started) == 1
    r = client.post(prepare_path(listing))
    assert r.status_code == 202 or r.status_code == 200, r.text
    blue = firm(crud, listing, {"colour": "#0000FF"})
    wait_run_ended(client, listing)
    # Red had its price from publish: only blue was sliced.
    assert blue["price_key"] != red["price_key"]
    assert len(sliced.started) == 2
    # Prepare again: every variant has a done price, so nothing is sliced.
    assert client.post(prepare_path(listing)).status_code in (200, 202)
    wait_run_ended(client, listing)
    assert len(sliced.started) == 2


def test_a_variant_waits_for_room_under_the_slice_cap(
    client: TestClient,
    crud: CrudCaller,
    template: dict[str, Any],
    dense_red: str,
    sliced: type[FakeSlicer],
) -> None:
    """`storefront_slices_concurrent` 1, held by another slice: the warmed variant starts
    nothing while it runs, and is sliced once there is room."""
    prices = _state(crud.api).components.get(ORDERS).slice_prices
    assert prices.claim("f" * 64, "0" * 64, limit=1).claimed
    listing = publish(client, create(client, listing_body(template, dense_red))["id"])
    deadline = time.monotonic() + 60
    while not rows(crud, "SELECT 1 FROM render_jobs WHERE state = 'done'"):
        assert time.monotonic() < deadline, "the default was never rendered"
        time.sleep(0.2)
    time.sleep(3)
    assert sliced.started == []
    assert instant(crud, listing, {})["source"] == "none"
    with _state(crud.api).projection.pool.connection() as conn:
        conn.execute("DELETE FROM storefront_slice_prices WHERE price_key = %s", ("f" * 64,))
    firm(crud, listing, {})
    assert len(sliced.started) == 1


def started_defaults(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Each listing a default price run was started for, with no run started."""
    started: list[str] = []

    async def start_default(
        _self: prepare.TemporalPreparer, listing_id: str, *, restart: bool = True
    ) -> None:
        started.append(listing_id)

    monkeypatch.setattr(prepare.TemporalPreparer, "start_default", start_default)
    return started


def test_a_pictures_change_on_a_published_listing_warms_its_default_again(
    client: TestClient,
    template: dict[str, Any],
    dense_red: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #6970: a pictures change republishes a published listing, a new version
    whose variants have new keys, so it starts the default's run again; a draft's starts
    none."""
    started = started_defaults(monkeypatch)
    draft = create(client, listing_body(template, dense_red))
    body = {"media_ids": [template["media_id"]], "updated_at": draft["updated_at"]}
    r = client.put(f"{LISTINGS}/{draft['id']}/media", json=body)
    assert r.status_code == 200, r.text
    assert started == []
    listing = publish(client, draft["id"])
    assert started == [listing["id"]]
    body = {"media_ids": [template["media_id"]], "updated_at": listing["updated_at"]}
    r = client.put(f"{LISTINGS}/{listing['id']}/media", json=body)
    assert r.status_code == 200, r.text
    assert started == [listing["id"], listing["id"]]


def test_a_publish_answers_when_temporal_does_not(
    client: TestClient,
    template: dict[str, Any],
    dense_red: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review of #6970: a start that stalls is given up after `WARM_START_TIMEOUT` and
    logged as given up (the run may yet start); the publish answers."""

    async def stalls(_self: prepare.TemporalPreparer, _listing_id: str) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(prepare.TemporalPreparer, "start_default", stalls)
    monkeypatch.setattr(prepare, "WARM_START_TIMEOUT", 0.2)
    began = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=prepare.logger.name):
        listing = publish(client, create(client, listing_body(template, dense_red))["id"])
    assert listing["published"] is True
    assert time.monotonic() - began < 30
    given_up = f"gave up waiting for the default price run of listing {listing['id']} to start"
    [record] = [r for r in caplog.records if r.getMessage() == given_up]
    assert record.exc_info is not None


def swept(client: TestClient) -> int:
    """One pass of the sweep (#7665), on the app's own event loop."""
    state = _state(client)
    portal = client.portal
    assert portal is not None
    started: int = portal.call(prepare.warm_unpriced, state, state.components.get(ORDERS))
    return started


def newest_runs(
    monkeypatch: pytest.MonkeyPatch, runs: dict[str, prepare.DefaultRun]
) -> dict[str, prepare.DefaultRun]:
    """What Temporal holds of each listing's default run, by listing id."""

    async def last_default(
        _self: prepare.TemporalPreparer, listing_id: str
    ) -> prepare.DefaultRun | None:
        return runs.get(listing_id)

    monkeypatch.setattr(prepare.TemporalPreparer, "last_default", last_default)
    return runs


def version_of(listing: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(listing["updated_at"])


def unwarmed(
    client: TestClient, template: dict[str, Any], red: str, count: int = 1
) -> list[dict[str, Any]]:
    """Published listings that publish priced nothing of (their starts were recorded only)."""
    body = listing_body(template, red)
    return [publish(client, create(client, body)["id"]) for _ in range(count)]


def test_the_sweep_prices_a_published_listing_with_no_price(
    client: TestClient,
    crud: CrudCaller,
    template: dict[str, Any],
    dense_red: str,
    sliced: type[FakeSlicer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7665: a listing published with no run (as before #6922) has no price until the
    sweep starts one; once it has its price, the sweep leaves it alone."""
    with monkeypatch.context() as patched:
        started_defaults(patched)
        [listing] = unwarmed(client, template, dense_red)
    draft = create(client, listing_body(template, dense_red))
    assert instant(crud, listing, {})["source"] == "none"
    assert swept(client) == 1
    assert firm(crud, listing, {})["price_minor"] > 0
    started = started_defaults(monkeypatch)
    newest_runs(monkeypatch, {})
    assert swept(client) == 0
    assert started == [] and draft["published"] is False
    assert len(sliced.started) == 1


def test_the_sweep_tries_a_version_of_a_listing_once(
    client: TestClient,
    template: dict[str, Any],
    dense_red: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7665: a listing with no price to find (its run ended with none) is not started on
    each pass: only while its newest run started before this version of it."""
    started = started_defaults(monkeypatch)
    [listing] = unwarmed(client, template, dense_red)
    started.clear()
    version = version_of(listing)
    since = prepare.DefaultRun(running=False, started_at=version + timedelta(seconds=1))
    runs = newest_runs(monkeypatch, {listing["id"]: since})
    assert swept(client) == 0
    assert started == []
    before = prepare.DefaultRun(running=False, started_at=version - timedelta(seconds=1))
    runs[listing["id"]] = before
    assert swept(client) == 1
    assert started == [listing["id"]]


def test_the_sweep_leaves_only_a_few_runs_open(
    client: TestClient,
    template: dict[str, Any],
    dense_red: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7665: at most `WARM_SWEEP_BATCH` default runs are open, those already open
    counted, so visitors' renders keep their room; the next pass starts the rest."""
    started = started_defaults(monkeypatch)
    listings = unwarmed(client, template, dense_red, 4)
    started.clear()
    runs = newest_runs(monkeypatch, {})
    ids = [listing["id"] for listing in listings]
    now = datetime.now(UTC)
    running = prepare.DefaultRun(running=True, started_at=now)
    ended = prepare.DefaultRun(running=False, started_at=now)
    assert swept(client) == prepare.WARM_SWEEP_BATCH == 2
    first = list(started)
    # One of them still runs: one more starts.
    runs.update({first[0]: running, first[1]: ended})
    assert swept(client) == 1
    runs.update({first[0]: ended, started[-1]: ended})
    assert swept(client) == 1
    assert sorted(started) == sorted(ids)


def test_the_sweep_goes_on_past_a_listing_that_fails(
    client: TestClient,
    template: dict[str, Any],
    dense_red: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = started_defaults(monkeypatch)
    first, second = unwarmed(client, template, dense_red, 2)
    started.clear()

    async def last_default(
        _self: prepare.TemporalPreparer, listing_id: str
    ) -> prepare.DefaultRun | None:
        if listing_id == first["id"]:
            raise RuntimeError("away")
        return None

    monkeypatch.setattr(prepare.TemporalPreparer, "last_default", last_default)
    with caplog.at_level(logging.WARNING, logger=prepare.logger.name):
        assert swept(client) == 1
    assert started == [second["id"]]
    assert f"could not sweep the default price of listing {first['id']}" in caplog.text


def test_the_sweep_obeys_the_warm_prices_switch(
    client: TestClient,
    template: dict[str, Any],
    dense_red: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = started_defaults(monkeypatch)
    unwarmed(client, template, dense_red)
    started.clear()
    newest_runs(monkeypatch, {})
    monkeypatch.setattr(_state(client).settings, "storefront_warm_prices", False)
    assert swept(client) == 0
    assert started == []
