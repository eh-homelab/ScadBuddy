"""`/api/v1/filament` history, forecast and metrics routes (#6269)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.requires_postgres

URL = "/api/v1/filament"
MISSING = "00000000-0000-4000-8000-000000000001"
PATHS = (
    "/api/v1/filament/spools/{spool_id}/history",
    "/api/v1/filament/forecast",
    "/api/v1/filament/metrics",
    "/api/v1/filament/metrics/purchasing",
)


def _spool(client: TestClient) -> str:
    material = next(
        m["id"] for m in client.get(f"{URL}/catalog/materials").json() if m["name"] == "PLA"
    )
    made = client.post(f"{URL}/spools", json={"material_id": material, "grams_left": 500})
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def test_history_route_404(client: TestClient) -> None:
    missing = client.get(f"{URL}/spools/{MISSING}/history")
    assert missing.status_code == 404, missing.text
    found = client.get(f"{URL}/spools/{_spool(client)}/history")
    assert found.status_code == 200, found.text
    body = found.json()
    assert [row["event"]["kind"] for row in body["events"]] == ["created"]
    assert body["window_days"] == 30
    assert body["currency"] == "USD"


def test_metrics_route_rejects_reversed_range(client: TestClient) -> None:
    reversed_ = client.get(f"{URL}/metrics", params={"since": "2026-11-02", "until": "2026-11-01"})
    assert reversed_.status_code == 422, reversed_.text
    same = client.get(f"{URL}/metrics", params={"since": "2026-11-01", "until": "2026-11-01"})
    assert same.status_code == 422, same.text
    wide = client.get(f"{URL}/metrics", params={"since": "2020-01-01", "until": "2026-11-01"})
    assert wide.status_code == 422, wide.text
    bad = client.get(
        f"{URL}/metrics/purchasing", params={"since": "2026-11-02", "until": "2026-11-01"}
    )
    assert bad.status_code == 422, bad.text


def test_metrics_route_defaults(client: TestClient) -> None:
    _spool(client)
    found = client.get(f"{URL}/metrics")
    assert found.status_code == 200, found.text
    body = found.json()
    today = datetime.now(UTC).date()
    assert (body["dimension"], body["bucket"]) == ("material", "month")
    assert body["until"] == today.isoformat()
    assert body["since"] == (today - timedelta(days=180)).isoformat()
    assert [(r["label"], r["spools"]) for r in body["stock"]] == [("PLA", 1)]
    purchasing = client.get(f"{URL}/metrics/purchasing")
    assert purchasing.status_code == 200, purchasing.text
    assert purchasing.json() == {"prices": [], "spend": []}
    forecast = client.get(f"{URL}/forecast")
    assert forecast.status_code == 200, forecast.text
    assert forecast.json()["window_days"] == 30
    assert len(forecast.json()["spools"]) == 1
    by_type = client.get(
        f"{URL}/metrics",
        params={"dimension": "type", "bucket": "week", "since": str(today - timedelta(days=400))},
    )
    assert by_type.status_code == 200, by_type.text
    assert by_type.json()["stock"][0]["label"] == "No type"


def test_metrics_routes_in_openapi_have_summary(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for path in PATHS:
        operation = paths[path]["get"]
        assert operation.get("summary"), path
        assert operation.get("description"), path
