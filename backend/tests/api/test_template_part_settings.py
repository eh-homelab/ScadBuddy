"""#5626: a template's settings per colour part reach the copy a run uploads, under the
request's ``part_settings``, and the output's naming carries them as its slots."""

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
from tests.api.test_filament_profiles import preset_values
from tests.api.test_internal_outputs import local, remote, state
from tests.api.test_print_filaments import prepared
from tests.api.test_print_run_choices import body, run_print
from tests.api.test_print_supports import routes
from tests.api.test_send import _uploaded_3mf, upload_route

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


def uploaded_settings(client: TestClient, output_id: str, request: dict[str, Any]) -> str:
    upload = upload_route()
    routes()
    preset_values({})
    response = run_print(client, output_id, json=request)
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        return archive.read(MODEL_SETTINGS_NAME).decode()


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
