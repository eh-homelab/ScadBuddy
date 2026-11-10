"""An order's customer messages (#5660, spec 2026-11-08-order-notify-customer-design.md §7):
the claim before a send, its outcome, a person's "Mark sent", and the stale rule."""

from __future__ import annotations

import threading
import time
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.storefront.visitors import VisitorStore, visitor_actor
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import AGENT, create_order
from tests.api.test_settings_runtime import _state
from tests.api.test_storefront_mail import CONNECTION
from tests.conftest import PgPool

BINDING: dict[str, Any] = {
    "source": "remote:mail",
    "tool": "send_email",
    "contact_kind": "email",
    "args": {"to": "to", "subject": "subject", "text": "body"},
    "fixed_args": {"from": "shop@example.com"},
}


def claim_body(template: str = "ready", **fields: Any) -> dict[str, Any]:
    return {
        "template": template,
        "source": "remote:mail",
        "tool": "send_email",
        "recipients": [{"kind": "email", "value": "ada@example.com"}],
        "subject": "Order ready",
        "text": "Hello Ada,\n\nOrder is ready.",
        **fields,
    }


def claim(client: TestClient, order_id: str, key: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(
        f"/api/v1/orders/{order_id}/messages",
        json=body or claim_body(),
        headers={**AGENT, "Idempotency-Key": key},
    )


@pytest.fixture
def order_id(client: TestClient) -> str:
    order: dict[str, Any] = create_order(client, create_customer(client)["id"])
    return str(order["id"])


def events(client: TestClient, order_id: str) -> list[str]:
    return [
        e["kind"]
        for e in client.get(f"/api/v1/orders/{order_id}/events").json()
        if e["kind"].startswith("message.")
    ]


def test_a_claim_is_written_once_per_key(client: TestClient, order_id: str) -> None:
    first = claim(client, order_id, "k1")
    again = claim(client, order_id, "k1")

    assert (first.status_code, again.status_code) == (201, 200), first.text
    message = first.json()
    assert again.json() == message
    assert (message["status"], message["template"], message["finished_at"]) == (
        "sending",
        "ready",
        None,
    )
    assert message["by"]["kind"] == "agent"
    assert message["by"]["session"] == "s1"
    assert [m["id"] for m in client.get(f"/api/v1/orders/{order_id}/messages").json()] == [
        message["id"]
    ]
    assert events(client, order_id) == ["message.sending"]


def test_a_key_of_another_order_is_refused(client: TestClient, order_id: str) -> None:
    other = create_order(client, create_customer(client)["id"])
    assert claim(client, order_id, "k1").status_code == 201
    reused = claim(client, other["id"], "k1")
    assert reused.status_code == 409
    assert reused.json()["type"].endswith("/idempotency_key_reused")


def test_a_key_another_order_takes_at_the_same_time_is_refused(
    client: TestClient, order_id: str, pg_conninfo: str
) -> None:
    other = create_order(client, create_customer(client)["id"])
    answers: list[Any] = []
    with psycopg.connect(pg_conninfo) as rival:
        # The other order's claim has written the key and not committed yet.
        rival.execute(
            "INSERT INTO order_messages (order_id, template, source, tool, recipients, text,"
            " status, idempotency_key, by) VALUES (%s, 'ready', 'remote:mail', 'send_email',"
            " '[]', 'x', 'sending', 'k1', '{\"kind\": \"browser\"}')",
            (other["id"],),
        )
        rival_pid = rival.info.backend_pid
        racing = threading.Thread(target=lambda: answers.append(claim(client, order_id, "k1")))
        racing.start()
        # Only a backend the rival's uncommitted key blocks counts: the claim's insert, waiting.
        with psycopg.connect(pg_conninfo, autocommit=True) as watch:
            for _ in range(200):
                waiting = watch.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                    (rival_pid,),
                ).fetchone()
                if waiting is not None and waiting[0] > 0:
                    break
                time.sleep(0.05)
            else:
                rival.rollback()
                racing.join(timeout=10)
                pytest.fail("the claim never waited on the rival's key, so the race was not run")
        rival.commit()
        racing.join(timeout=10)

    (reused,) = answers
    assert reused.status_code == 409, reused.text
    assert reused.json()["type"].endswith("/idempotency_key_reused")
    assert client.get(f"/api/v1/orders/{order_id}/messages").json() == []


