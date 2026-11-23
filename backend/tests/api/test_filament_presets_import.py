"""#7244: ``POST /filament/presets/bambuddy-import``, the one-time import of Bambuddy's
filament presets into ScadBuddy's own, whatever ``filament_sync.presets`` says."""

from __future__ import annotations

from typing import Any

import respx
from fastapi.testclient import TestClient

from tests.api.test_filament import TYPES, _type
from tests.api.test_send import API, configure

IMPORT = "/api/v1/filament/presets/bambuddy-import"


def _preset(preset_id: int, name: str, stamp: str) -> dict[str, Any]:
    return {
        "id": preset_id,
        "name": name,
        "preset_type": "filament",
        "source": "orcaslicer",
        "updated_at": stamp,
        "setting": {"name": name, "filament_type": ["PLA"], "inherits": ""},
    }


def test_the_presets_import_needs_bambuddy(client: TestClient) -> None:
    refused = client.post(IMPORT)
    assert refused.status_code == 409, refused.text


def test_the_presets_import_runs_with_presets_set_to_push(client: TestClient) -> None:
    """The direction gates only the Schedule's pull: a person's import runs with
    ``presets`` ``sb_to_bb`` too, and a second one finds nothing new."""
    configure(client, filament_sync={"presets": "sb_to_bb"})
    made = _type(client)
    nine = _preset(9, "Nine", "2026-11-23T01:00:00")
    with respx.mock(assert_all_called=False) as router:
        listing = {"filament": [{k: v for k, v in nine.items() if k != "setting"}]}
        router.get(f"{API}/local-presets/").respond(json=listing)
        router.get(f"{API}/local-presets/9").respond(json=nine)
        bound = client.put(
            f"{TYPES}/{made['id']}/presets",
            json=[{"printer_model": "H2C", "nozzle_diameter": "0.4", "slicer_filament": "9"}],
        )
        assert bound.status_code == 200, bound.text
        first = client.post(IMPORT)
        assert first.status_code == 200, first.text
        assert first.json() == {"adopted": 1, "updated": 0}
        again = client.post(IMPORT)
        assert again.json() == {"adopted": 0, "updated": 0}
    (row,) = client.get(f"{TYPES}/{made['id']}").json()["presets"]
    assert row["slicer_filament"].startswith("sb-")
