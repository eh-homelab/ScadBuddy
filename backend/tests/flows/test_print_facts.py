"""What a queue_print call counts toward a print bound (#3652): each chosen plate."""

from typing import Any, cast

import httpx
import pytest

from scadbuddy.flows.store import FlowStore
from scadbuddy.workflows.flow_grants import FlowGrants

ORDER = {
    "id": "ord1",
    "items": [
        {"id": "i1", "quantity": 4, "reference": {"slug": "box"}, "output_id": "o1"},
        {"id": "i2", "quantity": 2, "reference": None, "library_file": {"id": 9}},
    ],
}


def _api(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v1/settings/print-options":
        return httpx.Response(200, json={"printer_id": 3})
    if request.url.path == "/api/v1/orders/ord1":
        return httpx.Response(200, json=ORDER)
    return httpx.Response(200, json={"id": "o1", "slug": "box", "layout_plates": [1, 2, 3]})


async def test_a_line_items_print_names_its_order_and_counts_its_quantity_on_every_plate() -> None:
    """#4252 (orders spec §5.4, §7.2): the item's order, its template, and its quantity
    on each plate of its output; a library file's plates are not counted."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_api), base_url="http://api"
    ) as client:
        grants = FlowGrants(cast(FlowStore, None), client, None)
        source = {"order_id": "ord1", "order_item_id": "i1"}
        facts = await grants.facts_of("queue_print", None, {"source": source, "request": {}})
        assert facts.order_id == "ord1"
        assert facts.models == ["box"]
        assert facts.prints == 12
        assert facts.printer_id == 3
        assert facts.pins == {"/request/printer_id": 3}
        source = {"order_id": "ord1", "order_item_id": "i2"}
        facts = await grants.facts_of("queue_print", None, {"source": source, "request": {}})
        assert (facts.library_file, facts.prints, facts.models) == (9, None, [])
        tool = await grants.facts_of("tool", "order_item_fulfil", {"order_id": "ord1"})
        assert tool.order_id == "ord1"


async def _prints(source: dict[str, Any], request: dict[str, Any]) -> int | None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_api), base_url="http://api"
    ) as client:
        grants = FlowGrants(cast(FlowStore, None), client, None)
        facts = await grants.facts_of("queue_print", None, {"source": source, "request": request})
    return facts.prints


@pytest.mark.parametrize(
    ("request_", "prints"),
    [
        ({"copies": 2}, 2),
        ({"copies": 2, "plate_ids": [1, 3]}, 4),
        ({"copies": 2, "plate_ids": [1, 3, 3]}, 4),
        ({"copies": 2, "all_plates": True}, 6),
        ({"copies": 2, "all_plates": True, "plate_ids": [1]}, 6),
    ],
)
async def test_an_outputs_chosen_plates_each_count(request_: dict[str, Any], prints: int) -> None:
    assert await _prints({"output_id": "o1"}, request_) == prints


async def test_a_library_files_chosen_plates_each_count() -> None:
    assert await _prints({"file_id": 7}, {"copies": 3, "plate_ids": [2, 4]}) == 6
    assert await _prints({"file_id": 7}, {"copies": 3, "all_plates": True}) is None


@pytest.mark.parametrize("plate_ids", [3, "1,3", {"1": 3}, [], [1, "3"], [True], [2.0], [0]])
async def test_plate_ids_that_are_not_plate_numbers_are_refused_not_a_type_error(
    plate_ids: object,
) -> None:
    """#3762 review: a flow's `plate_ids` that is not a list of plate numbers is a
    ValueError, which `match_grant` turns into an outside verdict (the call asks a
    person, and the print route answers it 422), never a TypeError."""
    with pytest.raises(ValueError, match="plate_ids is not a list of plate numbers"):
        await _prints({"output_id": "o1"}, {"copies": 2, "plate_ids": plate_ids})