def test_one_template_is_told_once_per_status_change(client: TestClient, order_id: str) -> None:
    first = claim(client, order_id, "k1").json()
    check = client.get(f"/api/v1/orders/{order_id}/messages/check", params={"template": "ready"})
    assert check.json()["already_notified"]["id"] == first["id"]

    twice = claim(client, order_id, "k2")
    assert twice.status_code == 409
    assert twice.json()["type"].endswith("/already_notified")
    # Another template is its own notice.
    assert claim(client, order_id, "k3", claim_body("quoted")).status_code == 201

    # A failed send does not count as told.
    sent = client.post(
        f"/api/v1/orders/{order_id}/messages/{first['id']}/outcome",
        json={"status": "failed", "reason": "plugin_error"},
        headers=AGENT,
    )
    assert sent.status_code == 200, sent.text
    assert claim(client, order_id, "k4").status_code == 201

    # A new status change of the order is a new episode.
    cancelled = client.post(f"/api/v1/orders/{order_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    after = client.get(f"/api/v1/orders/{order_id}/messages/check", params={"template": "ready"})
    assert after.json() == {"already_notified": None, "storefront_mail": False}
    assert claim(client, order_id, "k5").status_code == 201


def test_the_check_writes_nothing(client: TestClient, order_id: str) -> None:
    check = client.get(f"/api/v1/orders/{order_id}/messages/check", params={"template": "ready"})
    assert check.status_code == 200
    assert check.json() == {"already_notified": None, "storefront_mail": False}
    assert client.get(f"/api/v1/orders/{order_id}/messages").json() == []
    assert events(client, order_id) == []


def test_resend_names_a_finished_message_of_the_order(client: TestClient, order_id: str) -> None:
    first = claim(client, order_id, "k1").json()
    in_flight = claim(client, order_id, "k2", claim_body(resend_of=first["id"]))
    assert in_flight.status_code == 409
    assert in_flight.json()["type"].endswith("/message_in_flight")

    client.post(
        f"/api/v1/orders/{order_id}/messages/{first['id']}/outcome",
        json={"status": "sent", "provider_message_id": "m-1"},
        headers=AGENT,
    )
    unknown = claim(client, order_id, "k3", claim_body(resend_of="nope"))
    assert unknown.status_code == 422
    again = claim(client, order_id, "k4", claim_body(resend_of=first["id"]))
    assert again.status_code == 201, again.text
    assert again.json()["resend_of"] == first["id"]


def test_resend_keeps_the_template_of_the_message(client: TestClient, order_id: str) -> None:
    first = claim(client, order_id, "k1").json()
    client.post(
        f"/api/v1/orders/{order_id}/messages/{first['id']}/outcome",
        json={"status": "failed", "reason": "transport"},
        headers=AGENT,
    )
    other = claim(client, order_id, "k2", claim_body("shipped", resend_of=first["id"]))
    assert other.status_code == 422
    assert other.json()["errors"][0]["loc"] == ["body", "resend_of"]
    assert "'ready'" in other.json()["detail"]
    assert len(client.get(f"/api/v1/orders/{order_id}/messages").json()) == 1


def test_an_outcome_is_taken_until_the_message_is_final(client: TestClient, order_id: str) -> None:
    first = claim(client, order_id, "k1").json()
    path = f"/api/v1/orders/{order_id}/messages/{first['id']}/outcome"

    lost = client.post(path, json={"status": "unknown", "reason": "no_answer"}, headers=AGENT)
    assert lost.json()["status"] == "unknown"
    # The plugin's late answer is still recorded.
    late = client.post(path, json={"status": "sent", "provider_message_id": "m-9"}, headers=AGENT)
    assert late.status_code == 200, late.text
    assert (late.json()["status"], late.json()["provider_message_id"]) == ("sent", "m-9")
    assert late.json()["finished_at"] is not None

    final = client.post(path, json={"status": "failed", "reason": "transport"}, headers=AGENT)
    assert final.status_code == 409
    assert final.json()["type"].endswith("/message_final")
    assert events(client, order_id) == ["message.sending", "message.unknown", "message.sent"]


def test_an_outcome_needs_a_reason_unless_sent(client: TestClient, order_id: str) -> None:
    first = claim(client, order_id, "k1").json()
    path = f"/api/v1/orders/{order_id}/messages/{first['id']}/outcome"
    assert client.post(path, json={"status": "failed"}).status_code == 422
    assert client.post(path, json={"status": "sent", "reason": "transport"}).status_code == 422
    assert (
        client.post(path, json={"status": "failed", "reason": "the plugin said"}).status_code == 422
    )
    missing = f"/api/v1/orders/{order_id}/messages/nope/outcome"
    assert client.post(missing, json={"status": "sent"}).status_code == 404


def test_a_stale_send_reads_unknown_and_is_never_sent_again(
    client: TestClient, order_id: str, pg_conninfo: str
) -> None:
    first = claim(client, order_id, "k1").json()
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE order_messages SET created_at = created_at - interval '16 minutes'"
            " WHERE id = %s",
            (first["id"],),
        )

    (listed,) = client.get(f"/api/v1/orders/{order_id}/messages").json()
    assert listed["status"] == "unknown"
    # A retry of its key gets it back, and nothing new is written.
    retried = claim(client, order_id, "k1")
    assert (retried.status_code, retried.json()["status"]) == (200, "unknown")
    # A new call is refused: the customer may have been told.
    assert claim(client, order_id, "k2").json()["type"].endswith("/already_notified")
    assert len(client.get(f"/api/v1/orders/{order_id}/messages").json()) == 1
    # A send that names it is not refused as in flight: it reads unknown.
    resent = claim(client, order_id, "k3", claim_body(resend_of=first["id"]))
    assert resent.status_code == 201, resent.text

    marked = client.post(f"/api/v1/orders/{order_id}/messages/{first['id']}/mark-sent")
    assert marked.status_code == 200, marked.text
    assert marked.json()["status"] == "sent"
    assert events(client, order_id) == [
        "message.sending",
        "message.sending",
        "message.marked_sent",
    ]
    refused = client.post(f"/api/v1/orders/{order_id}/messages/{first['id']}/mark-sent")
    assert refused.json()["type"].endswith("/message_not_unknown")


