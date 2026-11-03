"""Printing an order item end to end (#4719, spec 2026-10-18 §5.4):
``POST /orders/{id}/items/{item}/print`` answers with the ``order_item_print``
operation, whose run starts the item's output's print run as ``POST .../run`` starts one
(Bambuddy's slice and queue over the ``respx`` recordings ``tests/api/test_print.py``
uses), records the attempt and queues the item; the item then follows the print's
recorded outcome."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import Response
from fastapi.testclient import TestClient

from scadbuddy.activity.component import ACTIVITY
from scadbuddy.api.printing import accept_run
from scadbuddy.bambuddy.runs import PrintRun
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.events import PrintOutcome
from scadbuddy.operations.component import OPERATIONS
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.lifecycle import FULFILMENT_HOLDER
from scadbuddy.orders.operations import ORDER_ITEM_PRINT, existing_run, print_body
from scadbuddy.orders.printing import print_request_id
from scadbuddy.workflows.print_models import SourceSpec
from tests.api.conftest import follow_operation
from tests.api.test_customers import create as create_customer
from tests.api.test_order_quote import COPY, BambuddySlices, sliced_plate, two_colours
from tests.api.test_order_quote import Shop as QuoteShop
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import add_item, problem, quote_and_accept
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import body, follow_run, run_routes
from tests.api.test_send import API, configure, make_output, upload_route
from tests.api.test_settings_runtime import _state
from tests.api.test_versions import SLUG, upload
from tests.orders.test_lifecycle import BY
from tests.support.operations import press

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_git]


class Projects:
    """Bambuddy's project create (§8.1): ids from 700, each with its folder."""

    def __init__(self) -> None:
        self.made: list[dict[str, Any]] = []
        #: Projects deleted in Bambuddy (#5263).
        self.deleted: set[int] = set()
        #: Creates that fail with a 500 before one succeeds.
        self.failing = 0
        respx.post(f"{API}/projects/").mock(side_effect=self.create)
        respx.get(url__regex=rf"{API}/projects/\d+$").mock(side_effect=self.read)
        respx.get(url__regex=rf"{API}/library/folders/by-project/\d+").mock(
            side_effect=self.folders
        )

    def create(self, request: httpx.Request) -> httpx.Response:
        if self.failing:
            self.failing -= 1
            return httpx.Response(500, json={"detail": "down"})
        body = json.loads(request.content)
        project = {"id": 700 + len(self.made), "status": "active", **body}
        self.made.append(project)
        return httpx.Response(200, json=project)

    def read(self, request: httpx.Request) -> httpx.Response:
        project_id = int(request.url.path.rsplit("/", 1)[1])
        found = next((p for p in self.made if p["id"] == project_id), None)
        if found is None or project_id in self.deleted:
            return httpx.Response(404, json={"detail": "Project not found"})
        return httpx.Response(200, json=found)

    def folders(self, request: httpx.Request) -> httpx.Response:
        project_id = int(request.url.path.rsplit("/", 1)[1])
        folder = {"id": project_id + 100, "name": "p", "project_id": project_id}
        return httpx.Response(200, json=[folder])


@pytest.fixture
def bambuddy(client: TestClient, fake_openscad: str) -> Iterator[TestClient]:
    with respx.mock as router:
        router.route(host="testserver").pass_through()
        two_colours(fake_openscad)
        configure(client)
        upload_route(COPY)
        run_routes()
        Projects()
        yield client


class Item:
    """One item of an order from an output, quoted by hand and accepted."""

    def __init__(self, client: TestClient, *, quantity: int = 2, accept: bool = True) -> None:
        self.client = client
        upload(client)
        customer = create_customer(client)
        self.order = create_order(client, customer["id"])
        self.output_id = make_output(client, SLUG)
        self.item = add_item(client, self.order["id"], output_id=self.output_id, quantity=quantity)
        quote_and_accept(client, self.order["id"], self.item["id"])
        if accept:
            accepted = client.post(f"/api/v1/orders/{self.order['id']}/accept")
            assert accepted.status_code == 200, accepted.text
        self.path = f"/api/v1/orders/{self.order['id']}/items/{self.item['id']}"

    def post(self, key: str | None = None, **extra: Any) -> Any:
        headers = {"Idempotency-Key": key} if key else press()
        return self.client.post(f"{self.path}/print", json={**body(), **extra}, headers=headers)

    def get(self) -> dict[str, Any]:
        detail = self.client.get(f"/api/v1/orders/{self.order['id']}").json()
        found: dict[str, Any] = next(i for i in detail["items"] if i["id"] == self.item["id"])
        return found

    def order_status(self) -> str:
        detail = self.client.get(f"/api/v1/orders/{self.order['id']}").json()
        status: str = detail["order"]["status"]
        return status


