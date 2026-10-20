"""``WS /api/v1/ws`` carries ``orders`` and ``order:<id>`` (#4249, spec 2026-10-18 §4):
every write the lifecycle makes is news for both."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.events import EventBus, OrderChanged, OrderItemChanged
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.models import Actor
from tests.api.test_realtime import WS, bus, subscribe

__all__ = ["bus"]


def test_order_topics_are_followed(client: TestClient, bus: EventBus) -> None:
    order_id = "0e5a3c1e-1111-4222-8333-944455556666"
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "orders", f"order:{order_id}")
        bus.publish(OrderChanged(order_id=order_id, status="accepted"))
        frame = ws.receive_json()
        assert frame["kind"] == "order.changed"
        assert frame["topics"] == ["orders", f"order:{order_id}"]
        assert frame["data"] == {"order_id": order_id, "status": "accepted"}
        bus.publish(OrderItemChanged(order_id=order_id, item_id="i1", status="failed", attempt=2))
        frame = ws.receive_json()
        assert frame["kind"] == "order_item.changed"
        assert frame["topics"] == ["orders", f"order:{order_id}"]
        assert frame["data"] == {
            "order_id": order_id,
            "item_id": "i1",
            "status": "failed",
            "attempt": 2,
        }


def test_an_order_change_is_news_for_orders_and_its_order(app: FastAPI, client: TestClient) -> None:
    """A socket following `orders` hears what the lifecycle writes through the
    component, which is what every route of Tasks 6 and 7 goes through."""
    state: AppState = getattr(app.state, STATE_ATTR)
    orders = state.components.get(ORDERS)
    customer = orders.store.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "orders")
        order = orders.lifecycle.create_order(customer.id, Actor(kind="browser"), currency="USD")
        frame = ws.receive_json()
        assert (frame["kind"], frame["data"]["order_id"], frame["data"]["status"]) == (
            "order.changed",
            order.id,
            "draft",
        )
        # The frame names the topics this socket follows, not every one the event has.
        assert frame["topics"] == ["orders"]
        orders.lifecycle.hold(order.id, Actor(kind="browser"))
        assert ws.receive_json()["data"] == {"order_id": order.id, "status": "draft"}
