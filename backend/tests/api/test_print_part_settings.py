"""#5571: a print's settings per filament slot reach the copy the run uploads, and a
slot the file lacks is refused before anything goes (any key and value is taken, #2998). #5628:
a library file takes them as an output does."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy.print_run import PART_SETTINGS_SLICED, PrintRunRequest
from scadbuddy.bambuddy.send import PART_SETTINGS_AS_IS, PART_SETTINGS_UNWRITABLE
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

#: The dialog's check, asking for the process's part settings too (#5627).
WITH_VALUES = "?process_values=true"


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
    # #5627: the result keeps them, for the print record: only what is set.
    assert response.json()["part_settings"] == {"1": {"top_shell_layers": 8, "ironing_type": "top"}}


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

    check = client.post(f"/api/v1/print/outputs/{output_id}/check{WITH_VALUES}", json=body())

    assert check.status_code == 200, check.text
    # #2998: a value Bambu Studio may not take (`ironing_type` here) is read as it is.
    assert check.json()["process_part_settings"] == {
        "top_shell_layers": 5,
        "bottom_shell_layers": 3,
        "wall_loops": 2,
        "sparse_infill_density": 15,
        "sparse_infill_pattern": "grid",
        "ironing_type": "sideways",
    }


@respx.mock
def test_an_unreadable_process_leaves_the_controls_without_values(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    routes()
    preset_values(resolved=False, values={}, reason="sidecar_unavailable")

    check = client.post(f"/api/v1/print/outputs/{output_id}/check{WITH_VALUES}", json=body())

    assert check.status_code == 200, check.text
    assert check.json()["process_part_settings"] is None


@respx.mock
def test_a_check_that_does_not_ask_reads_no_process_values(client: TestClient, model: str) -> None:
    """#5627: the read can be one more Bambuddy call, so only a check that asks makes it."""
    output_id = prepared(client, model)
    routes()
    values = preset_values({"top_shell_layers": "5"})

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=body())

    assert check.status_code == 200, check.text
    assert check.json()["process_part_settings"] is None
    assert not values.called


@respx.mock
def test_a_library_files_check_gives_the_process_values(client: TestClient) -> None:
    """#5628 lets a library file take part settings, so its controls open on them too."""
    configure(client)
    routes()
    library_file(89)
    preset_values({"top_shell_layers": "5"})
    plan = {"slots": [{"slot_id": 1, "spool_id": 9}]}

    check = client.post(
        f"/api/v1/print/library/89/check{WITH_VALUES}", json={**body(), "filament_plan": plan}
    )

    assert check.status_code == 200, check.text
    assert check.json()["process_part_settings"] == {"top_shell_layers": 5}


def test_a_models_choices_remember_its_part_settings(client: TestClient, model: str) -> None:
    """#5627: kept with the model's other choices; a slot that sets nothing is dropped."""
    choices = {"part_settings": {"2": {"top_shell_layers": 8}, "1": {}}}

    saved = client.put(f"/api/v1/print/models/{model}/choices", json=choices)

    assert saved.status_code == 200, saved.text
    assert saved.json()["part_settings"] == {"2": {"top_shell_layers": 8}}
    forgot = client.put(f"/api/v1/print/models/{model}/choices", json={"part_settings": {"1": {}}})
    # None remembered reads as an entry from before the field existed.
    assert "part_settings" not in forgot.json()


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


def test_a_value_that_is_no_setting_at_all_is_refused(client: TestClient, model: str) -> None:
    """#2998: no key, value or range is refused (a density of 150 goes to the slicer);
    only a value no Bambu config can hold, an object, fails the request."""
    output_id = prepared(client, model)
    request = {**body(), "part_settings": {"1": {"sparse_infill_density": {"a": 1}}}}

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


@respx.mock
def test_a_library_file_whose_parts_settings_cannot_be_read_says_so(
    client: TestClient, tmp_path: Path
) -> None:
    """#5646: an author's file whose ``model_settings.config`` is not XML is laid out,
    but its parts cannot take settings: the refusal names that file, not ``as_is``."""
    configure(client)
    upload = flow_copy_routes()
    library_file(89, content=author_two_colours(tmp_path, "<config><object>"))
    run_routes()
    plan = {"slots": [{"slot_id": 1, "spool_id": 9}]}
    request = {**body(), "filament_plan": plan, "part_settings": {"1": {"wall_loops": 3}}}

    check = client.post("/api/v1/print/library/89/check", json=request)
    response = run_library(client, 89, json=request)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == PART_SETTINGS_UNWRITABLE
    assert PART_SETTINGS_UNWRITABLE in check.json()["errors"]
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
