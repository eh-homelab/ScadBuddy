"""Preset writes that validate against the template as ``library`` operations (#1054)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import params as params_api
from tests.api.test_model_operations import _workflow_ids

pytestmark = [pytest.mark.requires_postgres]


def _presets(model: str) -> str:
    return f"/api/v1/models/{model}/presets"


def test_preset_writes_are_operations(client: TestClient, app: FastAPI, model: str) -> None:
    created = client.post(_presets(model), json={"name": "Wide", "params": {"width": 20}})
    assert created.status_code == 201, created.text
    preset_id = created.json()["id"]
    copied = client.post(f"{_presets(model)}/{preset_id}/duplicate", json={"name": "Copy"})
    assert copied.status_code == 201, copied.text
    assert copied.json()["params"] == {"width": 20}
    patched = client.patch(f"{_presets(model)}/{preset_id}", json={"params": {"width": 30}})
    assert patched.status_code == 200, patched.text
    assert patched.json()["params"] == {"width": 30}
    renamed = client.patch(f"{_presets(model)}/{preset_id}", json={"name": "Wider"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Wider"
    for kind in ("preset_create", "preset_duplicate", "preset_update"):
        assert _workflow_ids(app, kind), kind


def test_a_repeated_preset_create_makes_one_preset(
    client: TestClient, app: FastAPI, model: str
) -> None:
    key = uuid.uuid4().hex
    body = {"name": "Once", "params": {"width": 20}}
    first = client.post(_presets(model), json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 201, first.text
    again = client.post(_presets(model), json=body, headers={"Idempotency-Key": key})
    assert again.status_code == 201, again.text
    assert again.json() == first.json()
    names = [preset["name"] for preset in client.get(_presets(model)).json()]
    assert names == ["Once"]
    assert len(_workflow_ids(app, "preset_create")) == 1


def test_a_preset_create_without_openscad_is_still_503(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review focus 5: the operation records the 503, never a 500."""

    async def no_openscad(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("openscad")

    monkeypatch.setattr(params_api, "cached_schema", no_openscad)
    response = client.post(_presets(model), json={"name": "Wide", "params": {"width": 20}})
    assert response.status_code == 503, response.text
    assert "openscad is not available" in response.json()["detail"]
