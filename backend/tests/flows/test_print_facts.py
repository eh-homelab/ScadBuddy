"""What a queue_print call counts toward a print bound (#3652): each chosen plate."""

from typing import Any, cast

import httpx
import pytest

from scadbuddy.flows.store import FlowStore
from scadbuddy.workflows.flow_grants import FlowGrants


def _api(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v1/settings/print-options":
        return httpx.Response(200, json={"printer_id": 3})
    return httpx.Response(200, json={"id": "o1", "slug": "box", "layout_plates": [1, 2, 3]})


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


@pytest.mark.parametrize("plate_ids", [3, "1,3", {"1": 3}, [1, "3"], [True], [0]])
async def test_plate_ids_that_are_not_plate_numbers_are_refused_not_a_type_error(
    plate_ids: object,
) -> None:
    """#3762 review: a flow's `plate_ids` that is not a list of plate numbers is a
    ValueError, which `match_grant` turns into an outside verdict (the call asks a
    person, and the print route answers it 422), never a TypeError."""
    with pytest.raises(ValueError, match="plate_ids is not a list of plate numbers"):
        await _prints({"output_id": "o1"}, {"copies": 2, "plate_ids": plate_ids})
