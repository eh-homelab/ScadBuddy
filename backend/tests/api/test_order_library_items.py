"""A library file as an order line item through the API (#5544): `POST /orders/{id}/items`
with ``library_file_id`` (its exclusivity, the file read as the library print route reads
it), a manual quote, the print of the file itself on the library's run subject, and the
refusals once the file is gone. Bambuddy is the ``respx`` recordings the library print
tests use."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api import library_print
from scadbuddy.core.problems import ApiError
from scadbuddy.render.bambu3mf import MODEL_SETTINGS_NAME
from tests.api.conftest import follow_operation
from tests.api.test_customers import create as create_customer
from tests.api.test_order_print import Projects, answered
from tests.api.test_order_quote import BambuddySlices, price_book, sliced_plate
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import add_item, problem, quote_and_accept
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_library import flow_copy_routes, library_file, one_color
from tests.api.test_print_run_choices import body, follow_run, run_routes
from tests.api.test_send import API, _uploaded_3mf, configure
from tests.bambuddy.test_part_settings import author_two_colours
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

FILE_ID = 89


@pytest.fixture
def bambuddy(client: TestClient) -> Iterator[TestClient]:
    with respx.mock as router:
        router.route(host="testserver").pass_through()
        configure(client)
        one_color(FILE_ID)
        flow_copy_routes()
        library_file(FILE_ID)
        run_routes()
        Projects()
        yield client


def an_order(client: TestClient) -> dict[str, Any]:
    return create_order(client, create_customer(client)["id"])


def gone(file_id: int = FILE_ID) -> None:
    """The file deleted in Bambuddy."""
    respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(404, json={"detail": "File not found"})
    )


def test_a_library_file_is_added_as_an_item(bambuddy: TestClient) -> None:
    order = an_order(bambuddy)
    item = add_item(bambuddy, order["id"], library_file_id=FILE_ID, quantity=2, sku="BR")
    assert (item["kind"], item["reference"], item["output_id"]) == ("library_file", None, None)
    assert item["library_file"] == {"id": FILE_ID, "filename": "file-89.3mf", "library": "bambuddy"}
    assert (item["title"], item["status"], item["colours"]) == ("file-89.3mf", "draft", [])
    # `kind` may be named; the order's detail shows the source.
    named = add_item(
        bambuddy, order["id"], kind="library_file", library_file_id=FILE_ID, quantity=1, title="B"
    )
    assert named["title"] == "B"
    detail = bambuddy.get(f"/api/v1/orders/{order['id']}").json()
    assert [i["library_file"]["id"] for i in detail["items"]] == [FILE_ID, FILE_ID]
    assert detail["totals"]["unpriced_items"] == [item["id"], named["id"]]


def test_a_library_file_item_takes_no_other_source(bambuddy: TestClient) -> None:
    order = an_order(bambuddy)
    url = f"/api/v1/orders/{order['id']}/items"
    base = {"library_file_id": FILE_ID, "quantity": 1}
    extras: list[dict[str, Any]] = [
        {"output_id": "out-1"},
        {"slug": "demo"},
        {"commit": "a" * 40},
        {"inputs": {"params": {}}},
        {"kind": "custom", "title": "x", "unit_price_minor": 1},
        {"kind": "request", "title": "x"},
        {"kind": "model"},
        {"unit_price_minor": 5},
        {"requires_printing": True},
        {"accept": False},
        {"library_file_id": 0},
    ]
    for extra in extras:
        refused = bambuddy.post(url, json={**base, **extra})
        assert refused.status_code == 422, (extra, refused.text)
    no_file = bambuddy.post(url, json={"kind": "library_file", "quantity": 1})
    assert no_file.status_code == 422, no_file.text


def test_a_file_the_library_does_not_have_or_print_is_refused(bambuddy: TestClient) -> None:
    order = an_order(bambuddy)
    url = f"/api/v1/orders/{order['id']}/items"
    gone(90)
    missing = bambuddy.post(url, json={"library_file_id": 90, "quantity": 1})
    assert missing.status_code == 422, missing.text
    assert missing.json()["errors"][0]["loc"] == ["body", "library_file_id"]
    # A native id is the other library's on `library_backend=bambuddy`: not there.
    native = bambuddy.post(url, json={"library_file_id": 2**40, "quantity": 1})
    assert native.status_code == 422, native.text
    library_file(91, file_type="pdf")
    unprintable = bambuddy.post(url, json={"library_file_id": 91, "quantity": 1})
    assert unprintable.status_code == 422, unprintable.text
    assert "3MF and STL" in unprintable.json()["detail"]
    assert bambuddy.get(f"/api/v1/orders/{order['id']}").json()["items"] == []


def test_an_accepted_order_takes_a_library_file_item_unpriced(bambuddy: TestClient) -> None:
    order = an_order(bambuddy)
    first = add_item(bambuddy, order["id"], library_file_id=FILE_ID, quantity=1)
    quote_and_accept(bambuddy, order["id"], first["id"])
    assert bambuddy.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    plain = bambuddy.post(
        f"/api/v1/orders/{order['id']}/items", json={"library_file_id": FILE_ID, "quantity": 1}
    )
    problem(plain, "item_locked")
    late = bambuddy.post(
        f"/api/v1/orders/{order['id']}/items/after-acceptance",
        json={"library_file_id": FILE_ID, "quantity": 1},
    )
    assert late.status_code == 201, late.text
    assert (late.json()["kind"], late.json()["status"]) == ("library_file", "draft")
    refused = bambuddy.post(
        f"/api/v1/orders/{order['id']}/items/after-acceptance",
        json={"library_file_id": FILE_ID, "quantity": 1, "accept": True},
    )
    assert refused.status_code == 422, refused.text


def test_a_library_file_item_prints_its_file(bambuddy: TestClient) -> None:
    # The copy is filed in the item's project's folder (§8.1), empty so far.
    respx.get(f"{API}/library/files", params={"folder_id": 801}).mock(
        return_value=httpx.Response(200, json=[])
    )
    slice_routes()
    queued = queue_route()
    order = an_order(bambuddy)
    item = add_item(bambuddy, order["id"], library_file_id=FILE_ID, quantity=2)
    quote_and_accept(bambuddy, order["id"], item["id"])
    assert bambuddy.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    path = f"/api/v1/orders/{order['id']}/items/{item['id']}"
    attempt = answered(
        bambuddy, bambuddy.post(f"{path}/print", json=body(), headers={"Idempotency-Key": "lf-1"})
    )
    assert (attempt["copies"], attempt["sliced_sha256"]) == (2, None)
    run = follow_run(bambuddy, attempt["print_run_id"])
    assert run["status"] == "succeeded", run
    # The library file's own run, as `POST /print/library/{id}/run` starts it.
    assert run["subject"] == f"library:{FILE_ID}"
    assert json.loads(queued.calls.last.request.content)["quantity"] == 2
    detail = bambuddy.get(f"/api/v1/orders/{order['id']}").json()
    # Queued, or already printing: the run's follow announces the print as it starts
    # (`bambuddy/progress.py`, `print.progress`), and `Lifecycle.follow_print` then moves
    # a queued item to printing, at a time this test does not control.
    assert detail["items"][0]["status"] in ("queued", "printing")


def test_a_file_gone_since_is_refused_on_quote_and_print(bambuddy: TestClient) -> None:
    order = an_order(bambuddy)
    item = add_item(bambuddy, order["id"], library_file_id=FILE_ID, quantity=1)
    quote_and_accept(bambuddy, order["id"], item["id"])
    assert bambuddy.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    gone()
    path = f"/api/v1/orders/{order['id']}/items/{item['id']}"
    printed = bambuddy.post(f"{path}/print", json=body(), headers=press())
    assert problem(printed, "library_file_gone")["items"] == [item["id"]]
    # A second item, still unpriced: its slice quote is refused before the 202.
    other = an_order(bambuddy)
    library_file(FILE_ID)
    unpriced = add_item(bambuddy, other["id"], library_file_id=FILE_ID, quantity=1)
    gone()
    pla, _ = price_book(bambuddy)
    dialog = {name: value for name, value in body().items() if name != "copies"}
    quoted = bambuddy.post(
        f"/api/v1/orders/{other['id']}/items/{unpriced['id']}/quote",
        json={**dialog, "filaments": {"0": pla}},
        headers=press(),
    )
    problem(quoted, "library_file_gone")
    # It is still priced by hand, and shown by its name as it was added.
    quote_and_accept(bambuddy, other["id"], unpriced["id"])
    shown = bambuddy.get(f"/api/v1/orders/{other['id']}").json()["items"][0]
    assert shown["library_file"]["filename"] == "file-89.3mf"


def test_a_library_file_items_print_is_worded_for_a_caller_of_the_api(
    bambuddy: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5602 review: the library file's run gets the operation's `api` as the output's
    does, so its refusal names `path` for a caller that is not the page."""
    asked: list[bool | None] = []

    async def refused(*args: Any, api: bool | None = None, **kwargs: Any) -> Any:
        asked.append(api)
        raise ApiError(422, "refused for the test")

    monkeypatch.setattr(library_print, "start_library_run", refused)
    for headers in (press(), {**press(), "X-ScadBuddy-Request": "1"}):
        order = an_order(bambuddy)
        item = add_item(bambuddy, order["id"], library_file_id=FILE_ID, quantity=1)
        quote_and_accept(bambuddy, order["id"], item["id"])
        assert bambuddy.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
        path = f"/api/v1/orders/{order['id']}/items/{item['id']}"
        started = bambuddy.post(f"{path}/print", json=body(), headers=headers)
        if started.status_code == 202:
            op = follow_operation(bambuddy, started.json(), timeout=60)
            assert op["status"] == "failed", op
        else:
            assert started.status_code == 422, started.text
    assert asked == [True, None]


