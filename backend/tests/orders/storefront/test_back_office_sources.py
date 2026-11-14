"""The back office's "Customer's models" panel over the routes (#6133 PR 4, spec 2026-11-13
§7, §8.1, §9.3, §11): the order shows its sources and the colours asked for; a person
downloads a staged file, sees its preview with fixed headers, runs a failed check again,
declines a link, and retries a stuck import; the orders list filters by own model,
purchase pending and import stuck."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scadbuddy.orders.models import IMPORT_STUCK
from tests.orders.storefront.test_customer_sources import PREVIEW_HEADERS, preview_headers
from tests.orders.storefront.test_drafts import (  # noqa: F401  (fixtures)
    CrudCaller,
    _state,
    asks,
    s3_api,
)
from tests.orders.storefront.test_intake import (  # noqa: F401  (fixtures)
    FACTS,
    PNG,
    SHA,
    Intake,
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

ORDERS = "/api/v1/orders"
MAKERWORLD = "https://makerworld.com/en/models/123-cube"
PRINTABLES = "https://www.printables.com/model/77-lid"


def sent(asks: CrudCaller, worker: Intake, outcome: str = "ready") -> tuple[str, dict[str, str]]:  # noqa: F811
    """A sent order with one file (checked ``outcome``), a MakerWorld link, a Printables
    link and a thing to buy: its id, and each source's id by name."""
    [upload_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    links = [
        {"url": MAKERWORLD},
        {"url": PRINTABLES},
        {"url": "https://parts.example.com/m", "purpose": "purchase", "note": "magnets"},
    ]
    body = {"uploads": [upload_id], "links": links, "colours": {"shop_choice": True}}
    order_id = ask(asks, body).json()["id"]
    ids = {
        r["name"]: r["id"]
        for r in rows(
            asks.api,
            "SELECT id, coalesce(filename, link_type || ':' || purpose) AS name"
            " FROM order_item_sources WHERE order_id = %s",
            order_id,
        )
    }
    file_id = ids["cube.stl"]
    if outcome == "ready":
        put_preview(worker, file_id)
        result: dict[str, Any] = {"outcome": "ready", "sha256": SHA, "facts": FACTS}
    elif outcome == "failed":
        result = {"outcome": "failed", "reason": "The check timed out."}
    else:
        result = {"outcome": outcome}
    if outcome != "checking":
        assert worker.check(file_id, result).json() == {"recorded": True}
    submitted(asks, order_id)
    ids["upload"] = upload_id
    return order_id, ids


def detail(api: TestClient, order_id: str) -> dict[str, Any]:
    r = api.get(f"{ORDERS}/{order_id}")
    assert r.status_code == 200, r.text
    found: dict[str, Any] = r.json()
    return found


def slug(response: Any) -> str:
    return str(response.json().get("type", "")).rsplit("/", 1)[-1]


def test_the_order_shows_its_sources_and_the_colours_asked_for(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
) -> None:
    order_id, ids = sent(asks, worker)
    found = detail(asks.api, order_id)
    by_id = {s["id"]: s for s in found["sources"]}
    file = by_id[ids["cube.stl"]]
    assert (file["kind"], file["check_status"], file["facts"], file["has_preview"]) == (
        "file",
        "ready",
        FACTS,
        True,
    )
    assert (file["staged"], file["import_stuck"], file["size"]) == (True, False, len(STL))
    maker = by_id[ids["makerworld:print"]]
    assert (maker["url"], maker["link_type"], maker["canonical"], maker["status"]) == (
        MAKERWORLD,
        "makerworld",
        "123",
        "open",
    )
    assert by_id[ids["printables:print"]]["status"] == "manual"
    purchase = by_id[ids["manual:purchase"]]
    assert (purchase["purpose"], purchase["status"], purchase["note"]) == (
        "purchase",
        "pending",
        "magnets",
    )
    [colours] = found["model_requests"]
    assert colours["shop_choice"] is True and colours["item_id"] == file["item_id"]


def test_the_back_office_reads_the_preview_with_fixed_headers(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
) -> None:
    order_id, ids = sent(asks, worker)
    r = asks.api.get(f"{ORDERS}/{order_id}/sources/{ids['cube.stl']}/preview")
    assert r.status_code == 200, r.text
    assert r.content == PNG
    assert preview_headers(r) == PREVIEW_HEADERS
    # The URL stays when "Check again" stores a new preview, so the browser asks again.
    assert r.headers["cache-control"] == "private, no-cache"
    none = asks.api.get(f"{ORDERS}/{order_id}/sources/{ids['makerworld:print']}/preview")
    assert none.status_code == 404
    # An order or source that is not one says so, as the download does (review of #6217).
    gone = "00000000-0000-4000-8000-000000000000"
    no_order = asks.api.get(f"{ORDERS}/{gone}/sources/{ids['cube.stl']}/preview")
    assert no_order.status_code == 404 and slug(no_order) == "order_not_found"
    no_source = asks.api.get(f"{ORDERS}/{order_id}/sources/{gone}/preview")
    assert no_source.status_code == 404 and slug(no_source) == "source_not_found"


def test_a_staged_file_downloads_through_a_presigned_get(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
) -> None:
    order_id, ids = sent(asks, worker)
    r = asks.api.get(f"{ORDERS}/{order_id}/sources/{ids['cube.stl']}/download")
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert ".frozen?" in url
    got = httpx.get(url)
    assert got.content == STL
    assert got.headers["content-disposition"] == 'attachment; filename="cube.stl"'
    link = asks.api.get(f"{ORDERS}/{order_id}/sources/{ids['makerworld:print']}/download")
    assert link.status_code == 409 and slug(link) == "source_not_staged"


def test_a_failed_check_runs_again(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
    checker: FakeChecker,  # noqa: F811
) -> None:
    order_id, ids = sent(asks, worker, outcome="failed")
    file_id = ids["cube.stl"]
    checker.started.clear()
    r = asks.api.post(f"{ORDERS}/{order_id}/sources/{file_id}/check")
    assert r.status_code == 200, r.text
    assert r.json()["check_status"] == "checking"
    assert checker.started == [file_id]
    # The worker reads it again, so its verdict lands.
    assert worker.read(file_id).status_code == 200
    events = [e.kind for e in _orders_events(asks.api, order_id)]
    assert "source.rechecked" in events


def test_a_check_that_did_not_fail_is_not_run_again(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
) -> None:
    order_id, ids = sent(asks, worker)
    r = asks.api.post(f"{ORDERS}/{order_id}/sources/{ids['cube.stl']}/check")
    assert r.status_code == 409 and slug(r) == "source_not_failed"


def test_the_shop_declines_a_link_and_a_file(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
    watcher: FakeWatcher,  # noqa: F811
) -> None:
    order_id, ids = sent(asks, worker)
    link = ids["printables:print"]
    r = asks.api.post(f"{ORDERS}/{order_id}/sources/{link}/decline", json={"reason": "Paid"})
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["status_reason"]) == ("declined", "Paid")
    again = asks.api.post(f"{ORDERS}/{order_id}/sources/{link}/decline", json={})
    assert again.status_code == 200 and again.json()["status_reason"] == "Paid"
    file = asks.api.post(f"{ORDERS}/{order_id}/sources/{ids['cube.stl']}/decline", json={})
    assert file.status_code == 200 and file.json()["status"] == "declined"
    assert (ids["upload"], "declined") in watcher.signals
    purchase = asks.api.post(
        f"{ORDERS}/{order_id}/sources/{ids['manual:purchase']}/decline", json={}
    )
    assert purchase.status_code == 409 and slug(purchase) == "source_not_print"
    # The customer never reads the shop's reason.
    assert "Paid" not in str(
        [s.get("customer_reason") for i in _view(asks, order_id)["items"] for s in i["sources"]]
    )


