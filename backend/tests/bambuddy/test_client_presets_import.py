"""#6625 Task 2: the client calls the profile-fetch worker uses: one zip of presets in,
one printer's K table out. Writes are tested with respx only."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import Scope
from scadbuddy.bambuddy.models import KProfile, PresetImport
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL

API = f"{BASE_URL}/api/v1"
SPEC = Path(__file__).parents[1] / "fixtures" / "bambuddy-openapi-1.2.6b1.json"


@respx.mock
async def test_import_sends_one_zip_as_multipart(bambuddy: BambuddyClient) -> None:
    route = respx.post(f"{API}/local-presets/import").mock(
        return_value=httpx.Response(
            200, json={"success": True, "imported": 2, "skipped": 1, "errors": ["x"]}
        )
    )

    result = await bambuddy.import_local_presets(b"PK\x03\x04zip-bytes")

    assert result == PresetImport(success=True, imported=2, skipped=1, errors=["x"])
    request = route.calls.last.request
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="file"; filename="profiles.zip"' in body
    assert b"PK\x03\x04zip-bytes" in body


@respx.mock
async def test_import_403_names_the_settings_permission(bambuddy: BambuddyClient) -> None:
    respx.post(f"{API}/local-presets/import").mock(
        return_value=httpx.Response(403, json={"detail": "Forbidden"})
    )

    with pytest.raises(ApiError) as caught:
        await bambuddy.import_local_presets(b"zip")

    assert caught.value.extensions["required_scope"] == Scope.MANAGE_LIBRARY.value
    assert Scope.MANAGE_LIBRARY.value in caught.value.detail
    assert "import local presets" in caught.value.detail


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


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/v1/local-presets/import", "post"),
        ("/api/v1/printers/{printer_id}/kprofiles/", "get"),
    ],
)
def test_new_operations_are_in_bambuddy_openapi(path: str, method: str) -> None:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    assert method in spec["paths"][path]
