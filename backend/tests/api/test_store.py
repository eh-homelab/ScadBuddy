from __future__ import annotations

from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from scadbuddy import main
from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import StoreNotReadyError


def test_store_usage_reports_the_local_backend(client: TestClient) -> None:
    body = client.get("/api/v1/store/usage").json()
    assert body["backend"] == "local"
    assert {"count", "bytes", "max_count", "max_total_bytes", "by_kind"} <= set(body)


def test_healthz_says_where_blobs_live_and_whether_workers_hold_the_full_key(
    client: TestClient,
) -> None:
    store = client.get("/healthz").json()["store"]
    assert store == {
        "backend": "local",
        "configured_backend": "local",
        "render_key_fallback": False,
        "multi_worker": False,
    }
    client.put("/api/v1/settings", json={"bambuddy_api_key": "full"})
    # the API invalidates its settings source on its own write, so this is immediate
    assert client.get("/healthz").json()["store"]["render_key_fallback"] is True


def test_a_refused_store_closes_the_settings_pool(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(**_: object) -> NoReturn:
        raise StoreNotReadyError("refused")

    monkeypatch.setattr(main, "build_store", refuse)
    app = main.create_app(settings)
    with pytest.raises(StoreNotReadyError), TestClient(app):
        pass
    assert getattr(app.state, STATE_ATTR).settings_store.pool.closed
