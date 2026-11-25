# ruff: noqa: F811  (pytest fixtures imported by name, then taken as arguments)
"""The instant read of a visitor's choices (#6921): ``POST …/storefront/listings/{id}/variant``
takes the render's body and answers at once from what the shop holds, keyed before the
render (the variant key). It starts no render and no slice. Its price is ``slice`` (the
firm price of the choices as the shop's settings stand now), ``last`` (the newest price of
the choices, which the page shows marked as updating) or ``none``. Its preview is the
output a priced slice of the choices saved, streamed by ``…/variants/{key}/preview.glb``.
The charge is unchanged: a cart line takes the firm price only (`line_prices.py`)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.library.output_bytes import OutputBytes
from scadbuddy.library.output_rows import ByteWrite
from scadbuddy.library.outputs import PREVIEW_NAME
from scadbuddy.library.settings_store import ModelPrintChoices
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.models import MachineRateBreakdown
from scadbuddy.orders.storefront import slice_price
from tests.api.test_storefront_listings import (  # noqa: F401  (fixtures)
    _state,
    client,
    kek_file,
    template,
)
from tests.api.test_storefront_public import CRUD, TOKEN_A
from tests.api.test_storefront_renders import (  # noqa: F401  (fixtures)
    GLB,
    TOKEN_B,
    TOKEN_C,
    CrudCaller,
    FakeSlicer,
    crud,
    dense_red,
    listing,
    priced,
    render_body,
    render_until_done,
    sliced,
)
from tests.support.output_blobs import put_folder_at_keys
from tests.support.store import local_content, store_pool

pytestmark = pytest.mark.requires_postgres


def rows(crud: CrudCaller, sql: str, *args: Any) -> list[dict[str, Any]]:
    with _state(crud.api).projection.pool.connection() as conn:
        return list(conn.execute(sql, args).fetchall())


def instant(
    crud: CrudCaller, listing: dict[str, Any], values: dict[str, Any], *, token: str = TOKEN_A
) -> Any:
    return crud.post(
        f"/listings/{listing['id']}/variant", token=token, json=render_body(listing, values)
    )


def instant_price(
    crud: CrudCaller, listing: dict[str, Any], values: dict[str, Any], *, token: str = TOKEN_A
) -> dict[str, Any]:
    r = instant(crud, listing, values, token=token)
    assert r.status_code == 200, r.text
    price: dict[str, Any] = r.json()["price"]
    return price


def variant_keys(crud: CrudCaller) -> dict[str, str | None]:
    """Each render row's job and its variant key."""
    return {
        r["job_id"]: r["variant_key"]
        for r in rows(crud, "SELECT job_id, variant_key FROM storefront_renders")
    }


def started_work(crud: CrudCaller, sliced: type[FakeSlicer]) -> tuple[int, int, int, int]:
    """The render rows, the render jobs, the price rows and the slices started."""
    [renders] = rows(crud, "SELECT count(*) AS n FROM storefront_renders")
    [jobs] = rows(crud, "SELECT count(*) AS n FROM render_jobs")
    [prices] = rows(crud, "SELECT count(*) AS n FROM storefront_slice_prices")
    return renders["n"], jobs["n"], prices["n"], len(sliced.started)


