"""#5626: a template's settings per colour part reach the copy a run uploads, under the
request's ``part_settings``, and the output's naming carries them as its slots. The
dialog's background slice and the slice quote upload them too, and refuse a model.json
that refuses them, as the run does (#5649)."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy.part_settings import PartSettings
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.outputs import PARAMS_NAME
from scadbuddy.render.bambu3mf import MODEL_SETTINGS_NAME
from tests.api.conftest import follow_operation
from tests.api.test_filament_profiles import preset_values
from tests.api.test_internal_outputs import local, remote, state
from tests.api.test_order_quote import COPY as QUOTE_COPY
from tests.api.test_order_quote import BambuddySlices, Shop, sliced_plate
from tests.api.test_order_quote import bambuddy as bambuddy  # a fixture the quote tests use
from tests.api.test_print_filaments import prepared
from tests.api.test_print_preview import Job, sliced_routes, start_slice
from tests.api.test_print_run_choices import body, run_print, run_routes
from tests.api.test_print_supports import routes
from tests.api.test_send import _uploaded_3mf, upload_route
from tests.api.test_versions import SLUG
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres


def declare(
    client: TestClient, paths: DataPaths, model: str, output_id: str, colour: str | None = None
) -> None:
    """Give ``model`` a ``label_color`` part setting, and the output a ``label_color``
    of ``colour`` (its slot 1's colour when ``None``)."""
    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    meta["part_print_settings"] = {"label_color": {"top_shell_layers": 8, "wall_loops": 3}}
    paths.model_meta(model).write_text(json.dumps(meta), encoding="utf-8")
    source = paths.model_source(model)
    source.write_text(
        'label_color = "#FFFFFF"; // color\n' + source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    outputs = state(client).outputs
    params_file = outputs.directory(output_id) / PARAMS_NAME
    params = json.loads(params_file.read_text(encoding="utf-8"))
    params["label_color"] = colour or outputs.get(output_id).colors[0]
    params_file.write_text(json.dumps(params), encoding="utf-8")


def misdeclare(paths: DataPaths, model: str) -> None:
    """Give ``model`` a ``part_print_settings`` key that is no colour parameter: a typo
    the model.json load refuses."""
    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    meta["part_print_settings"] = {"lable_color": {"top_shell_layers": 8}}
    paths.model_meta(model).write_text(json.dumps(meta), encoding="utf-8")


def copy_settings(upload: respx.Route) -> str:
    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        return archive.read(MODEL_SETTINGS_NAME).decode()


def uploaded_settings(client: TestClient, output_id: str, request: dict[str, Any]) -> str:
    upload = upload_route()
    routes()
    preset_values({})
    response = run_print(client, output_id, json=request)
    assert response.status_code == 200, response.text
    return copy_settings(upload)


@respx.mock
def test_the_templates_settings_go_into_the_copy(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = prepared(client, model)
    declare(client, paths, model, output_id)

    settings = uploaded_settings(client, output_id, body())

    assert '<metadata key="top_shell_layers" value="8"/>' in settings
    assert '<metadata key="wall_loops" value="3"/>' in settings


@respx.mock
def test_the_requests_settings_win_per_key(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = prepared(client, model)
    declare(client, paths, model, output_id)
    request = {**body(), "part_settings": {"1": {"top_shell_layers": 4}}}

    settings = uploaded_settings(client, output_id, request)

    assert '<metadata key="top_shell_layers" value="4"/>' in settings
    assert 'value="8"' not in settings
    assert '<metadata key="wall_loops" value="3"/>' in settings


@respx.mock
def test_a_colour_the_output_does_not_print_sets_nothing(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """A template's settings never refuse a run: a colour no slot is sets nothing."""
    output_id = prepared(client, model)
    declare(client, paths, model, output_id, colour="#123456")

    settings = uploaded_settings(client, output_id, body())

    assert "top_shell_layers" not in settings


@pytest.mark.parametrize("reader", [local, remote])
def test_the_naming_carries_them_as_the_outputs_slots(
    client: TestClient, model: str, paths: DataPaths, reader: Any
) -> None:
    output_id = prepared(client, model)
    declare(client, paths, model, output_id)
    meta = state(client).outputs.get(output_id)

    naming = asyncio.run(reader(client).naming(meta))

    assert naming.part_settings == {1: PartSettings(top_shell_layers=8, wall_loops=3)}


@respx.mock
def test_the_background_slices_copy_takes_them(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#5649: the dialog's background slice uploads the copy a run would."""
    output_id = prepared(client, model)
    declare(client, paths, model, output_id)
    upload = upload_route()
    run_routes()
    Job().routes()
    sliced_routes()

    start_slice(client, output_id, body())

    settings = copy_settings(upload)
    assert '<metadata key="top_shell_layers" value="8"/>' in settings
    assert '<metadata key="wall_loops" value="3"/>' in settings


@pytest.mark.requires_git
def test_the_quotes_copy_takes_them(bambuddy: TestClient) -> None:
    """#5649: the slice quote prices the copy a run would print."""
    shop = Shop(bambuddy)
    output_id = shop.item["output_id"]
    declare(bambuddy, state(bambuddy).paths, SLUG, output_id)
    upload = upload_route(QUOTE_COPY)
    BambuddySlices({1: sliced_plate(1, 3600, [12.5, 3.0])})

    op = shop.quote()

    assert op["status"] == "succeeded", op
    settings = copy_settings(upload)
    assert '<metadata key="top_shell_layers" value="8"/>' in settings
    assert '<metadata key="wall_loops" value="3"/>' in settings


@respx.mock
def test_a_refused_model_json_refuses_the_background_slice_and_the_check_says_why(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#5649: a slice with none of the template's settings would show a print the run
    then refuses (409), so the slice refuses it too, and the dialog's check names it."""
    output_id = prepared(client, model)
    misdeclare(paths, model)
    upload = upload_route()
    routes()
    preset_values({})

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=body())
    started = client.post(
        f"/api/v1/print/outputs/{output_id}/preview-slice", json=body(), headers=press()
    )

    assert check.status_code == 200, check.text
    assert "lable_color" in check.json()["errors"][0]
    # The route answers once the operation is recorded (`answers_running`); its run
    # refuses.
    assert started.status_code == 202, started.text
    op = follow_operation(client, started.json(), timeout=30, poll=0.1)
    assert op["status"] == "failed", op
    assert op["error"]["status"] == 409, op
    assert "lable_color" in op["error"]["detail"], op
    assert not upload.called


@pytest.mark.requires_git
def test_a_refused_model_json_refuses_the_quote(bambuddy: TestClient) -> None:
    """#5649: never a price for a print the run refuses."""
    shop = Shop(bambuddy)
    misdeclare(state(bambuddy).paths, SLUG)
    upload = upload_route(QUOTE_COPY)
    slices = BambuddySlices({1: sliced_plate(1, 3600, [12.5, 3.0])})

    started = shop.post()

    assert started.status_code == 202, started.text
    op = follow_operation(bambuddy, started.json(), timeout=120, poll=0.2)
    assert op["status"] == "failed", op
    assert op["error"]["status"] == 409, op
    assert "lable_color" in op["error"]["detail"], op
    assert not upload.called
    assert not slices.posted.called
