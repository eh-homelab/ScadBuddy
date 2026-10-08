"""The print worker's reads of an output (#1060): `LocalOutputs` on the API's volume, the
internal routes that serve them, and `RemoteOutputs` through those routes."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.output_reader import LocalOutputs, OutputReader, RemoteOutputs
from scadbuddy.library.outputs import MODEL_NAME, OutputNotFoundError
from tests.api.test_send import make_output

UNKNOWN = "f" * 32


def state(client: TestClient) -> AppState:
    app_state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return app_state


def local(client: TestClient) -> LocalOutputs:
    return LocalOutputs(state(client).outputs, state(client).catalogue)


def remote(client: TestClient) -> RemoteOutputs:
    """The remote reader, its requests answered by the app under test."""

    def answer(request: httpx.Request) -> httpx.Response:
        response = client.request(request.method, f"http://testserver{request.url.path}")
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    http = httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(answer))
    return RemoteOutputs("http://api", client=http)


def test_the_routes_answer_the_record_the_stored_3mf_and_the_naming(
    client: TestClient, model: str
) -> None:
    output_id = make_output(client, model)

    meta = client.get(f"/api/v1/internal/outputs/{output_id}")
    assert meta.status_code == 200 and meta.json()["id"] == output_id
    stored = client.get(f"/api/v1/internal/outputs/{output_id}/model.3mf")
    path = state(client).outputs.directory(output_id) / MODEL_NAME
    assert stored.status_code == 200 and stored.content == path.read_bytes()
    naming = client.get(f"/api/v1/internal/outputs/{output_id}/naming").json()
    assert naming["stem"] and naming["invalid_meta"] is None


def test_the_routes_answer_404_for_an_unknown_output(client: TestClient) -> None:
    for suffix in ("", "/model.3mf", "/naming"):
        response = client.get(f"/api/v1/internal/outputs/{UNKNOWN}{suffix}")
        assert response.status_code == 404, suffix


def test_the_routes_are_not_in_the_schema(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if "/internal/" in path]


@pytest.mark.parametrize("reader", [local, remote])
def test_both_readers_read_the_same_output(client: TestClient, model: str, reader: Any) -> None:
    output_id = make_output(client, model)
    path = state(client).outputs.directory(output_id) / MODEL_NAME
    outputs: OutputReader = reader(client)

    async def read() -> None:
        meta = await outputs.get(output_id)
        assert meta == state(client).outputs.get(output_id)
        assert await outputs.model_3mf(output_id) == path.read_bytes()
        assert (await outputs.naming(meta)) == (await local(client).naming(meta))
        with pytest.raises(OutputNotFoundError):
            await outputs.get(UNKNOWN)
        assert await outputs.model_3mf(UNKNOWN) is None

    asyncio.run(read())


@pytest.mark.parametrize("reader", [local, remote])
def test_a_model_json_that_refuses_its_print_settings_is_named_not_raised(
    client: TestClient, model: str, reader: Any
) -> None:
    output_id = make_output(client, model)
    meta = state(client).outputs.get(output_id)
    model_json = state(client).paths.model_meta(model)
    body = json.loads(model_json.read_text()) if model_json.is_file() else {}
    model_json.write_text(json.dumps({**body, "print_settings": {"not_a_setting": "1"}}))

    naming = asyncio.run(reader(client).naming(meta))

    assert naming.invalid_meta and naming.print_settings == {}
    assert naming.stem
