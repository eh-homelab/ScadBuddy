"""#5571: a print's settings per filament slot reach the copy the run uploads, and a
slot the file lacks or a setting out of range is refused before anything goes. #5628:
a library file takes them as an output does."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy.print_run import PART_SETTINGS_SLICED, PrintRunRequest
from scadbuddy.bambuddy.send import PART_SETTINGS_AS_IS
from scadbuddy.core.paths import DataPaths
from scadbuddy.orders.quote import SliceQuoteRequest
from scadbuddy.render.bambu3mf import MODEL_SETTINGS_NAME, layout_of
from tests.api.test_filament_profiles import preset_values
from tests.api.test_print_filaments import prepared, queue_route, slice_routes
from tests.api.test_print_library import (
    SLICED_RUN,
    flow_copy_routes,
    library_3mf,
    library_file,
    run_library,
    sliced_file,
)
from tests.api.test_print_run_choices import body, run_print, run_routes
from tests.api.test_print_supports import routes
from tests.api.test_send import _uploaded_3mf, configure, upload_route
from tests.bambuddy.test_part_settings import author_two_colours, without_project_settings

pytestmark = pytest.mark.requires_postgres


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
def test_a_sliced_library_file_takes_no_part_settings(client: TestClient) -> None:
    """#5571 review: a sliced file prints as it was sliced, so it is refused before
    anything is queued."""
    configure(client)
    routes()
    sliced_file(104)
    request = {**SLICED_RUN, "part_settings": {"1": {"wall_loops": 3}}}

    response = run_library(client, 104, json=request)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == PART_SETTINGS_SLICED


@respx.mock
def test_a_library_files_parts_take_the_slots_settings(client: TestClient, tmp_path: Path) -> None:
    """#5628: an author's file in Bambuddy's library is copied with the settings on its
    parts of the slot, its layout kept, and the copy is what is sliced."""
    configure(client)
    upload = flow_copy_routes()
    library_file(89, content=author_two_colours(tmp_path))
    run_routes()
    sliced = slice_routes()
    queue_route()
    plan = {"slots": [{"slot_id": 1, "spool_id": 9}]}
    request = {**body(), "filament_plan": plan, "part_settings": {"2": {"top_shell_layers": 8}}}

    check = client.post("/api/v1/print/library/89/check", json=request)
    response = run_library(client, 89, json=request)

    assert check.status_code == 200, check.text
    assert not any("part" in error for error in check.json()["errors"]), check.json()
    assert response.status_code == 200, response.text
    copy = _uploaded_3mf(upload)
    assert layout_of(copy) == "author"
    with zipfile.ZipFile(io.BytesIO(copy)) as archive:
        settings = archive.read(MODEL_SETTINGS_NAME).decode()
    assert settings.count('<metadata key="top_shell_layers" value="8"/>') == 1
    assert "/library/files/141/slice" in str(sliced.calls.last.request.url)


@respx.mock
def test_a_slot_the_library_file_lacks_is_refused(client: TestClient, tmp_path: Path) -> None:
    """#5628: a slot no printed part of the file is made in is refused before anything
    is uploaded, naming the slots its parts are."""
    configure(client)
    upload = flow_copy_routes()
    library_file(89, content=author_two_colours(tmp_path))
    run_routes()
    plan = {"slots": [{"slot_id": 1, "spool_id": 9}]}
    request = {**body(), "filament_plan": plan, "part_settings": {"3": {"wall_loops": 3}}}

    response = run_library(client, 89, json=request)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "part_settings names slot 3, but no printed part of this file is made in it"
        " (its parts' slots: 1, 2)."
    )
    assert not upload.called


@respx.mock
def test_a_library_file_that_prints_as_it_is_refuses_part_settings(client: TestClient) -> None:
    """#5628: a file with no ``project_settings.config`` (another slicer's) is ``as_is``:
    its copy could not carry the settings, so it is refused before any upload."""
    configure(client)
    upload = flow_copy_routes()
    library_file(89, content=without_project_settings(library_3mf()))
    run_routes()
    plan = {"slots": [{"slot_id": 1, "spool_id": 9}]}
    request = {**body(), "filament_plan": plan, "part_settings": {"1": {"wall_loops": 3}}}

    response = run_library(client, 89, json=request)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == PART_SETTINGS_AS_IS
    assert not upload.called


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