def test_a_fresh_send_is_not_marked_sent(client: TestClient, order_id: str) -> None:
    first = claim(client, order_id, "k1").json()
    refused = client.post(f"/api/v1/orders/{order_id}/messages/{first['id']}/mark-sent")
    assert refused.status_code == 409
    assert refused.json()["type"].endswith("/message_not_unknown")


def test_a_cart_and_a_missing_order_are_refused(client: TestClient, pg_pool: PgPool) -> None:
    orders = _state(client).components.get(ORDERS)
    visitor = VisitorStore(pg_pool).ensure("v" * 43)
    draft = orders.lifecycle.ensure_draft(visitor.id, visitor_actor(visitor), currency="USD")

    cart = claim(client, draft.id, "k1")
    assert cart.status_code == 409
    assert cart.json()["type"].endswith("/order_not_submitted")
    assert claim(client, "nope", "k2").status_code == 404
    assert client.get("/api/v1/orders/nope/messages").status_code == 404


def test_a_claim_needs_its_key_and_a_recipient(client: TestClient, order_id: str) -> None:
    path = f"/api/v1/orders/{order_id}/messages"
    assert client.post(path, json=claim_body()).status_code == 422
    no_one = client.post(path, json=claim_body(recipients=[]), headers={"Idempotency-Key": "k"})
    assert no_one.status_code == 422
    plugin = client.post(
        path, json=claim_body(source="stdio:mail"), headers={"Idempotency-Key": "k"}
    )
    assert plugin.status_code == 422


def test_the_binding_is_a_setting(client: TestClient) -> None:
    assert client.get("/api/v1/settings").json()["orders_customer_messaging"] is None
    saved = client.put("/api/v1/settings", json={"orders_customer_messaging": BINDING})
    assert saved.status_code == 200, saved.text
    assert client.get("/api/v1/settings").json()["orders_customer_messaging"] == {
        **BINDING,
        "args": {**BINDING["args"], "to_shape": "list", "idempotency_key": None},
    }
    cleared = client.put("/api/v1/settings", json={"orders_customer_messaging": None})
    assert cleared.status_code == 200
    assert client.get("/api/v1/settings").json()["orders_customer_messaging"] is None


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"fixed_args": {"smtp_password": "x"}}, "secret"),
        ({"fixed_args": {"apiKey": "x"}}, "secret"),
        ({"fixed_args": {"auth": "x"}}, "secret"),
        ({"fixed_args": {"x_token": "x"}}, "secret"),
        ({"fixed_args": {"apitoken": "x"}}, "secret"),
        ({"fixed_args": {"smtppass": "x"}}, "secret"),
        ({"fixed_args": {"privatekey": "x"}}, "secret"),
        ({"fixed_args": {"body": "x"}}, "mapped"),
        ({"args": {"to": "to", "text": "to"}}, "two fields"),
        ({"source": "stdio:mail"}, "source"),
    ],
)
def test_a_bad_binding_is_refused(client: TestClient, change: dict[str, Any], why: str) -> None:
    refused = client.put(
        "/api/v1/settings", json={"orders_customer_messaging": {**BINDING, **change}}
    )
    assert refused.status_code == 422, why


