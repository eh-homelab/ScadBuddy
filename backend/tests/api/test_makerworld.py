"""#6119 — MakerWorld through Bambuddy's own MakerWorld routes, called by the API with its
own key: the status, a link's resolve, and the import, an operation on the ``library``
worker that files the file in ScadBuddy's own library on ``library_backend`` native.
Bambuddy is respx; its shapes are ``backend/app/schemas/makerworld.py`` of
maziggy/bambuddy at v1.3.2b1-daily.20261112."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterator
from functools import partial
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Request, Response

from scadbuddy.bambuddy.errors import SCOPE_PROBLEM
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.makerworld.imports import DEFAULT_FOLDER
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.native import NativeLibrary
from tests.api.test_library_native import _native, _native_settings
from tests.api.test_model_operations import _workflow_ids
from tests.api.test_send import API, configure
from tests.support.operations import press

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

BYTES = b"PK not really a 3mf, but bytes all the same"
DIGEST = hashlib.sha256(BYTES).hexdigest()


@pytest.fixture
def native_client(settings: Settings, s3: S3Settings) -> Iterator[TestClient]:
    with TestClient(create_app(_native_settings(settings, s3))) as client:
        configure(client)
        yield client


@pytest.fixture
def bambuddy_client(client: TestClient) -> TestClient:
    configure(client)
    return client


def _imported(was_existing: bool = False, folder_id: int | None = 31) -> dict[str, Any]:
    return {
        "library_file_id": 9,
        "filename": "Benchy.3mf",
        "folder_id": folder_id,
        "profile_id": 77,
        "was_existing": was_existing,
    }


def _bambuddy_file() -> None:
    respx.get(f"{API}/library/files/9").mock(
        return_value=Response(
            200, json={"id": 9, "filename": "Benchy.3mf", "folder_id": 31, "file_hash": DIGEST}
        )
    )
    respx.get(f"{API}/library/files/9/download").mock(return_value=Response(200, content=BYTES))


@respx.mock
def test_status_is_bambuddys_read_with_scadbuddys_key(bambuddy_client: TestClient) -> None:
    route = respx.get(f"{API}/makerworld/status").mock(
        return_value=Response(
            200, json={"has_cloud_token": True, "can_download": False, "sign_in_expired": True}
        )
    )
    answer = bambuddy_client.get("/api/v1/makerworld/status")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "has_cloud_token": True,
        "can_download": False,
        "sign_in_expired": True,
    }
    assert route.calls.last.request.headers["X-API-Key"] == "s3cret"


def test_status_without_a_bambuddy_is_the_409(client: TestClient) -> None:
    answer = client.get("/api/v1/makerworld/status")
    assert answer.status_code == 409, answer.text


@respx.mock
def test_resolve_on_bambuddy_names_bambuddys_files(bambuddy_client: TestClient) -> None:
    route = respx.post(f"{API}/makerworld/resolve").mock(
        return_value=Response(
            200,
            json={
                "model_id": 123,
                "profile_id": 456,
                "design": {"title": "Benchy"},
                "instances": [{"profileId": 456}],
                "already_imported_library_ids": [9],
            },
        )
    )
    url = "https://makerworld.com/en/models/123#profileId-456"
    answer = bambuddy_client.post("/api/v1/makerworld/resolve", json={"url": url})
    assert answer.status_code == 200, answer.text
    assert json.loads(route.calls.last.request.content) == {"url": url}
    assert answer.json() == {
        "model_id": 123,
        "profile_id": 456,
        "design": {"title": "Benchy"},
        "instances": [{"profileId": 456}],
        "already_imported_bambuddy_ids": [9],
        "already_imported_library_ids": [9],
        "library": "bambuddy",
    }


@respx.mock
def test_resolve_on_native_names_the_native_files_of_the_same_bytes(
    native_client: TestClient,
) -> None:
    native: NativeLibrary = _native(native_client)
    assert native_client.portal is not None
    held = native_client.portal.call(partial(native.upload, "Benchy.3mf", io.BytesIO(BYTES), None))
    respx.post(f"{API}/makerworld/resolve").mock(
        return_value=Response(
            200,
            json={
                "model_id": 123,
                "design": {},
                "instances": [],
                "already_imported_library_ids": [9, 10],
            },
        )
    )
    _bambuddy_file()
    # A Bambuddy file gone since its import is left out, not a failure.
    respx.get(f"{API}/library/files/10").mock(
        return_value=Response(404, json={"detail": "File not found"})
    )
    answer = native_client.post(
        "/api/v1/makerworld/resolve", json={"url": "https://makerworld.com/models/123"}
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["library"] == "native"
    assert body["profile_id"] is None
    assert body["already_imported_bambuddy_ids"] == [9, 10]
    assert body["already_imported_library_ids"] == [held.id]


@respx.mock
def test_a_link_bambuddy_refuses_is_its_refusal(bambuddy_client: TestClient) -> None:
    respx.post(f"{API}/makerworld/resolve").mock(
        return_value=Response(400, json={"detail": "No registered model provider supports"})
    )
    answer = bambuddy_client.post(
        "/api/v1/makerworld/resolve", json={"url": "https://example.com/x"}
    )
    assert answer.status_code == 422, answer.text
    assert "No registered model provider" in answer.json()["detail"]


@respx.mock
def test_import_on_native_files_the_file_in_scadbuddys_library_once(
    native_client: TestClient,
) -> None:
    seen: list[dict[str, Any]] = []
    answers = iter([_imported(), _imported(was_existing=True)])

    def bambuddy_import(request: Request) -> Response:
        seen.append(json.loads(request.content))
        return Response(200, json=next(answers))

    respx.post(f"{API}/makerworld/import").mock(side_effect=bambuddy_import)
    _bambuddy_file()
    body = {"model_id": 123, "profile_id": 77, "bambuddy_folder_id": 31}
    first = native_client.post("/api/v1/makerworld/import", json=body, headers=press())
    assert first.status_code == 200, first.text
    imported = first.json()
    assert imported["library"] == "native"
    assert is_native_id(imported["library_file_id"])
    assert imported["bambuddy_library_file_id"] == 9
    assert imported["bambuddy_folder_id"] == 31
    assert imported["filename"] == "Benchy.3mf"
    assert imported["profile_id"] == 77
    assert imported["was_existing"] is False
    assert seen == [
        {"model_id": 123, "source_type": "makerworld", "profile_id": 77, "folder_id": 31}
    ]
    native = _native(native_client)
    folder = next(f for f in native_client.portal.call(native.folders) if f.name == DEFAULT_FOLDER)  # type: ignore[union-attr]
    assert imported["folder_id"] == folder.id
    row = native.rows.file(imported["library_file_id"])
    assert row is not None and row.sha256 == DIGEST
    assert _workflow_ids(native_client.app, "makerworld_import")  # type: ignore[arg-type]

    # A repeat, with a new key: Bambuddy reuses its file, and the native copy is skipped.
    again = native_client.post("/api/v1/makerworld/import", json=body, headers=press())
    assert again.status_code == 200, again.text
    assert again.json()["library_file_id"] == imported["library_file_id"]
    assert again.json()["was_existing"] is True
    assert len(native.rows.files_by_sha256([DIGEST])) == 1


@respx.mock
def test_import_on_native_into_a_named_folder(native_client: TestClient) -> None:
    native = _native(native_client)
    folder = native.rows.create_folder("Boats", None)
    respx.post(f"{API}/makerworld/import").mock(
        return_value=Response(200, json=_imported(folder_id=None))
    )
    _bambuddy_file()
    answer = native_client.post(
        "/api/v1/makerworld/import",
        json={"model_id": 123, "folder_id": folder.id},
        headers=press(),
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["folder_id"] == folder.id
    assert answer.json()["bambuddy_folder_id"] is None


@respx.mock
def test_import_into_a_native_folder_that_is_not_there_is_refused_before_bambuddy(
    native_client: TestClient,
) -> None:
    route = respx.post(f"{API}/makerworld/import")
    answer = native_client.post(
        "/api/v1/makerworld/import",
        json={"model_id": 123, "folder_id": 1000000999},
        headers=press(),
    )
    assert answer.status_code == 404, answer.text
    assert not route.called


@respx.mock
def test_import_on_bambuddy_answers_bambuddys_file(bambuddy_client: TestClient) -> None:
    respx.post(f"{API}/makerworld/import").mock(return_value=Response(200, json=_imported()))
    answer = bambuddy_client.post(
        "/api/v1/makerworld/import", json={"model_id": 123}, headers=press()
    )
    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "library": "bambuddy",
        "library_file_id": 9,
        "folder_id": 31,
        "filename": "Benchy.3mf",
        "profile_id": 77,
        "bambuddy_library_file_id": 9,
        "bambuddy_folder_id": 31,
        "was_existing": False,
    }


@respx.mock
def test_a_native_folder_on_bambuddy_is_refused_before_bambuddy(
    bambuddy_client: TestClient,
) -> None:
    route = respx.post(f"{API}/makerworld/import")
    answer = bambuddy_client.post(
        "/api/v1/makerworld/import", json={"model_id": 123, "folder_id": 5}, headers=press()
    )
    assert answer.status_code == 422, answer.text
    assert "bambuddy_folder_id" in answer.json()["detail"]
    assert not route.called


@respx.mock
def test_an_import_bambuddy_refuses_names_the_scope(bambuddy_client: TestClient) -> None:
    respx.post(f"{API}/makerworld/import").mock(
        return_value=Response(403, json={"detail": "API key lacks permission"})
    )
    answer = bambuddy_client.post(
        "/api/v1/makerworld/import", json={"model_id": 123}, headers=press()
    )
    assert answer.status_code >= 400, answer.text
    assert answer.json()["type"] == SCOPE_PROBLEM
    assert "Manage Library" in answer.json()["detail"]
