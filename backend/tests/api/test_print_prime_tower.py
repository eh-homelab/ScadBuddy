"""#3628: the prime tower and its "No sparse layers", as a template declares them and as
a print chooses them, sent as Bambu Studio process overrides to the run's slice and to
the dialog's background slice."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy.prime_tower import (
    PRIME_TOWER_KEYS,
    PrimeTowerChoice,
    prime_tower_overrides,
)
from scadbuddy.core.paths import DataPaths
from tests.api.test_filament_profiles import preset_values
from tests.api.test_print_filaments import API, prepared, queue_route
from tests.api.test_print_preview import SLICED, Job, sliced_routes, start_slice
from tests.api.test_print_run_choices import (
    body,
    declare_print_settings,
    run_print,
    run_routes,
)
from tests.api.test_print_supports import overrides, routes
from tests.api.test_send import upload_route
from tests.bambuddy.conftest import recording

pytestmark = pytest.mark.requires_postgres

#: The tower settings as every Bambu process preset has them (``fdm_process_common``).
PRESET = {"enable_prime_tower": "1", "wipe_tower_no_sparse_layers": "0"}


@respx.mock
def test_the_prints_prime_tower_goes_out_as_process_overrides(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    sliced = routes()
    preset_values(PRESET)
    tower = {"enabled": True, "no_sparse_layers": True}

    response = run_print(client, output_id, json={**body(), "prime_tower": tower})

    assert response.status_code == 200, response.text
    assert overrides(sliced) == {
        "enable_prime_tower": "1",
        "wipe_tower_no_sparse_layers": "1",
    }


@respx.mock
def test_a_templates_tower_applies_and_the_prints_choice_wins(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = prepared(client, model)
    declare_print_settings(
        paths, model, {"enable_prime_tower": "1", "wipe_tower_no_sparse_layers": "1"}
    )
    sliced = routes()
    preset_values(PRESET)

    assert run_print(client, output_id, json=body()).status_code == 200
    assert overrides(sliced) == {"enable_prime_tower": "1", "wipe_tower_no_sparse_layers": "1"}

    # Sparse layers back on, over the template's.
    tower = {"enabled": True, "no_sparse_layers": False}
    assert run_print(client, output_id, json={**body(), "prime_tower": tower}).status_code == 200
    assert overrides(sliced) == {"enable_prime_tower": "1", "wipe_tower_no_sparse_layers": "0"}

    # Off, the template's No sparse layers is not sent either, as the dialog strikes it.
    off = {**body(), "prime_tower": {"enabled": False}}
    assert run_print(client, output_id, json=off).status_code == 200
    assert overrides(sliced) == {"enable_prime_tower": "0"}


@respx.mock
def test_an_unreadable_preset_still_sends_the_tower_and_says_nothing(
    client: TestClient, model: str
) -> None:
    """#3648 review: both keys are single values, so a preset that cannot be read has no
    shape to get wrong; the choice goes, with no prime-tower-unconfirmed warning."""
    output_id = prepared(client, model)
    sliced = routes()
    preset_values(resolved=False, values={}, reason="sidecar_unavailable")
    tower = {"enabled": True, "no_sparse_layers": True}

    response = run_print(client, output_id, json={**body(), "prime_tower": tower})

    assert response.status_code == 200, response.text
    assert overrides(sliced) == {"enable_prime_tower": "1", "wipe_tower_no_sparse_layers": "1"}
    assert not [w for w in response.json()["warnings"] if w["kind"] == "prime-tower-unconfirmed"]


@respx.mock
def test_a_setting_the_preset_lacks_is_sent_and_named(client: TestClient, model: str) -> None:
    """#3648 review: a custom process preset with no "No sparse layers" still gets it,
    and the run says the slicer may not take it."""
    output_id = prepared(client, model)
    sliced = routes()
    preset_values({"enable_prime_tower": "1"})
    request = {**body(), "prime_tower": {"enabled": True, "no_sparse_layers": True}}

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=request)
    response = run_print(client, output_id, json=request)

    assert response.status_code == 200, response.text
    assert overrides(sliced)["wipe_tower_no_sparse_layers"] == "1"
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "prime-tower-unconfirmed"]
    assert "wipe_tower_no_sparse_layers (the tower keeps its sparse layers)" in warning["message"]
    assert "enable_prime_tower" not in warning["message"]
    # Checks says it before Print, as the run will (#3648 review).
    assert check.status_code == 200, check.text
    assert warning in check.json()["warnings"]


@respx.mock
def test_a_preset_with_both_settings_says_nothing(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    routes()
    preset_values(PRESET)
    tower = {"enabled": True, "no_sparse_layers": True}

    response = run_print(client, output_id, json={**body(), "prime_tower": tower})

    assert response.status_code == 200, response.text
    assert not [w for w in response.json()["warnings"] if w["kind"] == "prime-tower-unconfirmed"]


@respx.mock
def test_no_choice_sends_no_tower_setting(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    sliced = routes()
    assert run_print(client, output_id, json=body()).status_code == 200
    assert not set(overrides(sliced)) & set(PRIME_TOWER_KEYS)


@respx.mock
def test_the_background_slice_slices_with_the_prints_tower(client: TestClient, model: str) -> None:
    """The live preview is the slice the run would make: the tower reaches it too, and a
    run of the same choices queues it rather than slicing again."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    preset_values(PRESET)
    sliced = Job().routes()
    sliced_routes()
    queued = queue_route()
    request = {**body(), "prime_tower": {"enabled": True, "no_sparse_layers": True}}

    start_slice(client, output_id, request)

    sent: dict[str, Any] = json.loads(sliced.calls.last.request.content)
    assert sent["process_overrides"] == {
        "enable_prime_tower": "1",
        "wipe_tower_no_sparse_layers": "1",
    }
    assert run_print(client, output_id, json=request).status_code == 200
    assert sliced.call_count == 1
    assert queued.called


