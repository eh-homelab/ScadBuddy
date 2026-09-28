from __future__ import annotations

import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import AppState
from scadbuddy.api.limits import MAX_TEXT_BODY_BYTES
from scadbuddy.api.metrics import refresh_asset_metrics
from scadbuddy.core.metrics import Metrics
from scadbuddy.library.assets import AssetStore
from tests.api.conftest import wait_for_job


def test_an_upload_store_without_a_database_keeps_the_last_gauges(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Its usage is rows (#591): with no pool it cannot be read, which costs the
    scrape nothing, as a store outage does not."""
    metrics = Metrics()
    metrics.assets_stored.set(7)
    state = cast(AppState, SimpleNamespace(assets=AssetStore(tmp_path), metrics=metrics))
    refresh_asset_metrics(state)
    assert "scadbuddy_assets_stored 7.0" in metrics.exposition().decode()
    assert "could not read the upload store's usage" in caplog.text


def test_metrics_are_served_as_prometheus_text(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'scadbuddy_build_info{revision="unknown",version="dev"} 1.0' in response.text
    assert "scadbuddy_render_queue_depth 0.0" in response.text


def test_counters_alerts_read_with_increase_start_at_zero(client: TestClient) -> None:
    """eh-homelab/clusters alerts on `increase()` of these. Over a series whose
    first sample is already 1, `increase()` sees nothing, so the first 503 (or
    the first store error) after a start would never page. They must be exported
    at 0 before anything happens."""
    text = client.get("/metrics").text
    assert "scadbuddy_render_jobs_rejected_total 0.0" in text
    assert 'scadbuddy_render_store_errors_total{operation="read"} 0.0' in text
    assert 'scadbuddy_render_jobs_finished_total{outcome="done"} 0.0' in text


def _metrics_once(client: TestClient, line: str) -> str:
    """/metrics as soon as it carries ``line``. A job's final state is written
    (store.finish, in a worker thread) a moment BEFORE the worker counts it back on
    the event loop, so a status poll can see the job settled while a scrape taken
    in that instant does not yet count it. Prometheus scrapes are eventually
    consistent anyway; the test waits out the gap instead of racing it."""
    text = ""
    for _ in range(200):
        text = str(client.get("/metrics").text)
        if line in text:
            return text
        time.sleep(0.01)
    return text


def test_metrics_count_renders(client: TestClient, model: str) -> None:
    accepted = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    wait_for_job(client, accepted.json()["job_id"])

    done = 'scadbuddy_render_jobs_finished_total{outcome="done"} 1.0'
    text = _metrics_once(client, done)
    assert "scadbuddy_render_jobs_submitted_total 1.0" in text
    assert done in text


def test_http_requests_are_labelled_by_route_template(client: TestClient, model: str) -> None:
    client.get(f"/api/v1/models/{model}")
    client.get("/definitely/not/a/route")

    text = client.get("/metrics").text
    # `/models/{slug}` as its router declares it, `/api/v1/…` where routers are flattened.
    assert re.search(
        r'scadbuddy_http_requests_total\{method="GET",route="(/api/v1)?/models/\{slug\}",'
        r'status="200"\} 1\.0',
        text,
    )
    assert 'route="other",status="404"' in text
    # Never the concrete path: a label per model would be a series per model.
    assert f'route="/api/v1/models/{model}"' not in text


def test_metrics_are_not_in_the_api_schema(client: TestClient) -> None:
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


def test_a_body_refused_on_its_size_is_counted(client: TestClient) -> None:
    """The gate answers a 413 without calling inward, so the counter must wrap it."""
    refused = client.post(
        "/api/v1/models",
        content=b"x" * (MAX_TEXT_BODY_BYTES + 1),
        headers={"Content-Type": "text/plain", "X-Model-Name": "Huge"},
    )
    assert refused.status_code == 413

    text = client.get("/metrics").text
    assert 'method="POST",route="other",status="413"' in text
