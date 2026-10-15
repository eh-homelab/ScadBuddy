"""#3628: the prime tower and its "No sparse layers", as a template declares them and as
a print chooses them, sent as Bambu Studio process overrides to the run's slice and to
the dialog's background slice."""

from __future__ import annotations

import json
from typing import Any

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
from tests.api.test_print_filaments import prepared, queue_route
from tests.api.test_print_preview import Job, sliced_routes, start_slice
from tests.api.test_print_run_choices import body, declare_print_settings, run_print, run_routes
from tests.api.test_print_supports import overrides, routes
from tests.api.test_send import upload_route

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
