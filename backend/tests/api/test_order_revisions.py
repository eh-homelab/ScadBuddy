"""Design revisions through the API (#5280): request items, the revision routes, the order
read's `revisions`, and the approval mode."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.api.test_customers import create as create_customer
from tests.api.test_order_attachments import png
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import add_item, problem
from tests.api.test_send import make_output
from tests.api.test_versions import SLUG, upload
from tests.support.operations import press

REQUEST = {"kind": "request", "title": "A dragon", "quantity": 1, "notes": "green"}


def a_request(client: TestClient) -> tuple[dict[str, Any], dict[str, Any]]:
    order = create_order(client, create_customer(client)["id"])
    return order, add_item(client, order["id"], **REQUEST)


def a_reference(client: TestClient) -> dict[str, Any]:
    upload(client)
    commit = client.get(f"/api/v1/models/{SLUG}").json()["version"]
    return {"slug": SLUG, "commit": commit, "inputs": {"params": {"width": 12}}}


def test_a_request_item_takes_its_design_as_revision_one(client: TestClient) -> None:
    order, item = a_request(client)
    assert item["kind"] == "request" and item["reference"] is None
    assert item["approval"] == "design_first"
    rev = client.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions",
        json={"reference": a_reference(client), "note": "first go"},
    )
    assert rev.status_code == 201, rev.text
    assert rev.json()["number"] == 1 and rev.json()["note"] == "first go"


def test_the_order_read_lists_each_items_revisions(client: TestClient) -> None:
    order, item = a_request(client)
    base = f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions"
    assert client.post(base, json={"reference": a_reference(client)}).status_code == 201
    assert client.post(base, json={"note": "again"}).status_code == 201
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert [r["number"] for r in detail["items"][0]["revisions"]] == [1, 2]


def test_a_sent_revisions_file_keeps_its_role(client: TestClient) -> None:
    order, item = a_request(client)
    base = f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions"
    rev = client.post(base, json={"reference": a_reference(client)}).json()
    a_render(client, base, rev["id"])
    [render] = client.get(f"/api/v1/orders/{order['id']}").json()["attachments"]
    patch = f"/api/v1/orders/{order['id']}/attachments/{render['id']}"
    assert client.patch(patch, json={"role": "test_print"}).status_code == 422
    assert client.post(f"{base}/{rev['id']}/send").status_code == 200
    problem(client.patch(patch, json={"caption": "late"}), "revision_sent")


def test_send_and_answer_are_refused_in_order(client: TestClient) -> None:
    order, item = a_request(client)
    base = f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions"
    rev = client.post(base, json={"reference": a_reference(client)}).json()
    answer = client.post(f"{base}/{rev['id']}/answer", json={"answer": "accepted"})
    problem(answer, "revision_not_sent")
    problem(client.post(f"{base}/{rev['id']}/send"), "revision_empty")


def test_a_request_item_with_no_design_is_not_quoted(client: TestClient) -> None:
    order, item = a_request(client)
    answer = client.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/quotes", json={"unit_price_minor": 500}
    )
    problem(answer, "item_has_no_design")


def test_approval_is_patched(client: TestClient) -> None:
    order, item = a_request(client)
    url = f"/api/v1/orders/{order['id']}/items/{item['id']}"
    patched = client.patch(url, json={"approval": "together"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["approval"] == "together"
    assert client.patch(url, json={"approval": "design_first"}).json()["approval"] == "design_first"


def test_a_request_names_no_source(client: TestClient) -> None:
    order, _ = a_request(client)
    url = f"/api/v1/orders/{order['id']}/items"
    named = client.post(url, json={"kind": "request", "title": "x", "quantity": 1, "slug": "demo"})
    assert named.status_code == 422
    untitled = client.post(url, json={"kind": "request", "title": "  ", "quantity": 1})
    assert untitled.status_code == 422


def a_render(client: TestClient, base: str, revision_id: str) -> None:
    uploaded = client.post(
        f"{base}/{revision_id}/attachments",
        files={"file": ("r.png", png(), "image/png")},
        data={"role": "render"},
        headers=press(),
    )
    assert uploaded.status_code == 200, uploaded.text


def test_a_revision_with_a_file_is_sent_and_answered(client: TestClient) -> None:
    order, item = a_request(client)
    base = f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions"
    rev = client.post(base, json={"reference": a_reference(client)}).json()
    a_render(client, base, rev["id"])
    sent = client.post(f"{base}/{rev['id']}/send")
    assert sent.status_code == 200, sent.text
    assert sent.json()["sent_at"] is not None
    needs_note = client.post(f"{base}/{rev['id']}/answer", json={"answer": "changes_requested"})
    assert needs_note.status_code == 422
    answered = client.post(f"{base}/{rev['id']}/answer", json={"answer": "accepted"})
    assert answered.status_code == 200, answered.text
    assert answered.json()["answer"] == "accepted"


def test_the_answer_body_names_no_unknown_field(client: TestClient) -> None:
    order, item = a_request(client)
    base = f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions"
    rev = client.post(base, json={"reference": a_reference(client)}).json()
    odd = client.post(f"{base}/{rev['id']}/answer", json={"answer": "accepted", "by": "me"})
    assert odd.status_code == 422


def test_accepting_a_together_revision_accepts_its_quote(client: TestClient) -> None:
    order, item = a_request(client)
    url = f"/api/v1/orders/{order['id']}/items/{item['id']}"
    assert client.patch(url, json={"approval": "together"}).status_code == 200
    base = f"{url}/revisions"
    rev = client.post(base, json={"reference": a_reference(client)}).json()
    quoted = client.post(f"{url}/quotes", json={"unit_price_minor": 500})
    assert quoted.status_code == 201, quoted.text
    a_render(client, base, rev["id"])
    assert client.post(f"{base}/{rev['id']}/send").status_code == 200
    answered = client.post(f"{base}/{rev['id']}/answer", json={"answer": "accepted"})
    assert answered.status_code == 200, answered.text
    items = client.get(f"/api/v1/orders/{order['id']}").json()["items"]
    assert items[0]["status"] == "approved"
    assert items[0]["quotes"][0]["accepted"] is True


def test_another_items_revision_is_not_found(client: TestClient) -> None:
    order, item = a_request(client)
    other = add_item(client, order["id"], **REQUEST)
    rev = client.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions",
        json={"reference": a_reference(client)},
    ).json()
    wrong = f"/api/v1/orders/{order['id']}/items/{other['id']}/revisions/{rev['id']}"
    problem(client.post(f"{wrong}/send"), "revision_not_found", 404)
    problem(client.post(f"{wrong}/answer", json={"answer": "accepted"}), "revision_not_found", 404)


def test_a_custom_item_takes_no_revision(client: TestClient) -> None:
    order = create_order(client, create_customer(client)["id"])
    custom = add_item(
        client, order["id"], kind="custom", title="Fee", quantity=1, unit_price_minor=100
    )
    added = client.post(f"/api/v1/orders/{order['id']}/items/{custom['id']}/revisions", json={})
    problem(added, "item_custom")


def test_a_revision_is_made_from_an_output(client: TestClient) -> None:
    order, item = a_request(client)
    upload(client)
    output_id = make_output(client, SLUG)
    rev = client.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions", json={"output_id": output_id}
    )
    assert rev.status_code == 201, rev.text
    assert rev.json()["reference"]["slug"] == SLUG
    detail = client.get(f"/api/v1/orders/{order['id']}").json()["items"][0]
    assert detail["output_id"] == output_id