def test_a_stuck_import_is_retried(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
    watcher: FakeWatcher,  # noqa: F811
) -> None:
    order_id, ids = sent(asks, worker)
    file_id = ids["cube.stl"]
    early = asks.api.post(f"{ORDERS}/{order_id}/sources/{file_id}/retry-import")
    assert early.status_code == 409 and slug(early) == "import_not_stuck"
    with _state(asks.api).projection.pool.connection() as conn:
        conn.execute(
            "UPDATE order_item_sources SET status_reason = %s WHERE id = %s",
            (IMPORT_STUCK, file_id),
        )
    [source] = [s for s in detail(asks.api, order_id)["sources"] if s["id"] == file_id]
    assert source["import_stuck"] is True
    r = asks.api.post(f"{ORDERS}/{order_id}/sources/{file_id}/retry-import")
    assert r.status_code == 200, r.text
    assert (ids["upload"], "retry") in watcher.signals


def test_the_orders_list_filters_by_own_model_purchase_and_stuck(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,  # noqa: F811
) -> None:
    order_id, ids = sent(asks, worker)
    other = asks.api.post(ORDERS, json={"customer_id": None})
    other_id = other.json()["id"] if other.status_code == 201 else None

    def listed(**params: str) -> set[str]:
        r = asks.api.get(ORDERS, params=params)
        assert r.status_code == 200, r.text
        return {row["id"] for row in r.json()["items"]}

    assert order_id in listed(own_model="true")
    if other_id is not None:
        assert other_id not in listed(own_model="true")
    assert order_id in listed(purchase_pending="true")
    assert order_id not in listed(import_stuck="true")
    with _state(asks.api).projection.pool.connection() as conn:
        conn.execute(
            "UPDATE order_item_sources SET status_reason = %s WHERE id = %s",
            (IMPORT_STUCK, ids["cube.stl"]),
        )
    assert order_id in listed(import_stuck="true")
    asks.api.post(f"{ORDERS}/{order_id}/purchases/{ids['manual:purchase']}/decline", json={})
    assert order_id not in listed(purchase_pending="true")


def _orders_events(api: TestClient, order_id: str) -> list[Any]:
    from tests.orders.storefront.test_drafts import _orders

    return _orders(api).store.events(order_id)


def _view(asks: CrudCaller, order_id: str) -> dict[str, Any]:  # noqa: F811
    from tests.orders.storefront.test_customer_sources import link_token, order_post

    found: dict[str, Any] = order_post(
        asks, "view", {"token": link_token(asks.api, order_id)}
    ).json()
    return found