def answered(client: TestClient, started: httpx.Response) -> dict[str, Any]:
    """The attempt a print answered with, followed past a 202."""
    assert started.status_code in (200, 202), started.text
    if started.status_code == 200:
        answer: dict[str, Any] = started.json()
        return answer
    op = follow_operation(client, started.json(), timeout=120)
    assert op["status"] == "succeeded", op
    result: dict[str, Any] = op["result"]
    return result


def printed(item: Item, key: str | None = None) -> dict[str, Any]:
    return answered(item.client, item.post(key))


def test_an_accepted_order_and_its_item_get_projects_the_print_files_under(
    client: TestClient, fake_openscad: str
) -> None:
    """§8.1: accepting makes the order's Bambuddy project and the item's nested under it;
    the item's print is queued in the item's project."""
    with respx.mock as router:
        router.route(host="testserver").pass_through()
        two_colours(fake_openscad)
        configure(client)
        upload_route(COPY)
        run_routes()
        slice_routes()
        queued = queue_route()
        projects = Projects()
        item = Item(client)
        number = item.order["number"]
        assert [(p["name"], p.get("parent_id")) for p in projects.made] == [
            (number, None),
            (f"{number} · 1 {item.item['title']}".strip(), 700),
        ]
        detail = client.get(f"/api/v1/orders/{item.order['id']}").json()
        assert [(p["subject"], p["project_id"], p["item_id"]) for p in detail["projects"]] == [
            (f"order:{item.order['id']}", 700, None),
            (f"order_item:{item.item['id']}", 701, item.item["id"]),
        ]
        attempt = printed(item, key="print-projects")
        run = follow_run(client, attempt["print_run_id"])
        assert run["status"] == "succeeded", run
        sent = json.loads(queued.calls.last.request.content)
        assert sent["project_id"] == 701
        # Ensured again by the print, nothing new was made.
        assert len(projects.made) == 2


def test_an_item_prints_from_its_quote_and_follows_the_print(bambuddy: TestClient) -> None:
    slice_routes()
    queued = queue_route()
    item = Item(bambuddy)
    attempt = printed(item, key="print-1")
    assert (attempt["attempt"], attempt["outcome"], attempt["quantity"]) == (1, "unknown", 0)
    assert attempt["copies"] == 2 and attempt["print_run_id"]
    # A manual quote names no sliced file.
    assert attempt["sliced_sha256"] is None
    assert item.get()["status"] == "queued"
    assert item.order_status() == "in_production"

    run = follow_run(bambuddy, attempt["print_run_id"])
    assert run["status"] == "succeeded", run
    assert run["subject"] == f"output:{item.output_id}"
    sent = json.loads(queued.calls.last.request.content)
    # §5.4: the quantity still to print.
    assert sent["quantity"] == 2

    # The same press again repeats its run and records nothing twice.
    assert printed(item, key="print-1")["id"] == attempt["id"]
    assert len(item.get()["fulfilments"]) == 1

    # The print's follow records its outcome (#2665): the item follows it.
    state = _state(bambuddy)
    with state.projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_outcomes (run_id, subject, slug, outcome)"
            " VALUES (%s, %s, %s, 'finished')",
            (run["id"], item.output_id, SLUG),
        )
    state.events.publish(
        PrintOutcome(output_id=item.output_id, slug=SLUG, run_id=run["id"], outcome="finished")
    )
    deadline = time.monotonic() + 30
    while item.get()["status"] != "printed" and time.monotonic() < deadline:
        time.sleep(0.05)
    current = item.get()
    assert current["status"] == "printed"
    [done] = current["fulfilments"]
    assert (done["outcome"], done["quantity"]) == ("succeeded", 2)
    accepted = bambuddy.post(f"{item.path}/fulfilments/{done['id']}/accept")
    assert accepted.status_code == 200, accepted.text
    assert item.get()["status"] == "fulfilled"
    assert item.order_status() == "ready"


def test_a_print_is_refused_before_anything_starts(bambuddy: TestClient) -> None:
    item = Item(bambuddy, accept=False)
    problem(item.post(), "order_not_accepted")
    # Nothing was started or recorded.
    assert item.get()["fulfilments"] == []
    assert item.get()["status"] == "approved"


