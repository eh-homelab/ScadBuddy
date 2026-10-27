"""The hidden routes the agent stores a session's workspace files through (#4789): it
asks for a `TransferUrl` to PUT a file's sealed bytes to, says what it PUT, and asks for
a `TransferUrl` to GET them back. It never mints a token and holds no store credential."""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.api.internal_workspace import INTERNAL_WORKSPACE
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.slicing.transfer import INTERNAL_BLOBS, WORKSPACE_PENDING_PREFIX
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import SWEPT_KINDS, BlobScope

pytestmark = pytest.mark.requires_postgres

INTERNAL = "http://scadbuddy"


@pytest.fixture
def kek_file(tmp_path: Path) -> Path:
    path = tmp_path / "kek"
    path.write_text(base64.b64encode(os.urandom(32)).decode())
    return path


def _on_s3(settings: Settings, s3: S3Settings, **update: object) -> Settings:
    return settings.model_copy(
        update={
            "store_backend": "s3",
            "store_s3_endpoint": s3.endpoint,
            "store_s3_bucket": s3.bucket,
            "store_s3_prefix": s3.prefix,
            "store_s3_access_key_id": s3.access_key_id,
            "store_s3_secret_access_key": s3.secret_access_key,
            **update,
        }
    )


@pytest.fixture
def s3_client(settings: Settings, s3: S3Settings, kek_file: Path) -> Iterator[TestClient]:
    with TestClient(
        create_app(_on_s3(settings, s3, secret_key_file=kek_file, api_internal_url=INTERNAL))
    ) as client:
        yield client


@pytest.fixture
def local_client(settings: Settings, kek_file: Path) -> Iterator[TestClient]:
    with TestClient(
        create_app(
            settings.model_copy(update={"secret_key_file": kek_file, "api_internal_url": INTERNAL})
        )
    ) as client:
        yield client


def _content(client: TestClient) -> ContentStore:
    content = getattr(client.app.state, STATE_ATTR).store.content  # type: ignore[attr-defined]
    assert isinstance(content, ContentStore)
    return content


def _upload(client: TestClient, body: bytes) -> str:
    """The key the PUT of ``body`` is stored under, through the three routes."""
    minted = client.post(f"{INTERNAL_WORKSPACE}/uploads")
    assert minted.status_code == 200, minted.text
    upload: dict[str, Any] = minted.json()
    assert upload["pending_key"].startswith(WORKSPACE_PENDING_PREFIX)
    url = upload["url"]
    assert url["url"] == f"{INTERNAL}{INTERNAL_BLOBS}/{upload['pending_key']}"
    put = client.put(urlsplit(url["url"]).path, headers=url["headers"], content=body)
    assert put.status_code == 204, put.text
    finished = client.post(
        f"{INTERNAL_WORKSPACE}/uploads/{upload['pending_key']}/finish",
        json={"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)},
    )
    assert finished.status_code == 200, finished.text
    key: str = finished.json()["key"]
    return key


def test_a_workspace_blob_round_trips_on_s3(s3_client: TestClient) -> None:
    body = os.urandom(3000)
    key = _upload(s3_client, body)
    assert key == f"workspace-{hashlib.sha256(body).hexdigest()}"
    assert s3_client.portal is not None
    stat = s3_client.portal.call(_content(s3_client).stat, key)
    assert stat is not None
    assert stat.ref.kind == "workspace"
    got = s3_client.get(f"{INTERNAL_WORKSPACE}/{key}/url")
    assert got.status_code == 200, got.text
    url = got.json()
    # A presigned S3 URL: the bytes never pass through the API.
    assert not url["url"].startswith(INTERNAL)
    response = httpx.get(url["url"], headers=url["headers"], follow_redirects=False)
    assert response.status_code == 200
    assert response.content == body


def test_workspace_blobs_are_swept_by_their_holders() -> None:
    """Held by `blob_refs` while their row stands, like a slice: never kept by grace alone."""
    assert "workspace" in SWEPT_KINDS


def test_finish_refuses_a_wrong_digest(s3_client: TestClient) -> None:
    upload = s3_client.post(f"{INTERNAL_WORKSPACE}/uploads").json()
    url = upload["url"]
    s3_client.put(urlsplit(url["url"]).path, headers=url["headers"], content=b"hello")
    finished = s3_client.post(
        f"{INTERNAL_WORKSPACE}/uploads/{upload['pending_key']}/finish",
        json={"sha256": hashlib.sha256(b"hellp").hexdigest(), "size": 5},
    )
    assert finished.status_code == 409


def test_finish_refuses_a_slice_pending_key(s3_client: TestClient) -> None:
    finished = s3_client.post(
        f"{INTERNAL_WORKSPACE}/uploads/pending-{'0' * 32}/finish",
        json={"sha256": "0" * 64, "size": 1},
    )
    assert finished.status_code == 404


def test_url_of_a_missing_or_foreign_key_is_404(s3_client: TestClient) -> None:
    assert s3_client.get(f"{INTERNAL_WORKSPACE}/workspace-{'a' * 64}/url").status_code == 404
    assert s3_client.portal is not None
    slice_ref = s3_client.portal.call(
        partial(_content(s3_client).put, "slice", b"sliced", name="x", scope=BlobScope())
    )
    foreign = f"slice-{slice_ref.sha256}"
    assert s3_client.get(f"{INTERNAL_WORKSPACE}/{foreign}/url").status_code == 404


def test_without_a_content_store_the_routes_are_404(local_client: TestClient) -> None:
    """`store_backend` local: the agent keeps the bytes in Postgres instead (#4789)."""
    response = local_client.post(f"{INTERNAL_WORKSPACE}/uploads")
    assert response.status_code == 404
    assert "store_backend" in response.json()["detail"]
    assert local_client.get(f"{INTERNAL_WORKSPACE}/workspace-{'a' * 64}/url").status_code == 404


def test_the_routes_are_hidden(s3_client: TestClient) -> None:
    paths = s3_client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if "internal" in p]


@pytest.mark.parametrize(
    ("update", "names"),
    [({"api_internal_url": None}, "SCADBUDDY_API_INTERNAL_URL"), ({}, "SCADBUDDY_SECRET_KEY_FILE")],
)
def test_on_s3_without_what_a_url_needs_the_upload_is_503(
    settings: Settings, s3: S3Settings, kek_file: Path, update: dict[str, object], names: str
) -> None:
    """Not the 404 of `local`: the store exists, so the agent refuses the write rather
    than keep its bytes in Postgres, and says why (#4789)."""
    base: dict[str, object] = (
        {"secret_key_file": kek_file, **update} if update else {"api_internal_url": INTERNAL}
    )
    with TestClient(create_app(_on_s3(settings, s3, **base))) as client:
        response = client.post(f"{INTERNAL_WORKSPACE}/uploads")
    assert response.status_code == 503
    assert names in response.json()["detail"]
