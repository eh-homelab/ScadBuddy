"""A past print as an order item over the API (#4777, order operations spec 2026-11-07
§3.2): `POST /orders/{id}/items` with `archive_id`, and the `archive` quote route."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api import orders as api_orders
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.models import FilamentInput
from scadbuddy.orders.problems import PROBLEMS
from scadbuddy.orders.reference import TemplateDeletedError
from scadbuddy.orders.store import OrderStore
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_send import make_output
from tests.api.test_versions import SLUG, upload
from tests.orders.test_past_prints import Seam, plate

pytestmark = pytest.mark.requires_git


def orders_of(client: TestClient) -> OrdersComponent:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(ORDERS)


def linked(store: OrderStore, subject: str, run_id: str, plates: dict[int, int]) -> None:
    """A run of ``subject`` whose plate ``n``'s queue item made archive ``plates[n]``."""
    with store.transaction() as conn:
        for plate_id, archive_id in plates.items():
            queue_item = archive_id * 10
            conn.execute(
                "INSERT INTO print_sends (queue_item_id, subject, run_id, plate_id)"
                " VALUES (%s, %s, %s, %s)",
                (queue_item, subject, run_id, plate_id),
            )
            conn.execute(
                "INSERT INTO print_links (subject, archive_id, matched_by, queue_item_id)"
                " VALUES (%s, %s, 'queue_item', %s)",
                (subject, archive_id, queue_item),
            )


def add(client: TestClient, order_id: str, **body: object):  # type: ignore[no-untyped-def]
    return client.post(f"/api/v1/orders/{order_id}/items", json={"quantity": 1, **body})


def test_an_archive_of_an_output_adds_that_outputs_item(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    output = make_output(client, SLUG)
    linked(orders_of(client).store, f"output:{output}", "run-1", {1: 81, 2: 82})
    added = add(client, order["id"], archive_id=82)
    assert added.status_code == 201, added.text
    item = added.json()
    assert (item["kind"], item["output_id"], item["origin_archive_ids"]) == (
        "model",
        output,
        [81, 82],
    )
    assert item["reference"]["slug"] == SLUG


def test_an_archive_of_a_deleted_templates_output_falls_through_to_the_copy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    output = make_output(client, SLUG)
    linked(orders_of(client).store, f"output:{output}", "run-1", {1: 81})
    # The model's delete takes its outputs with it (a 422 the add already passes over);
    # an output kept while its template is a tombstone answers `template_deleted`.

    async def tombstone(*args: object, **kwargs: object) -> None:
        raise TemplateDeletedError(SLUG)

    monkeypatch.setattr(api_orders, "resolve_reference", tombstone)
    # Its printed file is still Bambuddy's: the add goes on to the copy, which needs the
    # native library.
    refused = add(client, order["id"], archive_id=81)
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"] == PROBLEMS + "archive_copy_not_native"


def test_an_archive_is_the_items_only_source(client: TestClient) -> None:
    order = create_order(client, create_customer(client)["id"])
    for extra in ({"output_id": "f" * 32}, {"library_file_id": 9}, {"kind": "model"}):
        refused = add(client, order["id"], archive_id=81, **extra)
        assert refused.status_code == 422, (extra, refused.text)


def test_a_foreign_archive_needs_the_native_library(client: TestClient) -> None:
    order = create_order(client, create_customer(client)["id"])
    refused = add(client, order["id"], archive_id=71)
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"] == PROBLEMS + "archive_copy_not_native"


def test_the_archive_quote_prices_the_item_from_its_print(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    output = make_output(client, SLUG)
    orders = orders_of(client)
    linked(orders.store, f"output:{output}", "run-1", {1: 81})
    item = add(client, order["id"], archive_id=81).json()
    pla = orders.store.insert_filament(
        FilamentInput(material="PLA", colour="#FF0000", cost_per_kg="20")
    ).id
    path = f"/api/v1/orders/{order['id']}/items/{item['id']}/quotes/archive"
    slots = {str(slot["slot"]): pla for slot in item["colours"]}
    # Without a Bambuddy there is no archive to read.
    unlinked = client.post(path, json={"filaments": slots})
    assert unlinked.status_code == 409, unlinked.text
    assert unlinked.json()["type"] == PROBLEMS + "not_linked"
    one_slot = [{"slot_id": 1, "used_g": 10.0, "color": "#FF0000"}]
    seam = Seam(plate(81, slots=one_slot))
    monkeypatch.setattr(OrdersComponent, "integrations", property(lambda self: seam))
    quoted = client.post(path, json={"filaments": slots})
    assert quoted.status_code == 201, quoted.text
    quote = quoted.json()
    assert (quote["source"], quote["plates"][0]["archive_id"]) == ("archive", 81)
    assert quote["plates"][0]["print_seconds"] == 1800
