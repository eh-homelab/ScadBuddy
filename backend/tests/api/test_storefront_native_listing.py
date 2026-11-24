# ruff: noqa: F811  (pytest fixtures imported by name, then taken as arguments)
"""A file listing without Bambuddy (#7467): on the native library with no
``bambuddy_url`` stored, it publishes, prices, sells and prints, from the library's file or
from its held copy put back, and no Bambuddy client is built.

Two things outside the process are faked: the slicer, where a Python `slice_plate`
answers in the Rust worker's place (`tests/support/fake_slicer.py`) with a slice of its
own, and the printer, a fake one on MQTT and FTPS (`tests/support/fake_lan.py`). The
content store's S3 is moto's. Everything else is ScadBuddy's own code: publish, the
storefront's CRUD routes, the price operation and its background slice, the order, its
print operation and the print run's workflow on a Temporal dev server. No payment provider
is set, so the order stands accepted at its checkout."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.library.settings_store import SettingsPatch, StoredSettings
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.served_library import STOREFRONT_FOLDER
from scadbuddy.orders.storefront.slice_profile import StorefrontSliceProfile
from tests.api.test_hot_path_native import (  # noqa: F401  (fixtures)
    NO_BAMBUDDY,
    fake_lan,
    held_spool,
    kek_file,
    lan_row,
    native_app,
    run_body,
    slicer,
    two_colours,
)
from tests.api.test_order_print import answered
from tests.api.test_print_run_choices import follow_run
from tests.api.test_storefront_file_listings import (  # noqa: F401  (fixtures)
    _native,
    _state,
    _trash,
    body,
    create,
    pla_red,
    publish,
    upload,
)
from tests.api.test_storefront_file_listings import two_colours as keychain_3mf
from tests.api.test_storefront_file_prices import add, priced, submit
from tests.api.test_storefront_renders import CrudCaller
from tests.support.fake_lan import FakeLan, needs_fake_lan
from tests.support.fake_slicer import FakeSlicer
from tests.support.operations import press
from tests.support.sb_rows import seed_app_type

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal, *needs_fake_lan]


@pytest.fixture
def client(native_app: TestClient) -> TestClient:
    """The hot path's app, under the name the storefront tests' fixtures take."""
    return native_app


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Where each Bambuddy client was built, or its settings asked for: a build with no URL
    stored is a 409 a caller may swallow, so the try counts too. The housekeeping's slice
    sweeps are left out (`main._with_bambuddy`): the prune Schedule may run them at any
    time, for no listing, and they do nothing with no URL."""
    found: list[str] = []
    config = BambuddyConfig.from_settings
    init = BambuddyClient.__init__

    def note() -> None:
        stack = traceback.extract_stack()
        if not any(frame.name == "_with_bambuddy" for frame in stack):
            found.append("".join(traceback.format_list(stack[-14:])))

    def from_settings(cls: type[BambuddyConfig], settings: StoredSettings) -> BambuddyConfig:
        note()
        return config(settings)

    def build(self: BambuddyClient, *args: Any, **kwargs: Any) -> None:
        note()
        init(self, *args, **kwargs)

    monkeypatch.setattr(BambuddyConfig, "from_settings", classmethod(from_settings))
    monkeypatch.setattr(BambuddyClient, "__init__", build)
    return found


def _print(client: TestClient, order_id: str, item_id: str, row: str) -> dict[str, Any]:
    """The item printed from the order on the dialog's choices, and its run's end."""
    sent = run_body(row, {"slots": [{"slot_id": 1}, {"slot_id": 2}]})
    attempt = answered(
        client,
        client.post(f"/api/v1/orders/{order_id}/items/{item_id}/print", json=sent, headers=press()),
    )
    return follow_run(client, attempt["print_run_id"], timeout=120)


@NO_BAMBUDDY
@pytest.mark.parametrize("gone", [False, True], ids=["in-the-library", "restored"])
def test_a_file_listing_publishes_prices_and_prints_without_bambuddy(
    client: TestClient,
    fake_lan: FakeLan,
    slicer: FakeSlicer,
    pla_red: str,
    tmp_path: Path,
    built: list[str],
    gone: bool,
) -> None:
    """With ``gone``, the file is trashed after the sale: the print puts the held copy back
    into the library's "Storefront" folder and prints that file."""
    state = _state(client)
    assert state.settings_store.load().bambuddy_url is None
    row = lan_row(client, fake_lan)
    held_spool(client)
    profile = StorefrontSliceProfile(
        printer=row,
        materials={"pla": seed_app_type(client, "PLA-W", "PLA", {"0.4": "Generic PLA @BBL H2C"})},
    )
    state.settings_store.save(SettingsPatch(storefront_slice_profile=profile))
    file = upload(client, keychain_3mf(tmp_path / "keychain.3mf"))

    listing = publish(client, create(client, body(file, [pla_red]))["id"])
    assert listing["snapshot"]["sha256"] == file.sha256

    crud = CrudCaller(client)
    colours = {0: pla_red, 1: pla_red}
    est = priced(crud, listing, colours)
    assert est["source"] == "slice" and est["price_minor"] > 0
    assert slicer.calls == 1

    draft = add(crud, listing, colours)
    assert draft.status_code == 200, draft.text
    [line] = draft.json()["items"]
    assert (line["price"], line["unit_price_minor"]) == ("firm", est["price_minor"])
    sent = submit(crud, draft.json())
    assert sent.status_code == 200, sent.text
    item = state.components.get(ORDERS).store.item(line["id"])
    assert item is not None
    order = client.get(f"/api/v1/orders/{item.order_id}").json()
    assert order["order"]["status"] == "accepted", order

    if gone:
        _trash(client, file)
    run = _print(client, item.order_id, item.id, row)
    assert run["status"] == "succeeded", run
    assert run["result"]["path"] == "native"
    printed = state.components.get(ORDERS).store.item(item.id)
    assert printed is not None and printed.library_file is not None
    file_id = printed.library_file.id
    assert run["subject"] == f"library:{file_id}"
    assert (file_id != file.id) == gone
    if gone:
        assert client.portal is not None
        put_back = client.portal.call(_native(client).library_file, file_id)
        assert put_back.file_hash == file.sha256
        folder = client.portal.call(_native(client).folder, put_back.folder_id or 0)
        assert folder.name == STOREFRONT_FOLDER
    assert built == [], "\n\n".join(built)