def test_a_library_file_items_slice_quote_writes_its_part_settings(
    bambuddy: TestClient, tmp_path: Path
) -> None:
    """#5646: a library file item's slice quote with ``part_settings`` goes through
    ``_library_quote`` and ``start_library_quote_preview`` to the writer: the copy it
    uploads and slices carries slot 2's setting on slot 2's part alone, and the quote
    records the settings it sliced with."""
    library_file(FILE_ID, content=author_two_colours(tmp_path))
    upload = flow_copy_routes()
    slices = BambuddySlices({1: sliced_plate(1, 3600, [12.5, 3.0])}, copy=141)
    pla, petg = price_book(bambuddy)
    order = an_order(bambuddy)
    item = add_item(bambuddy, order["id"], library_file_id=FILE_ID, quantity=1)
    dialog = {name: value for name, value in body().items() if name != "copies"}
    settings = {"2": {"top_shell_layers": 8}}

    started = bambuddy.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/quote",
        json={**dialog, "filaments": {"0": pla, "1": petg}, "part_settings": settings},
        headers=press(),
    )

    assert started.status_code == 202, started.text
    op = follow_operation(bambuddy, started.json(), timeout=120)
    assert op["status"] == "succeeded", op
    assert op["result"]["part_settings"] == settings
    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        written = archive.read(MODEL_SETTINGS_NAME).decode()
    assert written.count('<metadata key="top_shell_layers" value="8"/>') == 1
    assert "/library/files/141/slice" in str(slices.posted.calls.last.request.url)