@pytest.mark.parametrize("key", ["author", "keyword", "monkey", "passenger", "reply_to"])
def test_a_word_that_holds_a_secret_word_is_no_secret(client: TestClient, key: str) -> None:
    binding = {**BINDING, "fixed_args": {key: "x"}}
    saved = client.put("/api/v1/settings", json={"orders_customer_messaging": binding})
    assert saved.status_code == 200, saved.text


# -- the storefront overlap (spec §8, PR 5) ------------------------------------------------


class _Covers:
    """`storefront_mail_covers` faked: whether the storefront mails the order, and each ask."""

    def __init__(self, covers: bool) -> None:
        self.covers = covers
        self.asked: list[tuple[str, str, bool]] = []

    def __call__(self, conn: Any, order_id: str, event: str, *, ready: bool) -> bool:
        self.asked.append((order_id, event, ready))
        return self.covers


def _covers(monkeypatch: pytest.MonkeyPatch, covers: bool) -> _Covers:
    fake = _Covers(covers)
    monkeypatch.setattr("scadbuddy.orders.messages.storefront_mail_covers", fake)
    return fake


@pytest.mark.parametrize("covers", [True, False])
@pytest.mark.parametrize(
    ("template", "event"),
    [("quoted", "price_ready"), ("accepted", None), ("ready", None), ("shipped", "shipped")],
)
def test_a_template_the_storefront_mails_is_skipped(
    client: TestClient,
    order_id: str,
    monkeypatch: pytest.MonkeyPatch,
    template: str,
    event: str | None,
    covers: bool,
) -> None:
    """Only `quoted` and `shipped` have storefront mail; each is skipped only when the
    storefront mails this order. A skip is a `skipped` row with its audit row."""
    fake = _covers(monkeypatch, covers)
    check = client.get(
        f"/api/v1/orders/{order_id}/messages/check", params={"template": template}
    ).json()
    claimed = claim(client, order_id, "k1", claim_body(template))
    assert claimed.status_code == 201, claimed.text
    skipped = covers and event is not None
    assert check["storefront_mail"] is skipped
    message = claimed.json()
    if skipped:
        assert (message["status"], message["reason"]) == ("skipped", "storefront_mail")
        assert message["finished_at"] is not None
        assert events(client, order_id) == ["message.skipped"]
    else:
        assert (message["status"], message["reason"]) == ("sending", None)
        assert events(client, order_id) == ["message.sending"]
    # A template with no storefront event never asks the seam.
    assert [asked[1] for asked in fake.asked] == ([event, event] if event else [])


def test_a_skip_does_not_count_as_told(
    client: TestClient, order_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _covers(monkeypatch, True)
    assert claim(client, order_id, "k1", claim_body("shipped")).json()["status"] == "skipped"
    # The same key answers the skipped row; nothing new is written.
    again = claim(client, order_id, "k1", claim_body("shipped"))
    assert (again.status_code, again.json()["status"]) == (200, "skipped")
    fake.covers = False
    sent = claim(client, order_id, "k2", claim_body("shipped"))
    assert (sent.status_code, sent.json()["status"]) == (201, "sending")


def test_also_when_storefront_mails_sends_anyway(
    client: TestClient, order_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _covers(monkeypatch, True)
    body = claim_body("quoted", also_when_storefront_mails=True)
    claimed = claim(client, order_id, "k1", body)
    assert (claimed.status_code, claimed.json()["status"]) == (201, "sending")
    assert events(client, order_id) == ["message.sending"]


def test_the_skip_asks_whether_the_running_mail_config_is_ready(
    client: TestClient, order_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _covers(monkeypatch, False)
    claim(client, order_id, "k1", claim_body("quoted"))
    only_host = {"storefront_mail_host": "smtp.example.com"}
    assert client.put("/api/v1/settings", json=only_host).status_code == 200
    client.get(f"/api/v1/orders/{order_id}/messages/check", params={"template": "shipped"})
    assert client.put("/api/v1/settings", json=CONNECTION).status_code == 200
    client.get(f"/api/v1/orders/{order_id}/messages/check", params={"template": "shipped"})
    assert [(asked[0], asked[2]) for asked in fake.asked] == [
        (order_id, False),
        (order_id, False),
        (order_id, True),
    ]
