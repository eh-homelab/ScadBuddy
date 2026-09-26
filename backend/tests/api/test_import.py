from __future__ import annotations

import httpx
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.models import MAX_SOURCE_CHARS
from scadbuddy.core.paths import DataPaths

RAW_URL = "https://raw.githubusercontent.com/someone/models/main/Gridfinity%20Bin.scad"
SOURCE = "width = 10;\ncube(width);\n"

# respx only intercepts real transports, so the TestClient's own requests to the app
# pass straight through while the app's outbound fetch is mocked.


@respx.mock
def test_a_raw_url_imports_through_the_create_path(client: TestClient, paths: DataPaths) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text=SOURCE))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL})

    assert response.status_code == 201
    body = response.json()
    assert (body["slug"], body["name"]) == ("gridfinity-bin", "Gridfinity Bin")
    assert body["origin_url"] == RAW_URL
    assert client.get("/api/v1/models/gridfinity-bin/source").text == SOURCE
    # The same schema the create path stores, so the customizer opens without a rerun.
    assert paths.model_schema_cache("gridfinity-bin").exists()
    assert client.get("/api/v1/models/gridfinity-bin").json()["origin_url"] == RAW_URL


@respx.mock
def test_a_name_overrides_the_one_taken_from_the_url(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text=SOURCE))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL, "name": "My Bin"})

    assert response.status_code == 201
    assert response.json()["slug"] == "my-bin"


@respx.mock
def test_an_import_over_an_existing_slug_conflicts(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text=SOURCE))
    assert client.post("/api/v1/models/import", json={"url": RAW_URL}).status_code == 201

    response = client.post("/api/v1/models/import", json={"url": RAW_URL})

    assert response.status_code == 409


@respx.mock
def test_source_openscad_cannot_parse_is_refused_and_nothing_is_saved(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text="%%FAIL%%\n"))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL})

    assert response.status_code == 422
    assert response.json()["log_tail"] == ["ERROR: Parser error: syntax error"]
    assert client.get("/api/v1/models").json() == []


@respx.mock
def test_force_saves_an_import_that_does_not_parse(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text="%%FAIL%%\n"))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL, "force": True})

    assert response.status_code == 201


def test_a_plain_http_url_is_a_problem_422(client: TestClient) -> None:
    response = client.post("/api/v1/models/import", json={"url": "http://example.com/model.scad"})

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert "https" in response.json()["detail"]


def test_a_makerworld_url_is_a_problem_422_that_says_what_to_do(client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        response = client.post(
            "/api/v1/models/import", json={"url": "https://makerworld.com/en/models/1398039"}
        )

    assert response.status_code == 422
    assert "MakerWorld" in response.json()["detail"]
    assert not mock.calls
    assert client.get("/api/v1/models").json() == []


@respx.mock
def test_an_unreachable_url_is_a_502(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(404))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL})

    assert response.status_code == 502
    assert "404" in response.json()["detail"]


@respx.mock
def test_a_url_that_times_out_is_a_504(client: TestClient) -> None:
    respx.get(RAW_URL).mock(side_effect=httpx.ConnectTimeout("slow"))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL})

    assert response.status_code == 504


@respx.mock
def test_a_source_longer_than_a_paste_may_be_is_refused(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text="x" * (MAX_SOURCE_CHARS + 1)))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL})

    assert response.status_code == 422
    assert str(MAX_SOURCE_CHARS) in response.json()["detail"]


def test_a_model_created_any_other_way_has_no_origin(client: TestClient) -> None:
    response = client.post("/api/v1/models", json={"name": "Pasted", "source": SOURCE})

    assert response.json()["origin_url"] is None
