"""A customer's own models as the customer reads them (#6133 PR 4, spec 2026-11-13 §4.3,
§7 "Serving the preview", §10.5): the cart's lines and their previews, the submit that a
refused file blocks, and the order page's sources and charges. A preview is served as the
check stored it, opaque bytes with fixed headers, only to the reader of its own order."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.orders.storefront.mail import issue_link
from scadbuddy.orders.storefront.model_requests import ENDED, EXPIRED
from scadbuddy.storefront.wire import SESSION_HEADER, STOREFRONT_PROBLEMS
from tests.orders.storefront.test_drafts import (  # noqa: F401  (fixtures)
    CRUD,
    TOKEN_B,
    CrudCaller,
    _orders,
    _state,
    asks,
    s3_api,
)
from tests.orders.storefront.test_intake import (  # noqa: F401  (fixtures)
    FACTS,
    PNG,
    SHA,
    Intake,
    claimed,
    put_preview,
    worker,
)
from tests.orders.storefront.test_model_requests import (  # noqa: F401  (fixtures)
    STL,
    FakeChecker,
    FakeWatcher,
    ask,
    checker,
    rows,
    submitted,
    uploaded,
    watcher,
)
from tests.support.store import s3  # noqa: F401  (fixture)

pytestmark = pytest.mark.requires_postgres

#: The headers every served preview carries, whatever its bytes (spec §7).
PREVIEW_HEADERS = {
    "content-type": "image/png",
    "content-disposition": "inline",
    "x-content-type-options": "nosniff",
}


def ready(asks: CrudCaller, worker: Intake) -> tuple[str, str, str]:  # noqa: F811
    """A cart with one checked file: (order id, source id, upload id)."""
    source_id, upload_id = claimed(asks)
    put_preview(worker, source_id)
    r = worker.check(source_id, {"outcome": "ready", "sha256": SHA, "facts": FACTS})
    assert r.json() == {"recorded": True}, r.text
    [row] = rows(asks.api, "SELECT order_id FROM order_item_sources WHERE id = %s", source_id)
    return row["order_id"], source_id, upload_id


def preview_headers(response: Any) -> dict[str, str]:
    return {k: response.headers.get(k) for k in PREVIEW_HEADERS}


def link_token(api: TestClient, order_id: str) -> str:
    orders = _orders(api)
    order = orders.store.order(order_id)
    assert order is not None
    with orders.store.transaction() as conn:
        return issue_link(conn, orders.lifecycle.kek, order_id, order.label, "ada@example.com")


def order_post(asks: CrudCaller, path: str, body: dict[str, Any], **headers: str) -> Any:  # noqa: F811
    return asks.api.post(f"{CRUD}/order/{path}", json=body, headers={**asks.bearer, **headers})


def slug(response: Any) -> str:
    return str(response.json()["type"]).removeprefix(STOREFRONT_PROBLEMS)


# -- the cart ----------------------------------------------------------------------------


def test_the_carts_line_names_its_preview_once_the_file_is_ready(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
) -> None:
    _order_id, source_id, _ = ready(asks, worker)
    [item] = asks.get("/draft").json()["items"]
    [source] = item["sources"]
    assert source["check_status"] == "ready"
    assert source["preview_url"] == f"/api/store/v1/draft/sources/{source_id}/preview"


def test_a_line_still_checking_names_no_preview(asks: CrudCaller) -> None:  # noqa: F811
    claimed(asks)
    [item] = asks.get("/draft").json()["items"]
    [source] = item["sources"]
    assert (source["check_status"], source["preview_url"]) == ("checking", None)


def test_the_visitor_reads_its_lines_preview_with_fixed_headers(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
) -> None:
    _order_id, source_id, _ = ready(asks, worker)
    r = asks.get(f"/draft/sources/{source_id}/preview")
    assert r.status_code == 200, r.text
    assert r.content == PNG
    assert preview_headers(r) == PREVIEW_HEADERS
    assert "no-store" in r.headers["cache-control"] or "private" in r.headers["cache-control"]


@pytest.mark.parametrize("source", ["other-visitor", "unknown", "not-an-id"])
def test_no_one_else_reads_a_lines_preview(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
    source: str,
) -> None:
    _order_id, source_id, _ = ready(asks, worker)
    if source == "other-visitor":
        r = asks.get(f"/draft/sources/{source_id}/preview", token=TOKEN_B)
    elif source == "unknown":
        r = asks.get("/draft/sources/00000000-0000-4000-8000-000000000000/preview")
    else:
        r = asks.get("/draft/sources/not-an-id/preview")
    assert (r.status_code, r.content) == (404, b"")


def test_a_refused_file_blocks_the_submit(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
) -> None:
    source_id, _ = claimed(asks)
    refused = worker.check(source_id, {"outcome": "refused", "reason": "Not a readable zip."})
    assert refused.json() == {"recorded": True}
    [row] = rows(asks.api, "SELECT order_id FROM order_item_sources WHERE id = %s", source_id)
    body = {"draft_id": row["order_id"], "name": "Ada", "email": "ada@example.com"}
    r = asks.post("/draft/submit", json=body, key="submit-1")
    assert r.status_code == 409 and slug(r) == "model_file_refused", r.text
    assert r.json()["sources"] == [source_id]
    [order] = rows(asks.api, "SELECT submitted_at FROM orders WHERE id = %s", row["order_id"])
    assert order["submitted_at"] is None


def test_an_expired_file_blocks_the_submit_too(asks: CrudCaller) -> None:  # noqa: F811
    source_id, _ = claimed(asks)
    with _state(asks.api).projection.pool.connection() as conn:
        conn.execute(
            "UPDATE order_item_sources SET check_status = 'refused', check_reason = %s",
            (EXPIRED,),
        )
    [row] = rows(asks.api, "SELECT order_id FROM order_item_sources WHERE id = %s", source_id)
    body = {"draft_id": row["order_id"], "name": "Ada", "email": "ada@example.com"}
    r = asks.post("/draft/submit", json=body, key="submit-1")
    assert r.status_code == 409 and slug(r) == "model_file_refused"


def test_a_file_still_checking_does_not_block_the_submit(asks: CrudCaller) -> None:  # noqa: F811
    source_id, _ = claimed(asks)
    [row] = rows(asks.api, "SELECT order_id FROM order_item_sources WHERE id = %s", source_id)
    submitted(asks, row["order_id"])


# -- the order page ----------------------------------------------------------------------


def sent_order(asks: CrudCaller, worker: Intake) -> tuple[str, str, str]:  # noqa: F811
    """A sent order with one checked file, one model link and one thing to buy: (order id,
    file source id, the link's token)."""
    [upload_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    links = [
        {"url": "https://www.printables.com/model/123-cube", "purpose": "print"},
        {"url": "https://parts.example.com/magnets", "purpose": "purchase", "note": "6 mm"},
    ]
    order_id = ask(asks, {"uploads": [upload_id], "links": links}).json()["id"]
    [source] = rows(asks.api, "SELECT id FROM order_item_sources WHERE upload_id = %s", upload_id)
    put_preview(worker, source["id"])
    worker.check(source["id"], {"outcome": "ready", "sha256": SHA, "facts": FACTS})
    submitted(asks, order_id)
    return order_id, source["id"], link_token(asks.api, order_id)


def test_the_order_page_shows_each_source_and_every_charge(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
) -> None:
    order_id, file_id, token = sent_order(asks, worker)
    r = order_post(asks, "view", {"token": token})
    assert r.status_code == 200, r.text
    view = r.json()
    [item] = view["items"]
    file, link, purchase = item["sources"]
    assert file == {
        "kind": "file",
        "purpose": "print",
        "filename": "cube.stl",
        "url": None,
        "note": "",
        "status": "open",
        "amount_minor": None,
        "shop_note": "",
        "preview_url": f"/api/store/v1/order/sources/{file_id}/preview",
        "customer_reason": None,
    }
    assert (link["kind"], link["url"], link["preview_url"]) == (
        "link",
        "https://www.printables.com/model/123-cube",
        None,
    )
    assert (purchase["purpose"], purchase["status"], purchase["note"]) == (
        "purchase",
        "pending",
        "6 mm",
    )
    assert view["charges"] == []
    # The shop records the purchase: the customer sees its charge and its amount.
    [source] = rows(asks.api, "SELECT id FROM order_item_sources WHERE purpose = 'purchase'")
    recorded = asks.api.post(
        f"/api/v1/orders/{order_id}/purchases/{source['id']}/record",
        json={"amount_minor": 450, "note": "supplier invoice 12"},
    )
    assert recorded.status_code == 201, recorded.text
    view = order_post(asks, "view", {"token": token}).json()
    assert view["items"][0]["sources"][2]["amount_minor"] == 450
    [charge] = view["charges"]
    assert charge == {
        "kind": "purchase",
        "label": "Bought for your project: 6 mm",
        "amount_minor": 450,
    }
    assert "supplier invoice" not in r.text + str(view)


def test_only_an_ended_orders_reason_reaches_the_customer(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
) -> None:
    _order_id, file_id, token = sent_order(asks, worker)
    with _state(asks.api).projection.pool.connection() as conn:
        conn.execute(
            "UPDATE order_item_sources SET status = 'manual',"
            " status_reason = 'Bambuddy: sign in to MakerWorld' WHERE kind = 'link'"
            " AND purpose = 'print'"
        )
        conn.execute(
            "UPDATE order_item_sources SET status = 'declined', status_reason = %s,"
            " upload_id = NULL WHERE id = %s",
            (ENDED, file_id),
        )
    view = order_post(asks, "view", {"token": token}).json()
    file, link, _ = view["items"][0]["sources"]
    assert (file["status"], file["customer_reason"]) == ("declined", ENDED)
    assert (link["status"], link["customer_reason"]) == ("manual", None)
    assert "Bambuddy" not in str(view)


def test_the_reader_reads_a_sources_preview_with_fixed_headers(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
) -> None:
    _order_id, file_id, token = sent_order(asks, worker)
    r = order_post(asks, f"sources/{file_id}/preview", {"token": token})
    assert r.status_code == 200, r.text
    assert r.content == PNG
    assert preview_headers(r) == PREVIEW_HEADERS


def test_a_signed_in_reader_reads_the_preview_by_the_orders_id(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
) -> None:
    from scadbuddy.orders.storefront.accounts import attach_order
    from tests.orders.storefront.test_accounts import sign_in

    order_id, file_id, _token = sent_order(asks, worker)
    with _orders(asks.api).store.transaction() as conn:
        signed = sign_in(conn, "ada@example.com")
        attach_order(conn, signed.account.id, order_id)
    r = order_post(
        asks,
        f"sources/{file_id}/preview",
        {"order_id": order_id},
        **{SESSION_HEADER: signed.session_token},
    )
    assert r.status_code == 200, r.text
    assert r.content == PNG
    anonymous = order_post(asks, f"sources/{file_id}/preview", {"order_id": order_id})
    assert (anonymous.status_code, anonymous.content) == (404, b"")


def test_no_other_reader_reads_a_sources_preview(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
) -> None:
    _order_id, file_id, token = sent_order(asks, worker)
    # Another order's link names this order's source: a bare 404.
    [other_upload] = uploaded(asks, [("cube.stl", STL, "model/stl")], key="u2", token=TOKEN_B)
    other_order = ask(asks, {"uploads": [other_upload]}, key="m2", token=TOKEN_B).json()["id"]
    body = {"draft_id": other_order, "name": "Bo", "email": "bo@example.com"}
    assert asks.post("/draft/submit", json=body, key="s2", token=TOKEN_B).status_code == 200
    other_token = link_token(asks.api, other_order)
    for r in (
        order_post(asks, f"sources/{file_id}/preview", {"token": other_token}),
        order_post(asks, f"sources/{file_id}/preview", {"token": "x" * 43}),
        order_post(asks, f"sources/{file_id}/preview", {}),
        order_post(asks, "sources/not-an-id/preview", {"token": token}),
    ):
        assert (r.status_code, r.content) == (404, b"")