def test_a_slice_quote_prints_and_holds_its_sliced_file(bambuddy: TestClient) -> None:
    """The attempt names and holds the accepted slice quote's file (§6.1); a file that is
    no longer the one the quote priced wants a new quote."""
    shop = QuoteShop(bambuddy)
    BambuddySlices({1: sliced_plate(1, 3600, [12.5, 3.0])})
    op = shop.quote()
    assert op["status"] == "succeeded", op
    quote = op["result"]
    assert bambuddy.post(f"{shop.path}/quotes/{quote['id']}/accept").status_code == 200
    assert bambuddy.post(f"/api/v1/orders/{shop.order['id']}/accept").status_code == 200

    sliced = _state(bambuddy).store.blobs.dir_for(quote["sliced_blob_key"]) / "sliced.3mf"
    kept = sliced.read_bytes()
    sliced.write_bytes(b"not the file it priced")
    problem(bambuddy.post(f"{shop.path}/print", json=body(), headers=press()), "requote_needed")
    sliced.write_bytes(kept)

    # #4251 §5.4 "Spools": spools 9 and 5 are not the red PLA and green PETG it was
    # quoted with, so the print is refused before anything starts.
    refused = problem(
        bambuddy.post(f"{shop.path}/print", json=body(), headers=press()), "spool_not_filament"
    )
    assert refused["slots"] == [0, 1]
    # The book's records made the recorded spools' filaments (a bookkeeping fix, which
    # moves no quote): now they are the slots'.
    for record, (material, subtype, colour) in (
        (shop.pla, ("PETG", "Basic", "#688197")),
        (shop.petg, ("PLA", "Tri Color", "#0047BB")),
    ):
        fixed = bambuddy.patch(
            f"/api/v1/orders/price-book/filaments/{record}",
            json={"material": material, "subtype": subtype, "brand": "Bambu Lab", "colour": colour},
        )
        assert fixed.status_code == 200, fixed.text

    queue_route()
    attempt = answered(bambuddy, bambuddy.post(f"{shop.path}/print", json=body(), headers=press()))
    assert attempt["sliced_sha256"] == quote["sliced_sha256"]
    assert attempt["sliced_blob_key"] == quote["sliced_blob_key"]
    refs = _state(bambuddy).refs
    with refs.pool.connection() as conn:
        held = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            (FULFILMENT_HOLDER, attempt["id"]),
        ).fetchall()
    assert [row["key"] for row in held] == [quote["sliced_blob_key"]]


def _lookup(client: TestClient, item: Item, request: dict[str, Any], copies: int) -> Any:
    """``existing_run`` as a refused retry asks it, on the app's own loop and client."""
    state = _state(client)
    # The item's Bambuddy project (§8.1) is part of the run's request, so of its key.
    project = next(
        p["project_id"]
        for p in client.get(f"/api/v1/orders/{item.order['id']}").json()["projects"]
        if p["item_id"] == item.item["id"]
    )

    async def run() -> PrintRun | None:
        return await existing_run(
            state.print_runs,
            PrintSubject.output(item.output_id),
            print_body(request, copies, project),
        )

    assert client.portal is not None
    return client.portal.call(run)


def _operation_request(client: TestClient, item: Item, key: str) -> dict[str, Any]:
    with _state(client).projection.pool.connection() as conn:
        row = conn.execute(
            "SELECT request FROM operations WHERE kind = 'order_item_print' AND subject = %s"
            " ORDER BY created_at DESC LIMIT 1",
            (f"order_item:{item.item['id']}",),
        ).fetchone()
    assert row is not None, key
    request: dict[str, Any] = row["request"]
    return request


def test_a_retry_finds_the_run_its_request_made(bambuddy: TestClient) -> None:
    """The lookup a refused retry makes before it releases its claim (#5149 review): the
    run the print path recorded for the request, by its key, or none."""
    slice_routes()
    queue_route()
    item = Item(bambuddy)
    attempt = printed(item, key="print-1")
    run = follow_run(bambuddy, attempt["print_run_id"])
    assert run["status"] == "succeeded", run
    request = _operation_request(bambuddy, item, "print-1")
    found = _lookup(bambuddy, item, request, attempt["copies"])
    assert found is not None and found.id == attempt["print_run_id"]
    # Another request (a key never sent) made no run, so a claim of it is released.
    other = {**request, "request_id": request["request_id"][:-4] + "beef"}
    assert other["request_id"] != request["request_id"]
    assert _lookup(bambuddy, item, other, attempt["copies"]) is None


def test_a_deleted_project_is_made_again_and_a_failed_make_is_tried_again(
    client: TestClient, fake_openscad: str
) -> None:
    """#5263: the item's project was deleted in Bambuddy, and its first make again
    fails (a 500): the operation tries the print again, which makes it and files the
    print under the new one."""
    with respx.mock as router:
        router.route(host="testserver").pass_through()
        two_colours(fake_openscad)
        configure(client)
        upload_route(COPY)
        run_routes()
        slice_routes()
        queued = queue_route()
        projects = Projects()
        item = Item(client)
        projects.deleted.add(701)
        projects.failing = 1
        attempt = printed(item, key="print-remade")
        assert follow_run(client, attempt["print_run_id"])["status"] == "succeeded"
        assert projects.failing == 0
        assert json.loads(queued.calls.last.request.content)["project_id"] == 702
        detail = client.get(f"/api/v1/orders/{item.order['id']}").json()
        assert [p["project_id"] for p in detail["projects"] if p["item_id"]] == [702]


