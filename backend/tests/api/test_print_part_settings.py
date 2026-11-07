"""#5571: a print's settings per filament slot reach the copy the run uploads, and a
slot the output lacks or a setting out of range is refused before anything goes."""

from __future__ import annotations

import io
import zipfile

import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy.print_run import PART_SETTINGS_OUTPUT_ONLY, PrintRunRequest
from scadbuddy.bambuddy.send import PART_SETTINGS_AS_IS
from scadbuddy.core.paths import DataPaths
from scadbuddy.orders.quote import SliceQuoteRequest
from scadbuddy.render.bambu3mf import MODEL_SETTINGS_NAME
from tests.api.test_filament_profiles import preset_values
from tests.api.test_print_filaments import prepared
from tests.api.test_print_library import SLICED_RUN, library_file, run_library, sliced_file
from tests.api.test_print_run_choices import body, run_print
from tests.api.test_print_supports import routes
from tests.api.test_send import _uploaded_3mf, configure, upload_route
from tests.bambuddy.test_part_settings import without_project_settings

pytestmark = pytest.mark.requires_postgres

#: Every part setting at "the process's": how a slot's settings read back.
UNSET = {
    "top_shell_layers": None,
    "bottom_shell_layers": None,
    "wall_loops": None,
    "sparse_infill_density": None,
    "sparse_infill_pattern": None,
    "ironing_type": None,
}


@respx.mock
def test_the_slots_settings_go_into_the_uploaded_copy(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload = upload_route()
    routes()
    preset_values({})
    request = {**body(), "part_settings": {"1": {"top_shell_layers": 8, "ironing_type": "top"}}}

    response = run_print(client, output_id, json=request)

    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        settings = archive.read(MODEL_SETTINGS_NAME).decode()
    assert '<metadata key="ironing_type" value="top"/>' in settings
    assert '<metadata key="top_shell_layers" value="8"/>' in settings
    # #5627: the result keeps them, for the print record.
    assert response.json()["part_settings"] == {
        "1": {**UNSET, "top_shell_layers": 8, "ironing_type": "top"}
    }


@respx.mock
def test_a_run_without_settings_records_none(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    routes()
    preset_values({})
    request = {**body(), "part_settings": {"1": {}}}

    response = run_print(client, output_id, json=request)

    assert response.status_code == 200, response.text
    assert response.json()["part_settings"] is None


@respx.mock
def test_the_check_gives_the_process_values_the_controls_open_on(
    client: TestClient, model: str
) -> None:
    """#5627: each control opens on the process's own value, read with the check."""
    output_id = prepared(client, model)
    routes()
    preset_values(
        {
            "top_shell_layers": "5",
            "bottom_shell_layers": "3",
            "wall_loops": ["2"],
            "sparse_infill_density": "15%",
            "sparse_infill_pattern": "grid",
            "ironing_type": "sideways",
        }
    )

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=body())

    assert check.status_code == 200, check.text
    # A value out of the bounds (`ironing_type` here) is left out.
    assert check.json()["process_part_settings"] == {
        "top_shell_layers": 5,
        "bottom_shell_layers": 3,
        "wall_loops": 2,
        "sparse_infill_density": 15,
        "sparse_infill_pattern": "grid",
        "ironing_type": None,
    }


@respx.mock
def test_an_unreadable_process_leaves_the_controls_without_values(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    routes()
    preset_values(resolved=False, values={}, reason="sidecar_unavailable")

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=body())

    assert check.status_code == 200, check.text
    assert check.json()["process_part_settings"] is None


@respx.mock
def test_a_library_files_check_reads_no_process_values(client: TestClient) -> None:
    configure(client)
    routes()
    library_file(89)
    values = preset_values({"top_shell_layers": "5"})
    plan = {"slots": [{"slot_id": 1, "spool_id": 9}]}

    check = client.post("/api/v1/print/library/89/check", json={**body(), "filament_plan": plan})

    assert check.status_code == 200, check.text
    assert check.json()["process_part_settings"] is None
    assert not values.called


def test_a_models_choices_remember_its_part_settings(client: TestClient, model: str) -> None:
    """#5627: kept with the model's other choices; a slot that sets nothing is dropped."""
    choices = {"part_settings": {"2": {"top_shell_layers": 8}, "1": {}}}

    saved = client.put(f"/api/v1/print/models/{model}/choices", json=choices)

    assert saved.status_code == 200, saved.text
    assert saved.json()["part_settings"] == {"2": {**UNSET, "top_shell_layers": 8}}
    forgot = client.put(f"/api/v1/print/models/{model}/choices", json={"part_settings": {"1": {}}})
    assert forgot.json()["part_settings"] is None


@respx.mock
def test_a_slot_the_output_lacks_is_refused(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    routes()
    preset_values({})
    request = {**body(), "part_settings": {"2": {"top_shell_layers": 8}}}

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=request)
    response = run_print(client, output_id, json=request)

    assert response.status_code == 422, response.text
    assert "part_settings names slot 2" in response.json()["detail"]
    assert any("part_settings names slot 2" in error for error in check.json()["errors"])


@respx.mock
def test_an_output_that_prints_as_it_is_refuses_part_settings(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#5571 review: an output whose 3MF nothing can be written into (``as_is``: here
    without its ``project_settings.config``) is refused before anything is uploaded,
    never printed under a key that names settings its copy lacks."""
    output_id = prepared(client, model)
    upload = upload_route()
    routes()
    preset_values({})
    [model_3mf] = paths.outputs.glob(f"*/{output_id}/model.3mf")
    model_3mf.write_bytes(without_project_settings(model_3mf.read_bytes()))
    request = {**body(), "part_settings": {"1": {"top_shell_layers": 8}}}

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=request)
    response = run_print(client, output_id, json=request)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == PART_SETTINGS_AS_IS
    assert PART_SETTINGS_AS_IS in check.json()["errors"]
    assert not upload.called


def test_a_setting_out_of_range_is_refused(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    request = {**body(), "part_settings": {"1": {"sparse_infill_density": 150}}}

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=request)

    assert response.status_code == 422, response.text


@respx.mock
@pytest.mark.parametrize("sliced", [False, True])
def test_a_library_file_takes_no_part_settings(client: TestClient, sliced: bool) -> None:
    """#5571 review: a library file keeps its author's parts (#5628), and a sliced one
    prints as it was sliced: either is refused before anything is uploaded or sliced."""
    configure(client)
    routes()
    if sliced:
        sliced_file(104)
        file_id, request = 104, dict(SLICED_RUN)
    else:
        library_file(89)
        plan = {"slots": [{"slot_id": 1, "spool_id": 9}]}
        file_id, request = 89, {**body(), "filament_plan": plan}
    request["part_settings"] = {"1": {"wall_loops": 3}}

    response = run_library(client, file_id, json=request)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == PART_SETTINGS_OUTPUT_ONLY


def test_a_quote_takes_the_same_settings_as_the_print() -> None:
    quote = SliceQuoteRequest.model_validate(
        {
            "filament_plan": body()["filament_plan"],
            "choices": body()["choices"],
            "part_settings": {"1": {"top_shell_layers": 8}},
        }
    )
    settings = {"1": {"top_shell_layers": 8}}
    run = PrintRunRequest.model_validate({**body(), "part_settings": settings})
    assert quote.part_settings == run.part_settings