@pytest.mark.parametrize(
    "tower", [{}, {"enabled": "maybe"}, {"enabled": True, "no_sparse_layers": "sometimes"}]
)
def test_a_tower_choice_it_cannot_send_is_refused(
    client: TestClient, model: str, tower: dict[str, Any]
) -> None:
    output_id = prepared(client, model)
    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={**body(), "prime_tower": tower}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("choice", "wanted"),
    [
        (None, {}),
        (PrimeTowerChoice(enabled=True), {"enable_prime_tower": "1"}),
        (
            PrimeTowerChoice(enabled=True, no_sparse_layers=False),
            {"enable_prime_tower": "1", "wipe_tower_no_sparse_layers": "0"},
        ),
        # With the tower off, "No sparse layers" means nothing and is not sent.
        (PrimeTowerChoice(enabled=False, no_sparse_layers=True), {"enable_prime_tower": "0"}),
    ],
)
def test_the_overrides_a_choice_asks_for(
    choice: PrimeTowerChoice | None, wanted: dict[str, str]
) -> None:
    assert prime_tower_overrides(choice) == wanted


def test_the_tower_keys_are_bambu_studios_and_the_dialogs() -> None:
    """Bambu Studio's names (``PrintConfig.cpp``); ``frontend/src/lib/primeTower.ts``
    copies them, and its own test pins the same two."""
    assert PRIME_TOWER_KEYS == ("enable_prime_tower", "wipe_tower_no_sparse_layers")