def test_a_done_price_records_the_variant_key_of_its_render(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    job = render_until_done(crud, TOKEN_A, listing, {"width": 40})
    est = priced(crud, job["id"])
    assert est["source"] == "slice"
    key = variant_keys(crud)[job["id"]]
    assert key is not None and len(key) == 64
    [row] = rows(
        crud,
        "SELECT variant_key, slot_filaments, slot_colours, output_id"
        " FROM storefront_slice_prices WHERE price_key = %s",
        est["price_key"],
    )
    assert row["variant_key"] == key
    assert row["slot_filaments"] and all(isinstance(f, str) for f in row["slot_filaments"])
    assert row["slot_colours"] and row["output_id"]


def test_the_same_choices_give_the_same_variant_key(
    crud: CrudCaller, listing: dict[str, Any]
) -> None:
    """Two visitors, the same choices: one key. Other choices: another key."""
    one = render_until_done(crud, TOKEN_A, listing, {"width": 45})
    two = render_until_done(crud, TOKEN_B, listing, {"width": 45})
    other = render_until_done(crud, TOKEN_A, listing, {"width": 46})
    keys = variant_keys(crud)
    assert keys[one["id"]] == keys[two["id"]]
    assert keys[other["id"]] not in (None, keys[one["id"]])


def test_a_priced_variant_answers_its_price_and_preview_at_once(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    job = render_until_done(crud, TOKEN_A, listing, {"width": 47})
    est = priced(crud, job["id"])
    before = started_work(crud, sliced)
    r = instant(crud, listing, {"width": 47}, token=TOKEN_B)
    assert r.status_code == 200, r.text
    answer = r.json()
    price = answer["price"]
    assert (price["source"], price["price_minor"]) == ("slice", est["price_minor"])
    assert (price["currency"], price["minor_exponent"]) == ("USD", 2)
    preview = answer["preview"]
    key = variant_keys(crud)[job["id"]]
    assert preview["url"] == f"/api/store/v1/variants/{key}/preview.glb"
    assert preview["colors"] and preview["bbox_mm"]
    # Visitor B never rendered these choices; visitor A's render is its own.
    assert answer["render"] is None
    assert instant(crud, listing, {"width": 47}).json()["render"] == job["id"]
    # It starts no render and no slice.
    assert started_work(crud, sliced) == before


def test_the_preview_route_streams_the_outputs_glb(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    job = render_until_done(crud, TOKEN_A, listing, {"width": 52})
    priced(crud, job["id"])
    key = variant_keys(crud)[job["id"]]
    before = started_work(crud, sliced)
    # Any visitor reads it: the key is of the choices, not of a visitor.
    r = crud.get(f"/variants/{key}/preview.glb", token=TOKEN_C)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == GLB and r.content[:4] == b"glTF"
    assert started_work(crud, sliced) == before


def test_variant_preview_streams_blob(
    crud: CrudCaller,
    listing: dict[str, Any],
    sliced: type[FakeSlicer],
    tmp_path: Path,
    pg_conninfo: str,
) -> None:
    """#7256: the preview is streamed from the output's preview blob, also once its
    NFS file is gone."""
    job = render_until_done(crud, TOKEN_A, listing, {"width": 53})
    priced(crud, job["id"])
    key = variant_keys(crud)[job["id"]]
    [row] = rows(crud, "SELECT output_id FROM storefront_slice_prices WHERE variant_key = %s", key)
    outputs = _state(crud.api).outputs
    with store_pool(pg_conninfo) as pool:
        outputs.bytes = OutputBytes(local_content(tmp_path / "remote", pool))
        try:
            output_id = row["output_id"]
            directory = outputs.directory(output_id)
            glb = (directory / PREVIEW_NAME).read_bytes()
            portal = crud.api.portal
            assert portal is not None
            blobs = portal.call(
                put_folder_at_keys, outputs.bytes, directory, output_id, listing["slug"]
            )
            assert outputs.rows is not None
            outputs.rows.write_byte_copy([ByteWrite(output_id, blobs, "stamp", [])])
            (directory / PREVIEW_NAME).unlink()
            r = crud.get(f"/variants/{key}/preview.glb", token=TOKEN_C)
            assert r.status_code == 200, r.text
            assert r.headers["content-type"] == GLB
            assert r.content == glb
            # #7256 review B-M3: the edge's revalidation is a 304, as the outputs' routes.
            again = crud.api.get(
                f"{CRUD}/variants/{key}/preview.glb",
                headers={
                    **crud._headers(TOKEN_C, "203.0.113.1", None),
                    "If-None-Match": r.headers["etag"],
                },
            )
            assert again.status_code == 304 and again.content == b""
            # No preview at all: no variant.
            outputs.rows.set_blob(output_id, "preview", "output-gone-preview")
            assert crud.get(f"/variants/{key}/preview.glb").status_code == 404
        finally:
            # #7256 review B-M8: the app state is shared; never leave a closed store on it.
            outputs.bytes = None


def test_the_preview_route_refuses_a_key_that_names_no_preview(crud: CrudCaller) -> None:
    r = crud.get(f"/variants/{'0' * 64}/preview.glb")
    assert r.status_code == 404 and r.json()["type"].endswith("variant_not_found")
    assert crud.get("/variants/not-a-key/preview.glb").status_code in (404, 422)


def test_the_instant_price_is_the_last_one_after_the_book_changes(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    job = render_until_done(crud, TOKEN_A, listing, {"width": 48})
    est = priced(crud, job["id"])
    set_fee = crud.api.put("/api/v1/settings", json={"pricing_setup_fee": "4.75"})
    assert set_fee.status_code == 200, set_fee.text
    before = started_work(crud, sliced)
    answer = instant_price(crud, listing, {"width": 48})
    assert (answer["source"], answer["price_minor"]) == ("last", est["price_minor"])
    assert started_work(crud, sliced) == before
    # Once the render's estimate prices it again, the instant read is firm at the new price.
    again = priced(crud, render_until_done(crud, TOKEN_A, listing, {"width": 48})["id"])
    assert again["source"] == "slice" and again["price_minor"] != est["price_minor"]
    answer = instant_price(crud, listing, {"width": 48})
    assert (answer["source"], answer["price_minor"]) == ("slice", again["price_minor"])


def test_the_instant_price_is_the_last_one_after_the_rate_changes(
    crud: CrudCaller,
    listing: dict[str, Any],
    sliced: type[FakeSlicer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = render_until_done(crud, TOKEN_A, listing, {"width": 49})
    est = priced(crud, job["id"])
    rate = MachineRateBreakdown(
        printer="p-1",
        printer_name="H2C",
        source="profile",
        rate_per_hour="2.50",
        currency="USD",
        depreciation_per_hour="2.50",
        intervals="typed",
    )

    async def moved(_rates: Any, _printer: str) -> MachineRateBreakdown:
        return rate

    monkeypatch.setattr(slice_price, "live_rate", moved)
    answer = instant_price(crud, listing, {"width": 49})
    assert (answer["source"], answer["price_minor"]) == ("last", est["price_minor"])


def test_with_nothing_held_of_the_choices_the_read_is_empty(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    before = started_work(crud, sliced)
    r = instant(crud, listing, {"width": 50})
    assert r.status_code == 200, r.text
    assert r.json()["price"]["source"] == "none" and r.json()["price"]["price_minor"] is None
    assert (r.json()["preview"], r.json()["render"]) == (None, None)
    assert started_work(crud, sliced) == before


def test_the_instant_read_checks_the_body_as_the_render_does(
    crud: CrudCaller, listing: dict[str, Any]
) -> None:
    r = instant(crud, listing, {"width": 999})
    assert r.status_code == 422 and r.json()["type"].endswith("input_outside_listing")
    r = crud.post(f"/listings/{listing['id']}/variant", json={"listing_id": listing["id"]})
    assert r.status_code == 422 and r.json()["type"].endswith("render_body_invalid")
    stale = {**render_body(listing, {"width": 40}), "listing_updated_at": "2020-01-01T00:00:00Z"}
    r = crud.post(f"/listings/{listing['id']}/variant", json=stale)
    assert r.status_code == 409 and r.json()["type"].endswith("listing_changed")
    # The path names the listing the body does.
    other = str(uuid.uuid4())
    r = crud.post(f"/listings/{other}/variant", json=render_body(listing, {"width": 40}))
    assert r.status_code == 422 and r.json()["type"].endswith("render_body_invalid")


def add(crud: CrudCaller, listing: dict[str, Any], width: int, *, token: str = TOKEN_A) -> Any:
    body = {**render_body(listing, {"width": width}), "quantity": 1}
    return crud.post("/draft/items", json=body, key=uuid.uuid4().hex, token=token)


def test_a_cart_line_never_takes_the_last_price(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    """The charge is unchanged (#6921): the book moved, the instant read shows the last
    price, and the line added has no firm price: the shop prices it."""
    job = render_until_done(crud, TOKEN_A, listing, {"width": 51})
    assert priced(crud, job["id"])["source"] == "slice"
    crud.api.put("/api/v1/settings", json={"pricing_setup_fee": "5.25"})
    assert instant_price(crud, listing, {"width": 51})["source"] == "last"
    r = add(crud, listing, 51)
    assert r.status_code == 200, r.text
    [item] = r.json()["items"]
    assert item["price"] == "by_hand" and item["unit_price_minor"] is None


def test_a_visitor_with_no_render_adds_a_cached_variant_at_its_firm_price(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    """#6921: the page showed the cached preview and the firm price, so it posted no
    render. The add takes the variant the server recorded and reads the firm price by its
    key, as for a render: never from the browser."""
    job = render_until_done(crud, TOKEN_A, listing, {"width": 53})
    est = priced(crud, job["id"])
    assert instant_price(crud, listing, {"width": 53}, token=TOKEN_B)["source"] == "slice"
    before = started_work(crud, sliced)
    r = add(crud, listing, 53, token=TOKEN_B)
    assert r.status_code == 200, r.text
    [item] = r.json()["items"]
    assert (item["price"], item["unit_price_minor"]) == ("firm", est["price_minor"])
    renders, jobs, prices, slices = started_work(crud, sliced)
    assert (renders, jobs, prices, slices) == before


def test_the_preview_of_an_unpublished_listing_is_404(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    """#6921 review: the price row records the listing its variant key was made for, and
    the preview is served only while that listing is published."""
    job = render_until_done(crud, TOKEN_A, listing, {"width": 54})
    priced(crud, job["id"])
    key = variant_keys(crud)[job["id"]]
    [row] = rows(crud, "SELECT listing_id FROM storefront_slice_prices WHERE variant_key = %s", key)
    assert row["listing_id"] == listing["id"]
    assert crud.get(f"/variants/{key}/preview.glb").status_code == 200
    off = crud.api.post(f"/api/v1/storefront/listings/{listing['id']}/unpublish")
    assert off.status_code == 200, off.text
    r = crud.get(f"/variants/{key}/preview.glb")
    assert r.status_code == 404 and r.json()["type"].endswith("variant_not_found")


def test_a_finish_with_no_variant_keeps_what_the_row_recorded(
    crud: CrudCaller,
    listing: dict[str, Any],
    sliced: type[FakeSlicer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#6921 review: a slice whose operation started before #6921 names no variant. Its
    finish over a row that has one keeps the variant key, the listing, the slots and the
    output."""
    finished: list[tuple[Any, ...]] = []
    finish = slice_price.SlicePrices.finish

    def spy(self: slice_price.SlicePrices, *args: Any, **kwargs: Any) -> Any:
        finished.append(args)
        return finish(self, *args, **kwargs)

    monkeypatch.setattr(slice_price.SlicePrices, "finish", spy)
    job = render_until_done(crud, TOKEN_A, listing, {"width": 55})
    est = priced(crud, job["id"])
    [args] = finished
    columns = (
        "SELECT variant_key, listing_id, slot_filaments, slot_colours, output_id"
        " FROM storefront_slice_prices WHERE price_key = %s"
    )
    [before] = rows(crud, columns, est["price_key"])
    assert all(value is not None for value in before.values())
    finish(_state(crud.api).components.get(ORDERS).slice_prices, *args)
    assert rows(crud, columns, est["price_key"]) == [before]


def test_a_remembered_setup_keys_the_slice_the_instant_read_and_the_checkout_alike(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    """#6710 §7.2, §7.4: the key holds the setup the dialog seeds from the model's
    remembered choices. The instant read and the cart's firm price build their keys from
    those same choices (`_planned`), so with a remembered process override a priced variant
    is still `slice` at once, its line is firm, and the checkout charges that price."""
    _state(crud.api).settings_store.set_model_choices(
        listing["slug"], ModelPrintChoices(process_overrides={"enable_support": "1"})
    )
    job = render_until_done(crud, TOKEN_A, listing, {"width": 58})
    est = priced(crud, job["id"])
    assert est["source"] == "slice"
    [request] = rows(
        crud,
        "SELECT o.request FROM operations o JOIN storefront_slice_prices p"
        " ON p.operation_key = o.operation_key WHERE p.price_key = %s",
        est["price_key"],
    )
    assert request["request"]["slice"]["process_overrides"] == {"enable_support": "1"}
    price = instant_price(crud, listing, {"width": 58}, token=TOKEN_B)
    assert (price["source"], price["price_minor"]) == ("slice", est["price_minor"])
    r = add(crud, listing, 58, token=TOKEN_B)
    assert r.status_code == 200, r.text
    draft = r.json()
    [item] = draft["items"]
    assert (item["price"], item["unit_price_minor"]) == ("firm", est["price_minor"])
    body = {"draft_id": draft["id"], "name": "Ada", "email": "ada@example.com"}
    r = crud.post("/draft/submit", json=body, key=uuid.uuid4().hex, token=TOKEN_B)
    assert r.status_code == 200, r.text
    assert r.json()["pay_now"] is True


def test_the_price_shown_and_the_price_charged_are_of_one_row(
    crud: CrudCaller, listing: dict[str, Any], sliced: type[FakeSlicer]
) -> None:
    """#6921 review: a newer row of the variant with no output (one adopted from before
    #6921) beside the row whose slice saved the output. The instant read and the cart add
    both take the row with the output: its price, its preview and its 3MF."""
    job = render_until_done(crud, TOKEN_A, listing, {"width": 56})
    est = priced(crud, job["id"])
    key = variant_keys(crud)[job["id"]]
    [real] = rows(
        crud, "SELECT slot_filaments FROM storefront_slice_prices WHERE variant_key = %s", key
    )
    with _state(crud.api).projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO storefront_slice_prices (price_key, model_sha256, status, currency,"
            " unit_price_minor, total_minor, lines, variant_key, listing_id, slot_filaments,"
            " finished_at) VALUES (%s, %s, 'done', 'USD', 99999, 99999, '[]'::jsonb, %s, %s,"
            " %s, clock_timestamp() + interval '1 hour')",
            ("f" * 64, "e" * 64, key, listing["id"], Jsonb(real["slot_filaments"])),
        )
    answer = instant(crud, listing, {"width": 56}, token=TOKEN_B).json()
    assert (answer["price"]["source"], answer["price"]["price_minor"]) == (
        "slice",
        est["price_minor"],
    )
    assert answer["preview"] is not None
    r = add(crud, listing, 56, token=TOKEN_B)
    assert r.status_code == 200, r.text
    [item] = r.json()["items"]
    assert (item["price"], item["unit_price_minor"]) == ("firm", est["price_minor"])
