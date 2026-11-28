from __future__ import annotations

import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.api import metrics as api_metrics
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.limits import MAX_TEXT_BODY_BYTES
from scadbuddy.api.metrics import refresh_asset_metrics
from scadbuddy.core.metrics import Metrics
from scadbuddy.filament.component import FILAMENT
from scadbuddy.filament.metrics_models import Snapshot
from scadbuddy.library.assets import AssetStore
from tests.api.conftest import wait_for_job
from tests.conftest import PgPool
from tests.support.operations import press


def test_an_upload_store_outage_keeps_the_last_gauges(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its usage is rows (#591): a database that does not answer costs the scrape
    nothing."""
    metrics = Metrics()
    metrics.assets_stored.set(7)
    assets = AssetStore(tmp_path)

    def down() -> None:
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(assets, "usage", down)
    state = cast(AppState, SimpleNamespace(assets=assets, metrics=metrics))
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
    assert 'scadbuddy_render_jobs_rejected_total{kind="render"} 0.0' in text
    assert 'scadbuddy_render_jobs_rejected_total{kind="arrange"} 0.0' in text
    assert 'scadbuddy_render_store_errors_total{operation="read"} 0.0' in text
    # Every kind and outcome, so Arrange is charted apart from renders (#2294).
    for kind in ("render", "arrange"):
        for outcome in ("done", "failed", "superseded", "cancelled"):
            finished = f'scadbuddy_render_jobs_finished_total{{kind="{kind}",outcome="{outcome}"}}'
            assert f"{finished} 0.0" in text
        assert f'scadbuddy_render_accept_pending_total{{kind="{kind}"}} 0.0' in text
        assert f'scadbuddy_render_queue_wait_seconds_count{{kind="{kind}"}} 0.0' in text
        for outcome in ("done", "failed"):
            duration = (
                f'scadbuddy_render_duration_seconds_count{{kind="{kind}",outcome="{outcome}"}}'
            )
            assert f"{duration} 0.0" in text
            latency = (
                f'scadbuddy_render_job_latency_seconds_count{{kind="{kind}",outcome="{outcome}"}}'
            )
            assert f"{latency} 0.0" in text
    # Only outcomes something emits: a series stuck at 0 reads as "never happens"
    # (review #1066 4.2). A render withdrawn ends cancelled (#4968); none has a render
    # duration, which only a render that ran to `done` or `failed` records.
    for kind in ("render", "arrange"):
        latency = f'scadbuddy_render_job_latency_seconds_count{{kind="{kind}",outcome="cancelled"}}'
        assert f"{latency} 0.0" in text
    assert 'scadbuddy_render_duration_seconds_count{kind="render",outcome="cancelled"}' not in text
    # A native slice does end cancelled (its workflow cancelled), so each of its three
    # outcomes starts at 0 (plan 2026-10-16 Task 15).
    for outcome in ("completed", "failed", "cancelled"):
        assert f'scadbuddy_native_slice_seconds_count{{outcome="{outcome}"}} 0.0' in text


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

    done = 'scadbuddy_render_jobs_finished_total{kind="render",outcome="done"} 1.0'
    text = _metrics_once(client, done)
    assert 'scadbuddy_render_jobs_submitted_total{kind="render"} 1.0' in text
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
        headers={**press(), "Content-Type": "text/plain", "X-Model-Name": "Huge"},
    )
    assert refused.status_code == 413

    text = client.get("/metrics").text
    assert 'method="POST",route="other",status="413"' in text


class _Clock:
    """The filament refresh's clock, moved by hand."""

    def __init__(self) -> None:
        self.at = 1000.0

    def __call__(self) -> float:
        return self.at


class _Snapshots:
    """A stand-in for `MetricsStore.snapshot` that counts its calls and can fail."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def __call__(self) -> Snapshot:
        self.calls += 1
        if self.fail:
            raise psycopg.OperationalError("down")
        return Snapshot(
            stock=[("PLA", "Bambu", "Shelf", 2, 1500.0, None)],
            used={},
            burn={},
            days_of_stock={},
            low_stock={},
            reorder={},
            non_print_waste={},
            currency="USD",
        )


@pytest.fixture
def filament_scrape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[_Clock, _Snapshots]:
    """The scrape's filament refresh on a hand-moved clock and a fake snapshot."""
    clock = _Clock()
    defaults = api_metrics.refresh_filament_metrics.__kwdefaults__
    assert defaults is not None
    monkeypatch.setitem(defaults, "now", clock)
    snapshots = _Snapshots()
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    monkeypatch.setattr(state.components.get(FILAMENT).metrics, "snapshot", snapshots)
    return clock, snapshots


STOCK = 'scadbuddy_filament_stock_grams{brand="Bambu",location_kind="Shelf",material="PLA"} 1500.0'


def test_a_failed_snapshot_keeps_the_scrape(
    client: TestClient,
    filament_scrape: tuple[_Clock, _Snapshots],
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, snapshots = filament_scrape
    first = client.get("/metrics")
    assert "scadbuddy_filament_metrics_up 1.0" in first.text
    assert STOCK in first.text
    clock.at += 61
    snapshots.fail = True
    second = client.get("/metrics")
    assert second.status_code == 200
    assert "scadbuddy_filament_metrics_up 0.0" in second.text
    assert STOCK in second.text
    assert "could not read the filament metrics" in caplog.text


def test_snapshot_is_cached_for_a_minute(
    client: TestClient, filament_scrape: tuple[_Clock, _Snapshots]
) -> None:
    clock, snapshots = filament_scrape
    client.get("/metrics")
    clock.at += 59
    client.get("/metrics")
    assert snapshots.calls == 1
    clock.at += 2
    client.get("/metrics")
    assert snapshots.calls == 2


@pytest.mark.requires_postgres
def test_subscriber_lag_is_exported(pg_pool: PgPool) -> None:
    """#6949 (design §4.8): how far each event subscriber's cursor is behind the log."""
    metrics = Metrics()
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE event_subscribers SET seq = (SELECT coalesce(max(seq), 0) FROM events)"
            " WHERE name = 'history'"
        )
        for n in range(3):
            conn.execute(
                "INSERT INTO events (event_id, kind, at, logged_at, payload)"
                " VALUES (%s, 'job.progress', now(), now() - interval '5 seconds', '{}')",
                (f"lag{n}",),
            )
    state = cast(
        AppState, SimpleNamespace(projection=SimpleNamespace(pool=pg_pool), metrics=metrics)
    )
    api_metrics.refresh_subscriber_metrics(state)
    registry = metrics.registry
    labels = {"subscriber": "history"}
    assert registry.get_sample_value("scadbuddy_event_subscriber_lag_events", labels) == 3
    lag = registry.get_sample_value("scadbuddy_event_subscriber_lag_seconds", labels)
    assert lag is not None and lag >= 5
    assert registry.get_sample_value("scadbuddy_event_subscriber_stopped_seq", labels) == 0
    # #8074: the subscriber's row names the event it cannot pass.
    with pg_pool.connection() as conn:
        conn.execute("UPDATE event_subscribers SET stopped_seq = 42 WHERE name = 'history'")
    api_metrics.refresh_subscriber_metrics(state)
    assert registry.get_sample_value("scadbuddy_event_subscriber_stopped_seq", labels) == 42
