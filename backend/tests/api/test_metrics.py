from __future__ import annotations

import re

from fastapi.testclient import TestClient

from scadbuddy.api.limits import MAX_TEXT_BODY_BYTES
from tests.api.conftest import wait_for_job


def test_metrics_are_served_as_prometheus_text(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'scadbuddy_build_info{revision="unknown",version="dev"} 1.0' in response.text
    assert "scadbuddy_render_queue_depth 0.0" in response.text


def test_metrics_count_renders(client: TestClient, model: str) -> None:
    accepted = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    wait_for_job(client, accepted.json()["job_id"])

    text = client.get("/metrics").text
    assert "scadbuddy_render_jobs_submitted_total 1.0" in text
    assert 'scadbuddy_render_jobs_finished_total{outcome="done"} 1.0' in text


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
