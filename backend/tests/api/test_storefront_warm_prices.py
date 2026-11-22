# ruff: noqa: F811  (pytest fixtures imported by name, then taken as arguments)
"""The shop warms its listings' prices (#6922): publish renders the default variant and
starts its storefront slice price, and Prepare colours starts the price of each prepared
variant, both through the visitor's own path (`slice_price.py` `slice_estimate`), so the
instant read (``POST …/listings/{id}/variant``) has a firm price before any visitor asks.
A variant that has a ``done`` price starts no slice, and `storefront_slices_concurrent`
holds: a variant with no room waits for one."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import trimesh
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.orders.component import ORDERS
from tests.api.conftest import set_fake_env
from tests.api.test_storefront_listings import (  # noqa: F401  (fixtures)
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