class LocalPresets:
    """Bambuddy's ``/local-presets/``, as the recording lists them plus what is made."""

    def __init__(self) -> None:
        self.recorded = recording("local-presets.json")
        self.made: dict[int, dict[str, Any]] = {}
        respx.get(f"{API}/local-presets/").mock(side_effect=self._list)
        respx.get(url__regex=rf"{API}/local-presets/\d+$").mock(side_effect=self._read)
        self.create = respx.post(f"{API}/local-presets/").mock(side_effect=self._create)

    def _list(self, request: httpx.Request) -> httpx.Response:
        rows = [{k: v for k, v in row.items() if k != "setting"} for row in self.made.values()]
        return httpx.Response(
            200, json={**self.recorded, "filament": [*self.recorded["filament"], *rows]}
        )

    def _read(self, request: httpx.Request) -> httpx.Response:
        row = self.made.get(int(request.url.path.rsplit("/", 1)[1]))
        return httpx.Response(200, json=row) if row else httpx.Response(404)

    def _create(self, request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        preset_id = 900 + len(self.made)
        self.made[preset_id] = {
            "id": preset_id,
            "name": sent["name"],
            "preset_type": sent["preset_type"],
            "source": "manual",
            "setting": sent["setting"],
        }
        listed = {k: v for k, v in self.made[preset_id].items() if k != "setting"}
        return httpx.Response(200, json=listed)


def sliced_file(settings: dict[str, Any], *slots: int) -> bytes:
    filaments = "".join(
        f'<filament id="{slot}" type="PETG" color="#688197" used_g="2"/>' for slot in slots
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("Metadata/project_settings.config", json.dumps(settings))
        zipped.writestr(
            "Metadata/slice_info.config",
            f'<config><plate><metadata key="index" value="1"/>{filaments}</plate></config>',
        )
        zipped.writestr("Metadata/plate_1.gcode", "; sliced")
    return buffer.getvalue()


#: Spools 9 and 10 are both Bambu PETG Basic, so both slots get one cloud preset.
SAME_PRESET = {"slots": [{"slot_id": 1, "spool_id": 9}, {"slot_id": 2, "spool_id": 10}]}
PETG = "Bambu PETG Basic @BBL H2C 0.2 nozzle"


@respx.mock
def test_slots_sharing_a_preset_slice_each_with_its_own(client: TestClient, model: str) -> None:
    """#3962: Bambuddy's slicer turns the tower off when every slot's preset has one
    name, so the second slot refs a local preset inheriting the first's. The dialog's
    background slice makes it, and the run of the same choices finds it and queues
    that slice rather than making a second preset and slicing again."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    preset_values(PRESET)
    local = LocalPresets()
    sliced = Job().routes()
    sliced_routes()
    download = respx.get(f"{API}/library/files/{SLICED}/download").mock(
        return_value=httpx.Response(200, content=sliced_file({"enable_prime_tower": "1"}, 1, 2))
    )
    queue_route()
    request = {**body(), "filament_plan": SAME_PRESET, "prime_tower": {"enabled": True}}

    start_slice(client, output_id, request)
    response = run_print(client, output_id, json=request)

    assert response.status_code == 200, response.text
    assert sliced.call_count == 1
    assert json.loads(sliced.calls.last.request.content)["filament_presets"] == [
        {"source": "cloud", "id": "GFSG00_24"},
        {"source": "local", "id": "900"},
    ]
    assert [json.loads(call.request.content) for call in local.create.calls] == [
        {
            "name": f"{PETG} (ScadBuddy slot 2)",
            "preset_type": "filament",
            "setting": {
                "name": f"{PETG} (ScadBuddy slot 2)",
                "inherits": PETG,
                "from": "system",
                "type": "filament",
            },
        }
    ]
    assert not [w for w in response.json()["warnings"] if w["kind"] == "prime-tower-not-applied"]
    # Rewritten slots are what the guard proves worked (#4627 review): still read.
    assert download.called


@respx.mock
def test_a_slice_that_still_lost_its_tower_says_so(client: TestClient, model: str) -> None:
    """Bambuddy refused the preset (its auth denies API keys settings writes): the slot
    keeps the shared one, the print goes ahead, and the run warns."""
    output_id = prepared(client, model)
    sliced = routes()
    preset_values(PRESET)
    respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(403, json={"detail": "API key does not have permission"})
    )
    respx.get(f"{API}/library/files/77/download").mock(
        return_value=httpx.Response(
            200,
            content=sliced_file(
                {"enable_prime_tower": "0", "different_settings_to_system": ["enable_prime_tower"]},
                1,
                2,
            ),
        )
    )

    response = run_print(client, output_id, json={**body(), "filament_plan": SAME_PRESET})

    assert response.status_code == 200, response.text
    sent = json.loads(sliced.calls.last.request.content)
    assert sent["filament_presets"] == [{"source": "cloud", "id": "GFSG00_24"}] * 2
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "prime-tower-not-applied"]
    assert warning["message"].startswith("Bambuddy's slicer dropped the prime tower from plate 1")


#: A slice with no tower whose process lists it as changed: the CLI dropping it, or a
#: user process preset that turns it off over its system parent's on.
NO_TOWER = {"enable_prime_tower": "0", "different_settings_to_system": ["enable_prime_tower"]}


@respx.mock
def test_shared_slots_on_a_preset_with_its_tower_off_say_nothing(
    client: TestClient, model: str
) -> None:
    """#4627 review: no choice, and the process preset's own tower is off, so a slice
    with none is what was asked for, though ``different_settings_to_system`` names it."""
    output_id = prepared(client, model)
    routes()
    preset_values({"enable_prime_tower": "0", "wipe_tower_no_sparse_layers": "0"})
    respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(403, json={"detail": "API key does not have permission"})
    )
    download = respx.get(f"{API}/library/files/77/download").mock(
        return_value=httpx.Response(200, content=sliced_file(NO_TOWER, 1, 2))
    )

    response = run_print(client, output_id, json={**body(), "filament_plan": SAME_PRESET})

    assert response.status_code == 200, response.text
    assert not [w for w in response.json()["warnings"] if w["kind"] == "prime-tower-not-applied"]
    assert not download.called


@respx.mock
def test_slots_distinct_from_the_start_are_not_read(client: TestClient, model: str) -> None:
    """#4627 review: no slot needed a preset of its own, and the CLI keeps the tower
    for distinct names, so the sliced file is not downloaded for the guard."""
    output_id = prepared(client, model)
    sliced = routes()
    preset_values(PRESET)
    download = respx.get(url__regex=rf"{API}/library/files/\d+/download").mock(
        return_value=httpx.Response(200, content=sliced_file(NO_TOWER, 1, 2))
    )

    response = run_print(client, output_id, json={**body(), "prime_tower": {"enabled": True}})

    assert response.status_code == 200, response.text
    assert (
        len(
            {
                json.dumps(ref)
                for ref in json.loads(sliced.calls.last.request.content)["filament_presets"]
            }
        )
        == 2
    )
    assert not download.called
