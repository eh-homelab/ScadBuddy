# ruff: noqa: F811  (pytest fixtures imported by name, then taken as arguments)
"""Publish prices a file listing's default colours (#7523): the run of #6922 submits no
render and starts the slice a first visitor's page asks for, with no visitor charged."""

from __future__ import annotations

import time
from typing import Any

import pytest

import scadbuddy.main as main_module
from scadbuddy.core.settings import Settings
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.storefront import prepare
from scadbuddy.print_library.store import NativeFile
from tests.api.test_storefront_file_listings import (  # noqa: F401  (fixtures)
    _state,
    body,
    client,
    create,
    keychain,
    pla_red,
    publish,
)
from tests.api.test_storefront_file_prices import TWO_SLOTS, pla_blue, read, rows  # noqa: F401
from tests.api.test_storefront_renders import (  # noqa: F401  (fixtures)
    CrudCaller,
    FakeSlicer,
    crud,
    sliced,
)

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


def page_choice(crud: CrudCaller, listing: dict[str, Any]) -> dict[int, str]:
    """The colours a visitor's page opens on, as the public listing gives them."""
    r = crud.get(f"/listings/{listing['id']}")
    assert r.status_code == 200, r.text
    return {part["slot"]: part["default"] for part in r.json()["slots"]}


def wait_for(crud: CrudCaller, sql: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 90
    while True:
        found = rows(crud, sql)
        if found:
            return found
        assert time.monotonic() < deadline, f"no row: {sql}"
        time.sleep(0.3)


def test_publish_prices_the_default_colours(
    crud: CrudCaller,
    sliced: type[FakeSlicer],
    keychain: NativeFile,
    pla_red: str,
    pla_blue: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FakeSlicer, "file", TWO_SLOTS)
    listing = publish(crud.api, create(crud.api, body(keychain, [pla_red, pla_blue]))["id"])
    [row] = wait_for(
        crud,
        "SELECT price_key, listing_id, output_id, setup_key, slot_filaments"
        " FROM storefront_slice_prices WHERE status = 'done'",
    )
    default = page_choice(crud, listing)
    assert row["slot_filaments"] == [default[0], default[1]]
    assert (row["listing_id"], row["output_id"]) == (listing["id"], None)
    assert row["setup_key"]
    # No visitor asked: nothing is counted, and no render ran.
    assert rows(crud, "SELECT 1 FROM storefront_renders") == []
    assert rows(crud, "SELECT 1 FROM render_jobs") == []
    r = read(crud, listing, default)
    assert r.status_code == 200, r.text
    assert (r.json()["source"], r.json()["price_key"]) == ("slice", row["price_key"])
    # A publish again slices nothing: the price stands.
    publish(crud.api, listing["id"])
    time.sleep(3)
    assert len(sliced.started) == 1


def test_the_default_colours_wait_for_room_under_the_slice_cap(
    crud: CrudCaller,
    sliced: type[FakeSlicer],
    keychain: NativeFile,
    pla_red: str,
    pla_blue: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FakeSlicer, "file", TWO_SLOTS)
    prices = _state(crud.api).components.get(ORDERS).slice_prices
    assert prices.claim("f" * 64, "0" * 64, limit=1).claimed
    publish(crud.api, create(crud.api, body(keychain, [pla_red, pla_blue]))["id"])
    time.sleep(3)
    assert sliced.started == []
    with _state(crud.api).projection.pool.connection() as conn:
        conn.execute("DELETE FROM storefront_slice_prices WHERE price_key = %s", ("f" * 64,))
    wait_for(crud, "SELECT 1 FROM storefront_slice_prices WHERE status = 'done'")
    assert len(sliced.started) == 1


def test_the_sweep_prices_a_file_listing_with_no_price(
    crud: CrudCaller,
    sliced: type[FakeSlicer],
    keychain: NativeFile,
    pla_red: str,
    pla_blue: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7665: a file listing whose publish started no run is priced by the sweep, once."""
    monkeypatch.setattr(FakeSlicer, "file", TWO_SLOTS)
    state = _state(crud.api)
    portal = crud.api.portal
    assert portal is not None
    started: list[str] = []

    async def start_default(
        _self: prepare.TemporalPreparer, listing_id: str, *, restart: bool = True
    ) -> None:
        started.append(listing_id)

    with monkeypatch.context() as patched:
        patched.setattr(prepare.TemporalPreparer, "start_default", start_default)
        listing = publish(crud.api, create(crud.api, body(keychain, [pla_red, pla_blue]))["id"])
    orders = state.components.get(ORDERS)
    assert started == [listing["id"]]
    assert portal.call(prepare.warm_unpriced, state, orders) == 1
    wait_for(crud, "SELECT 1 FROM storefront_slice_prices WHERE status = 'done'")
    monkeypatch.setattr(prepare.TemporalPreparer, "start_default", start_default)

    async def no_run(_self: prepare.TemporalPreparer, _listing_id: str) -> None:
        return None

    monkeypatch.setattr(prepare.TemporalPreparer, "last_default", no_run)
    assert portal.call(prepare.warm_unpriced, state, orders) == 0
    assert len(sliced.started) == 1


def test_the_shop_can_turn_it_off(
    crud: CrudCaller,
    sliced: type[FakeSlicer],
    keychain: NativeFile,
    pla_red: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(crud.api)
    monkeypatch.setattr(state.settings, "storefront_warm_prices", False)
    publish(crud.api, create(crud.api, body(keychain, [pla_red]))["id"])
    time.sleep(2)
    assert rows(crud, "SELECT 1 FROM storefront_slice_prices") == []