def test_a_claim_from_before_finds_the_run_its_start_made(bambuddy: TestClient) -> None:
    """#5263: a claim from before claims recorded their project, whose run was keyed
    with no project (before #5231), is retried after the deploy: the retry attaches the
    run its start made, never a second print under the item's project's key."""
    slice_routes()
    queue_route()
    item = Item(bambuddy)
    state = _state(bambuddy)
    kind = state.components.get(OPERATIONS).kinds[ORDER_ITEM_PRINT]
    request = {
        "order_id": item.order["id"],
        "item_id": item.item["id"],
        "request": body(),
        "request_id": print_request_id(item.item["id"], "legacy"),
        "by": BY.model_dump(mode="json"),
    }
    assert bambuddy.portal is not None
    checked = bambuddy.portal.call(kind.check, request)
    claim = state.components.get(ORDERS).lifecycle.claim_print(
        item.order["id"],
        item.item["id"],
        BY,
        request_id=request["request_id"],
        sliced_sha256=checked["sliced_sha256"],
        sliced_blob_key=checked["sliced_blob_key"],
    )
    copies = claim.copies
    assert copies is not None
    with state.projection.pool.connection() as conn:
        conn.execute(
            "UPDATE order_item_fulfilments SET print_project_keyed = false WHERE id = %s",
            (claim.id,),
        )

    async def start_before() -> PrintRun:
        run = await accept_run(
            state.print_runs,
            Response(),
            subject=PrintSubject.output(item.output_id),
            slug=checked["slug"],
            request=print_body(request, copies),
            source=SourceSpec(kind="output", output_id=item.output_id),
            activity=state.components.get(ACTIVITY),
        )
        assert isinstance(run, PrintRun)
        return run

    before = bambuddy.portal.call(start_before)
    attempt = bambuddy.portal.call(kind.run, request, checked)

    assert attempt["id"] == claim.id
    assert attempt["print_run_id"] == before.id
    with state.projection.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM print_runs WHERE output_id = %s",
            (item.output_id,),
        ).fetchone()
    assert row is not None and row["n"] == 1


JPEG = b"\xff\xd8\xff\xe0" + b"j" * 1024


def test_an_attempts_archive_offers_its_finish_photo_as_a_copy(bambuddy: TestClient) -> None:
    """§8.2: the attempt's run's archive (``print_links`` by its queue item) is read from
    Bambuddy and its ``finish_`` photo copied into the item's files, once."""
    slice_routes()
    queue_route()
    item = Item(bambuddy)
    attempt = printed(item, key="print-archive")
    assert follow_run(bambuddy, attempt["print_run_id"])["status"] == "succeeded"
    with _state(bambuddy).projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_links (subject, archive_id, matched_by, queue_item_id)"
            " VALUES (%s, 81, 'queue_item', 51) ON CONFLICT DO NOTHING",
            (f"output:{item.output_id}",),
        )
    respx.get(f"{API}/archives/81").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 81,
                "status": "completed",
                "photos": ["a1b2c3d4.jpg", "finish_20261101_ab12.jpg"],
                "timelapse_path": None,
                "actual_time_seconds": 3600,
                "filament_used_grams": 9.5,
            },
        )
    )
    photo = respx.get(f"{API}/archives/81/photos/finish_20261101_ab12.jpg").mock(
        return_value=httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
    )
    base = f"{item.path}/fulfilments/{attempt['id']}/archives"
    read = bambuddy.get(base)
    assert read.status_code == 200, read.text
    offers = read.json()
    assert offers["linked"] is True
    assert [(a["archive_id"], a["finish_photos"], a["timelapse"]) for a in offers["archives"]] == [
        (81, ["finish_20261101_ab12.jpg"], False)
    ]
    # Not settled yet: nothing is copied onto the attempt.
    assert offers["attempt"]["archive_ids"] == []

    copied = answered(
        bambuddy,
        bambuddy.post(f"{base}/81/copy", json={"what": "finished_photo"}, headers=press()),
    )
    assert (copied["role"], copied["kind"], copied["item_id"]) == (
        "finished",
        "image",
        item.item["id"],
    )
    assert photo.call_count == 1
    refused = bambuddy.post(f"{base}/81/copy", json={"what": "timelapse"}, headers=press())
    problem(refused, "no_timelapse")
    elsewhere = bambuddy.post(f"{base}/99/copy", json={"what": "timelapse"}, headers=press())
    problem(elsewhere, "archive_not_attempts")
    detail = bambuddy.get(f"/api/v1/orders/{item.order['id']}").json()
    assert [a["id"] for a in detail["attachments"]] == [copied["id"]]
