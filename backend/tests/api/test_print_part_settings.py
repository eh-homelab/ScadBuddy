"""#5571: a print's settings per filament slot reach the copy the run uploads, and a
slot the output lacks or a setting out of range is refused before anything goes."""

from __future__ import annotations

import io
import zipfile

import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy.print_run import (
    PART_SETTINGS_OUTPUT_ONLY,
    PrintRunRequest,
    part_settings_refusal,
)
from scadbuddy.bambuddy.print_source import LibrarySource
from scadbuddy.orders.quote import SliceQuoteRequest
from scadbuddy.render.bambu3mf import MODEL_SETTINGS_NAME
from tests.api.test_filament_profiles import preset_values
from tests.api.test_print_filaments import prepared
from tests.api.test_print_run_choices import body, run_print
from tests.api.test_print_supports import routes
from tests.api.test_send import _uploaded_3mf, upload_route

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


def test_a_setting_out_of_range_is_refused(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    request = {**body(), "part_settings": {"1": {"sparse_infill_density": 150}}}

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=request)

    assert response.status_code == 422, response.text


def test_only_an_output_takes_part_settings() -> None:
    request = PrintRunRequest.model_validate({**body(), "part_settings": {"1": {"wall_loops": 3}}})
    library = LibrarySource.__new__(LibrarySource)
    assert part_settings_refusal(library, request) == PART_SETTINGS_OUTPUT_ONLY


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
