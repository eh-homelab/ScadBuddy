"""#6625 Task 2: the client call the profile fetch uses, one printer's K table (the zip
import went with #7176). Tested with respx only."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import BAD_BODY_PROBLEM
from scadbuddy.bambuddy.models import KProfile
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL

API = f"{BASE_URL}/api/v1"
SPEC = Path(__file__).parents[1] / "fixtures" / "bambuddy-openapi-1.2.6b1.json"


@respx.mock
async def test_k_profiles_passes_the_nozzle(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/printers/3/kprofiles/").mock(
        return_value=httpx.Response(
            200,
            json={
                "profiles": [
                    {
                        "slot_id": 1,
                        "nozzle_id": "HH01",
                        "nozzle_diameter": "0.6",
                        "filament_id": "GFA00",
                        "name": "Bambu PLA Basic",
                        "k_value": "0.020000",
                        "setting_id": "PFUS1",
                        "extra": "ignored",
                    }
                ],
                "nozzle_diameter": "0.6",
            },
        )
    )

    rows = await bambuddy.k_profiles(3, "0.6")

    assert route.calls.last.request.url.params["nozzle_diameter"] == "0.6"
    assert rows == [
        KProfile(
            slot_id=1,
            extruder_id=0,
            nozzle_id="HH01",
            nozzle_diameter="0.6",
            filament_id="GFA00",
            name="Bambu PLA Basic",
            k_value="0.020000",
            setting_id="PFUS1",
        )
    ]


@respx.mock
@pytest.mark.parametrize("body", ["<html>", ""])
async def test_k_profiles_body_that_is_not_json_is_a_502(
    bambuddy: BambuddyClient, body: str
) -> None:
    """#6856: a 200 whose body is not JSON (a proxy's HTML page, an empty body) is a 502
    `BAD_BODY_PROBLEM`, an `ApiError` the profile fill and the fetch catch, never a
    `JSONDecodeError` that answers 500."""
    respx.get(f"{API}/printers/3/kprofiles/").mock(return_value=httpx.Response(200, text=body))

    with pytest.raises(ApiError) as caught:
        await bambuddy.k_profiles(3, "0.4")

    assert caught.value.status == 502
    assert caught.value.type == BAD_BODY_PROBLEM


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/v1/printers/{printer_id}/kprofiles/", "get"),
    ],
)
def test_new_operations_are_in_bambuddy_openapi(path: str, method: str) -> None:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    assert method in spec["paths"][path]
