"""Saved presets live in Postgres (#332). Without a database, a template's own presets
still list, and saving, changing or deleting one says why it cannot."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths


def test_without_a_database_the_template_s_own_presets_list_and_a_save_is_a_503(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    meta["presets"] = [{"id": "wide", "name": "Wide", "params": {"width": 40}}]
    paths.model_meta(model).write_text(json.dumps(meta), encoding="utf-8")
    url = f"/api/v1/models/{model}/presets"

    assert [p["id"] for p in client.get(url).json()] == ["template-wide"]
    saved = client.post(url, json={"name": "Big", "params": {"width": 25}})
    assert saved.status_code == 503
    assert "database" in saved.json()["detail"]
    duplicated = client.post(f"{url}/template-wide/duplicate", json={"name": "Copy"})
    assert duplicated.status_code == 503
    assert client.delete(f"{url}/{'0' * 32}").status_code == 503
    # A template's own stay read-only first: that answer does not depend on a database.
    assert client.delete(f"{url}/template-wide").status_code == 403
