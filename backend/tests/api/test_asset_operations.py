"""Uploading and fetching an asset as ``library`` operations (#1054)."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from functools import partial
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import operations as operations_api
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.operations.component import OPERATIONS
from scadbuddy.workflows.commands import start_command
from tests.api.test_assets import _svg
from tests.api.test_model_operations import _state, _workflow_ids
from tests.conftest import MODEL_SLUG

pytestmark = [pytest.mark.requires_postgres]


def _upload(client: TestClient, data: bytes, key: str | None = None) -> Any:
    return client.post(
        f"/api/v1/models/{MODEL_SLUG}/assets",
        files={"file": ("a.svg", data, "image/svg+xml")},
        headers={"Idempotency-Key": key} if key else None,
    )


def test_a_repeated_asset_upload_stores_one_asset(
    client: TestClient, app: FastAPI, model: str
) -> None:
    key = uuid.uuid4().hex
    first = _upload(client, _svg(1), key)
    assert first.status_code == 201, first.text
    again = _upload(client, _svg(1), key)
    assert again.status_code == 201, again.text
    assert again.json() == first.json()
    assert len(_workflow_ids(app, "asset_upload")) == 1
    assert client.get("/api/v1/assets/usage").json()["count"] == 1


@pytest.fixture
def capped_client(settings: Settings, model: str) -> Iterator[TestClient]:
    capped = settings.model_copy(update={"asset_max_count": 1})
    with TestClient(create_app(capped)) as test_client:
        yield test_client


def test_the_quota_refusal_keeps_usage_after_a_202(
    capped_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review focus 2: a 413 that arrives after the 202 still carries `usage`."""
    assert _upload(capped_client, _svg(1)).status_code == 201
    monkeypatch.setattr(
        operations_api, "start_command", partial(start_command, deadline=timedelta(seconds=1))
    )
    store = _state(capped_client.app).assets  # type: ignore[arg-type]
    put = store.put

    def slowly(*args: Any, **kwargs: Any) -> Any:
        time.sleep(3)
        return put(*args, **kwargs)

    monkeypatch.setattr(store, "put", slowly)
    started = _upload(capped_client, _svg(2))
    assert started.status_code == 202, started.text
    op = started.json()
    deadline = time.monotonic() + 60
    while op["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.2)
        op = capped_client.get(f"/api/v1/operations/{op['id']}").json()
    assert op["status"] == "failed", op
    assert op["error"]["status"] == 413
    assert op["error"]["extensions"]["usage"]["max_count"] == 1


@pytest.mark.usefixtures("fake_dns")
@respx.mock
def test_an_asset_fetch_never_records_the_urls_query(
    client: TestClient, app: FastAPI, model: str
) -> None:
    url = "https://openmoji.org/data/color/svg/1F984.svg?token=s3cret"
    respx.get(url).mock(return_value=httpx.Response(200, content=_svg(3)))
    response = client.post(f"/api/v1/models/{MODEL_SLUG}/assets/fetch", json={"url": url})
    assert response.status_code == 201, response.text
    assert "s3cret" not in response.json()["source_url"]
    pool = _state(app).components.get(OPERATIONS).store._require()
    with pool.connection() as conn:
        rows = conn.execute("SELECT * FROM operations WHERE kind = 'asset_fetch'").fetchall()
    assert len(rows) == 1
    assert rows[0]["subject"] == "openmoji.org"
    assert "s3cret" not in json.dumps(rows[0], default=str)
