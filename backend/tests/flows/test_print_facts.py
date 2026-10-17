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
