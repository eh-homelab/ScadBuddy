"""#3628: the prime tower and its "No sparse layers", as a template declares them and as
a print chooses them, sent as Bambu Studio process overrides to the run's slice and to
the dialog's background slice."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.accepted_fixes import analysis_request
from scadbuddy.bambuddy import send
from scadbuddy.bambuddy.prime_tower import (
    PRIME_TOWER_KEYS,
    PrimeTowerChoice,
    file_tower_overrides,
    prime_tower_overrides,
    tower_asked,
)
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.bambu3mf import replate_3mf
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
from tests.support.saved_outputs import rewrite_model

pytestmark = [pytest.mark.requires_postgres, pytest.mark.usefixtures("recorded_sb_spools")]

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


def _state_tower_off(client: TestClient, output_id: str) -> None:
    """The output's 3MF as Arrange writes it with the tower off (#3770): it states
    ``enable_prime_tower`` "0" and places no tower."""
    rewrite_model(client, output_id, _tower_off)


def _tower_off(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    settings = json.loads(entries["Metadata/project_settings.config"])
    settings["enable_prime_tower"] = "0"
    settings.pop("wipe_tower_x", None)
    settings.pop("wipe_tower_y", None)
    entries["Metadata/project_settings.config"] = json.dumps(settings).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def _replated(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Each ``tower_on`` the send lays a copy out with (:func:`replate_3mf`)."""
    seen: list[bool] = []

    def replate(payload: bytes, plate: Any, **kwargs: Any) -> bytes:
        seen.append(kwargs.get("tower_on", False))
        return replate_3mf(payload, plate, **kwargs)

    monkeypatch.setattr(send, "replate_3mf", replate)
    return seen


@respx.mock
def test_a_file_arranged_with_the_tower_off_slices_with_it_off(
    client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3770: the slicer's process preset replaces the file's own ``enable_prime_tower``,
    so a print with no choice of its own sends the file's "0" as an override, and the
    send trusts the file's tower off."""
    output_id = prepared(client, model)
    _state_tower_off(client, output_id)
    replated = _replated(monkeypatch)
    sliced = routes()

    assert run_print(client, output_id, json=body()).status_code == 200

    assert overrides(sliced) == {"enable_prime_tower": "0"}
    assert replated == [False]


@respx.mock
def test_a_print_that_turns_the_tower_on_lays_out_room_over_the_files_off(
    client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3770: the print's tower on wins at the slice, so the copy is laid out with room
    for it (``replate_3mf`` ``tower_on``), rather than trusting the file's "0"."""
    output_id = prepared(client, model)
    _state_tower_off(client, output_id)
    replated = _replated(monkeypatch)
    sliced = routes()
    preset_values(PRESET)

    tower = {**body(), "prime_tower": {"enabled": True}}
    assert run_print(client, output_id, json=tower).status_code == 200

    assert overrides(sliced) == {"enable_prime_tower": "1"}
    assert replated == [True]


@respx.mock
def test_a_file_that_states_no_tower_off_is_laid_out_as_before(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_id = prepared(client, model)
    replated = _replated(monkeypatch)
    routes()
    preset_values(PRESET)

    tower = {**body(), "prime_tower": {"enabled": True}}
    assert run_print(client, output_id, json=tower).status_code == 200
    assert replated == [False]


@pytest.mark.parametrize(
    ("stated_off", "choice", "request_overrides", "wanted"),
    [
        (False, None, {}, {}),
        (True, None, {}, {"enable_prime_tower": "0"}),
        (True, None, {"layer_height": "0.2"}, {"enable_prime_tower": "0"}),
        # The print says on or off itself: its own keys go instead.
        (True, PrimeTowerChoice(enabled=True), {}, {}),
        (True, PrimeTowerChoice(enabled=False), {}, {}),
        (True, None, {"enable_prime_tower": "1"}, {}),
        (True, None, {"enable_prime_tower": ["1"]}, {}),
    ],
)
def test_a_files_tower_off_is_sent_only_when_the_print_says_nothing(
    stated_off: bool,
    choice: PrimeTowerChoice | None,
    request_overrides: dict[str, Any],
    wanted: dict[str, str],
) -> None:
    assert file_tower_overrides(stated_off, choice, request_overrides) == wanted


@pytest.mark.parametrize(
    ("choice", "request_overrides", "asked"),
    [
        (None, {}, None),
        (PrimeTowerChoice(enabled=True), {}, True),
        (PrimeTowerChoice(enabled=False), {}, False),
        # The request's override wins at the slice, so it wins here too.
        (PrimeTowerChoice(enabled=False), {"enable_prime_tower": "1"}, True),
        (PrimeTowerChoice(enabled=True), {"enable_prime_tower": ["0"]}, False),
    ],
)
def test_whether_the_print_asks_for_a_tower(
    choice: PrimeTowerChoice | None, request_overrides: dict[str, Any], asked: bool | None
) -> None:
    assert tower_asked(choice, request_overrides) is asked


@pytest.mark.parametrize(
    ("extra", "wanted"),
    [
        ({}, None),
        ({"prime_tower": {"enabled": True, "no_sparse_layers": True}}, (True, True)),
        # The request's override wins, as `tower_asked` reads it at the run.
        ({"process_overrides": {"enable_prime_tower": "1"}}, (True, None)),
        (
            {
                "prime_tower": {"enabled": True, "no_sparse_layers": True},
                "process_overrides": {"enable_prime_tower": "0"},
            },
            (False, None),
        ),
    ],
)
def test_the_analysis_judges_the_runs_tower(
    extra: dict[str, Any], wanted: tuple[bool, bool | None] | None
) -> None:
    """#3770: plate fit (SB4001) gets the run's tower choice, so it refuses what the send
    refuses."""
    tower = analysis_request(PrintRunRequest.model_validate({**body(), **extra})).prime_tower
    assert (None if tower is None else (tower.enabled, tower.no_sparse_layers)) == wanted


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
@pytest.mark.usefixtures("slots_from_bambuddy")
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
    # Every slot reaches the CLI under a name of its own, so the slice is not read for
    # a dropped tower (#4649, #4655 review).
    assert not download.called


@respx.mock
@pytest.mark.usefixtures("slots_from_bambuddy")
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
