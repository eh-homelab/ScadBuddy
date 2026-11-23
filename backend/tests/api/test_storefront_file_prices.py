# ruff: noqa: F811  (pytest fixtures imported by name, then taken as arguments)
"""The price and the cart line of a storefront listing of a library file (#6710, spec
2026-11-21 §7.5, §7.6, §10.1). ``POST …/storefront/listings/{id}/price`` takes the colour
of each offered slot and answers the slice price of the listing's held copy; a read that
claims a new slice counts as one render. The cart line is a ``library_file`` item with the
customer's colours, the pinned sha256 and the copy, which it holds. The price read, the
cart add and the checkout make one key for one variant, so a firm price stays firm."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.library.settings_store import ModelPrintChoices
from scadbuddy.orders.component import ORDERS
from scadbuddy.print_library.store import NativeFile
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS
from tests.api.test_storefront_file_listings import (  # noqa: F401  (fixtures)
    BOOK,
    LISTINGS,
    _state,
    body,
    client,
    create,
    holders,
    keychain,
    pla_red,
    publish,
    save,
    two_colours,
    upload,
)
from tests.api.test_storefront_public import TOKEN_A
from tests.api.test_storefront_renders import (  # noqa: F401  (fixtures)
    TOKEN_B,
    CrudCaller,
    FakeSlicer,
    crud,
    sliced,
)
from tests.support.sliced3mf import sliced_3mf

pytestmark = pytest.mark.requires_postgres

#: A slice of the two-slot keychain: both slots print.
TWO_SLOTS = sliced_3mf(plates={1: (3600, [10.0, 5.0])}, gcode={1: b"M620 S0A\nM620 S1A\n"})


def _filament(crud: CrudCaller, material: str, colour: str) -> str:
    made = crud.api.post(
        f"{BOOK}/filaments", json={"material": material, "colour": colour, "cost_per_kg": "20"}
    )
    assert made.status_code in (200, 201), made.text
    filament_id: str = made.json()["id"]
    return filament_id


@pytest.fixture
def pla_blue(crud: CrudCaller) -> str:
    return _filament(crud, "PLA", "#0000FF")


@pytest.fixture
def listed(
    crud: CrudCaller,
    sliced: type[FakeSlicer],
    keychain: NativeFile,
    pla_red: str,
    pla_blue: str,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """The keychain listed in red and blue, with the shop slice profile set."""
    monkeypatch.setattr(FakeSlicer, "file", TWO_SLOTS)
    return publish(crud.api, create(crud.api, body(keychain, [pla_red, pla_blue]))["id"])


def price_body(listing: dict[str, Any], colours: dict[int, str]) -> dict[str, Any]:
    return {
        "listing_updated_at": listing["updated_at"],
        "colours": {str(slot): f for slot, f in colours.items()},
    }


def read(
    crud: CrudCaller, listing: dict[str, Any], colours: dict[int, str], *, token: str = TOKEN_A
) -> Any:
    return crud.post(
        f"/listings/{listing['id']}/price", json=price_body(listing, colours), token=token
    )


def priced(
    crud: CrudCaller, listing: dict[str, Any], colours: dict[int, str], *, token: str = TOKEN_A
) -> dict[str, Any]:
    """The price once it is no longer being worked out."""
    deadline = time.monotonic() + 60
    while True:
        r = read(crud, listing, colours, token=token)
        assert r.status_code == 200, r.text
        answer: dict[str, Any] = r.json()
        if answer["source"] != "pending":
            return answer
        assert time.monotonic() < deadline, "the price was never worked out"
        time.sleep(0.2)


def rows(crud: CrudCaller, sql: str, *args: Any) -> list[dict[str, Any]]:
    with _state(crud.api).projection.pool.connection() as conn:
        return list(conn.execute(sql, args).fetchall())


def counted(crud: CrudCaller) -> int:
    [row] = rows(crud, "SELECT count(*) AS n FROM storefront_renders WHERE counted")
    n: int = row["n"]
    return n


def add(
    crud: CrudCaller,
    listing: dict[str, Any],
    colours: dict[int, str],
    *,
    token: str = TOKEN_A,
    quantity: int = 1,
) -> Any:
    sent = {
        "listing_id": listing["id"],
        "listing_updated_at": listing["updated_at"],
        "colours": {str(slot): f for slot, f in colours.items()},
        "quantity": quantity,
    }
    return crud.post("/draft/items", json=sent, key=uuid.uuid4().hex, token=token)


def submit(crud: CrudCaller, draft: dict[str, Any], *, token: str = TOKEN_A) -> Any:
    sent = {"draft_id": draft["id"], "name": "Ada", "email": "ada@example.com"}
    return crud.post("/draft/submit", json=sent, key=uuid.uuid4().hex, token=token)


def problem_of(r: Any, slug: str, status: int) -> dict[str, Any]:
    assert r.status_code == status, r.text
    found: dict[str, Any] = r.json()
    assert found["type"] == STOREFRONT_PROBLEMS + slug, found
    return found


def test_the_price_is_a_slice_of_the_held_copy(
    crud: CrudCaller,
    listed: dict[str, Any],
    sliced: type[FakeSlicer],
    keychain: NativeFile,
    pla_red: str,
    pla_blue: str,
) -> None:
    first = read(crud, listed, {0: pla_red, 1: pla_blue})
    assert first.status_code == 200, first.text
    assert first.json()["source"] == "pending"
    est = priced(crud, listed, {0: pla_red, 1: pla_blue})
    assert est["source"] == "slice" and est["price_minor"] > 0
    [(file_id, request, subject, slot_presets)] = sliced.started
    assert file_id == f"library:{keychain.id}"
    assert subject == f"storefront_price:{est['price_key']}"
    assert request.printer == "p-1" and slot_presets is not None and set(slot_presets) == {1, 2}
    [row] = rows(
        crud,
        "SELECT status, model_sha256, slot_filaments, listing_id, output_id"
        " FROM storefront_slice_prices WHERE price_key = %s",
        est["price_key"],
    )
    assert (row["status"], row["model_sha256"], row["output_id"]) == (
        "done",
        keychain.sha256,
        None,
    )
    assert (row["slot_filaments"], row["listing_id"]) == ([pla_red, pla_blue], listed["id"])


def test_a_read_that_claims_a_slice_counts_as_a_render(
    crud: CrudCaller, listed: dict[str, Any], pla_red: str, pla_blue: str
) -> None:
    assert crud.api.put("/api/v1/settings", json={"storefront_renders_per_minute": 1}).is_success
    assert counted(crud) == 0
    priced(crud, listed, {0: pla_red, 1: pla_blue})
    assert counted(crud) == 1
    # The same colours again: a price found costs nothing, for any visitor.
    assert priced(crud, listed, {0: pla_red, 1: pla_blue}, token=TOKEN_B)["source"] == "slice"
    assert priced(crud, listed, {0: pla_red, 1: pla_blue})["source"] == "slice"
    assert counted(crud) == 1
    # New colours over the visitor's limit: 429, and no slice starts.
    found = problem_of(read(crud, listed, {0: pla_blue, 1: pla_red}), "render_limited", 429)
    assert found["limit"] == "per_minute"
    assert rows(crud, "SELECT count(*) AS n FROM storefront_slice_prices") == [{"n": 1}]


def test_colours_of_two_materials_clash(
    crud: CrudCaller, sliced: type[FakeSlicer], keychain: NativeFile, pla_red: str
) -> None:
    petg = _filament(crud, "PETG", "#00FF00")
    listing = publish(crud.api, create(crud.api, body(keychain, [pla_red, petg]))["id"])
    found = problem_of(read(crud, listing, {0: pla_red, 1: petg}), "listing_colours_clash", 422)
    assert found["parts"] == ["colours.1"]


def test_each_offered_slot_takes_a_listing_colour(
    crud: CrudCaller, listed: dict[str, Any], pla_red: str
) -> None:
    other = _filament(crud, "PLA", "#FFFFFF")
    found = problem_of(read(crud, listed, {0: pla_red, 1: other}), "input_outside_listing", 422)
    assert (found["name"], found["reason"]) == ("colours.1", "colour")
    found = problem_of(read(crud, listed, {0: pla_red}), "input_outside_listing", 422)
    assert found["name"] == "colours.1"
    found = problem_of(
        read(crud, listed, {0: pla_red, 1: pla_red, 2: pla_red}), "input_outside_listing", 422
    )
    assert found["name"] == "colours.2"


def test_a_changed_listing_is_409(crud: CrudCaller, listed: dict[str, Any], pla_red: str) -> None:
    stale = {**listed, "updated_at": "2020-01-01T00:00:00+00:00"}
    problem_of(read(crud, stale, {0: pla_red, 1: pla_red}), "listing_changed", 409)


def test_an_unpublished_listing_has_no_price(
    crud: CrudCaller, listed: dict[str, Any], pla_red: str
) -> None:
    assert crud.api.post(f"{LISTINGS}/{listed['id']}/unpublish").status_code == 200
    problem_of(read(crud, listed, {0: pla_red, 1: pla_red}), "listing_not_found", 404)


def test_the_price_shown_is_the_price_charged(
    crud: CrudCaller,
    listed: dict[str, Any],
    keychain: NativeFile,
    pla_red: str,
    pla_blue: str,
) -> None:
    """One key for one variant (#6710 §7.4): the slice, the price read and the cart's firm
    price build it from the same subject and the same remembered setup, so a price the
    page shows as firm is the price charged."""
    _state(crud.api).settings_store.set_library_choices(
        keychain.id, ModelPrintChoices(process_overrides={"enable_support": "1"})
    )
    colours = {0: pla_red, 1: pla_blue}
    est = priced(crud, listed, colours)
    assert est["source"] == "slice"
    [request] = rows(
        crud,
        "SELECT o.request FROM operations o JOIN storefront_slice_prices p"
        " ON p.operation_key = o.operation_key WHERE p.price_key = %s",
        est["price_key"],
    )
    assert request["request"]["slice"]["process_overrides"] == {"enable_support": "1"}
    assert priced(crud, listed, colours, token=TOKEN_B) == est
    r = add(crud, listed, colours, token=TOKEN_B, quantity=2)
    assert r.status_code == 200, r.text
    draft = r.json()
    [line] = draft["items"]
    assert (line["kind"], line["price"], line["unit_price_minor"]) == (
        "library_file",
        "firm",
        est["price_minor"],
    )
    orders = _state(crud.api).components.get(ORDERS)
    item = orders.store.item(line["id"])
    assert item is not None and item.library_file is not None
    copy = f"listing-file-{keychain.sha256}"
    assert (item.library_file.id, item.library_file.sha256, item.library_file.copy_key) == (
        keychain.id,
        keychain.sha256,
        copy,
    )
    assert [(c.slot, c.colour, c.filament_id) for c in item.colours] == [
        (0, "#FF0000", pla_red),
        (1, "#0000FF", pla_blue),
    ]
    assert ("order_item", item.id) in holders(crud.api, copy)
    r = submit(crud, draft, token=TOKEN_B)
    assert r.status_code == 200, r.text
    assert r.json()["pay_now"] is True
    [quote] = orders.store.quotes(item.id)
    assert (quote.source, quote.unit_price_minor, quote.quantity) == (
        "storefront",
        est["price_minor"],
        2,
    )


def test_a_line_added_before_its_price_is_priced_at_checkout(
    crud: CrudCaller, listed: dict[str, Any], pla_red: str
) -> None:
    colours = {0: pla_red, 1: pla_red}
    r = add(crud, listed, colours)
    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["items"][0]["price"] == "by_hand"
    priced(crud, listed, colours)
    found = problem_of(submit(crud, draft), "prices_changed", 409)
    assert found["items"] == [{"id": draft["items"][0]["id"], "price": "firm"}]
    r = submit(crud, draft)
    assert r.status_code == 200, r.text
    assert r.json()["pay_now"] is True


def test_a_line_of_a_listing_moved_to_another_file_is_priced_by_hand(
    crud: CrudCaller, listed: dict[str, Any], pla_red: str, tmp_path: Path
) -> None:
    colours = {0: pla_red, 1: pla_red}
    priced(crud, listed, colours)
    r = add(crud, listed, colours)
    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["items"][0]["price"] == "firm"
    other = upload(crud.api, two_colours(tmp_path / "other.3mf", top="#00AA00"))
    moved = save(crud.api, listed, library_file={"id": other.id, "library": "native"})
    publish(crud.api, moved["id"])
    found = problem_of(submit(crud, draft), "prices_changed", 409)
    assert found["items"] == [{"id": draft["items"][0]["id"], "price": "by_hand"}]


def test_a_removed_line_drops_its_hold(
    crud: CrudCaller, listed: dict[str, Any], keychain: NativeFile, pla_red: str
) -> None:
    r = add(crud, listed, {0: pla_red, 1: pla_red})
    assert r.status_code == 200, r.text
    [line] = r.json()["items"]
    copy = f"listing-file-{keychain.sha256}"
    assert ("order_item", line["id"]) in holders(crud.api, copy)
    gone = crud.api.delete(
        "/api/v1/internal/crud/storefront/draft/items/" + line["id"],
        headers={**crud._headers(TOKEN_A, "203.0.113.1", uuid.uuid4().hex)},
    )
    assert gone.status_code == 200, gone.text
    assert ("order_item", line["id"]) not in holders(crud.api, copy)


def test_an_add_with_values_is_outside_the_listing(
    crud: CrudCaller, listed: dict[str, Any], pla_red: str
) -> None:
    sent = {
        "listing_id": listed["id"],
        "listing_updated_at": listed["updated_at"],
        "values": {"width": 4},
        "colours": {"0": pla_red, "1": pla_red},
    }
    r = crud.post("/draft/items", json=sent, key=uuid.uuid4().hex)
    found = problem_of(r, "input_outside_listing", 422)
    assert (found["name"], found["reason"]) == ("width", "unknown")


def test_a_read_that_finds_no_room_costs_nothing(
    crud: CrudCaller, listed: dict[str, Any], pla_red: str, pla_blue: str
) -> None:
    """#7075 review: a read that would claim a slice pays first, and a claim it then
    loses (no room for another slice) gives the render back."""
    with _state(crud.api).projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO storefront_slice_prices (price_key, model_sha256, status, claim_key)"
            " VALUES (%s, %s, 'running', 'other')",
            ("d" * 64, "e" * 64),
        )
    r = read(crud, listed, {0: pla_red, 1: pla_blue})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "pending"
    assert counted(crud) == 0


def test_a_slice_that_never_started_costs_nothing(
    crud: CrudCaller,
    listed: dict[str, Any],
    pla_red: str,
    pla_blue: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7075 review: a start that fails gives the claim back, and the render too."""
    import scadbuddy.api.operations as api_operations

    async def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the slicer refused")

    monkeypatch.setattr(api_operations, "run_operation", refused)
    r = read(crud, listed, {0: pla_red, 1: pla_blue})
    assert r.status_code == 200, r.text
    # The claim went back (`_start` released it), and so did the render.
    assert rows(crud, "SELECT count(*) AS n FROM storefront_slice_prices") == [{"n": 0}]
    assert counted(crud) == 0


def test_an_add_whose_listing_moved_meanwhile_is_409(
    crud: CrudCaller,
    listed: dict[str, Any],
    keychain: NativeFile,
    pla_red: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7075 review: the listing is checked again under the add's transaction, so a line
    never holds a copy its listing no longer sells."""
    from scadbuddy.orders.storefront import drafts, file_prices

    checked = file_prices.checked_file_choice

    async def then_moved(*args: Any, **kwargs: Any) -> Any:
        found = await checked(*args, **kwargs)
        # Another publish pinned other bytes meanwhile.
        with _state(crud.api).projection.pool.connection() as conn:
            conn.execute(
                "UPDATE storefront_listings SET library_file = jsonb_set(library_file,"
                " '{pinned}', to_jsonb(%s::text)) WHERE id = %s",
                ("c" * 64, listed["id"]),
            )
        return found

    monkeypatch.setattr(drafts, "checked_file_choice", then_moved)
    problem_of(add(crud, listed, {0: pla_red, 1: pla_red}), "listing_changed", 409)
    copy = f"listing-file-{keychain.sha256}"
    assert [h for h in holders(crud.api, copy) if h[0] == "order_item"] == []
    assert rows(crud, "SELECT count(*) AS n FROM order_items") == [{"n": 0}]
