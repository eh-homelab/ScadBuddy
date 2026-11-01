"""`/api/v1/native-spools` (#4643): ScadBuddy's own spool inventory, the native spool
provider's records, each write refreshing the provider's cached copy at once."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.spools.component import SPOOLS

pytestmark = pytest.mark.requires_postgres

UUID_A = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
URL = "/api/v1/native-spools"


def _cached(client: TestClient) -> dict[str, dict[str, Any]]:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    with state.components.get(SPOOLS).store._pool.connection() as conn:
        rows = conn.execute(
            "SELECT record_id, snapshot FROM spool_provider_records WHERE provider = 'native'"
        ).fetchall()
    return {row["record_id"]: row["snapshot"] for row in rows}


def test_a_spool_is_kept_and_its_record_cached_at_once(client: TestClient) -> None:
    assert client.get(URL).json() == []
    made = client.post(
        URL,
        json={
            "material": "PLA",
            "colour": "#ff0000",
            "name": "Red",
            "label_weight_g": 1000,
            "remaining_g": 800,
            "tray_uuid": UUID_A.lower(),
            "tag_uid": "0" * 16,
        },
    )
    assert made.status_code == 201, made.text
    spool = made.json()
    assert (spool["colour"], spool["tray_uuid"], spool["tag_uid"]) == ("#FF0000", UUID_A, None)
    assert _cached(client)[spool["id"]]["remaining_g"] == 800

    path = f"{URL}/{spool['id']}"
    assert client.get(path).json() == spool
    patched = client.patch(path, json={"remaining_g": 650.5, "name": None})
    assert patched.status_code == 200, patched.text
    assert (patched.json()["remaining_g"], patched.json()["name"]) == (650.5, None)
    assert _cached(client)[spool["id"]]["remaining_g"] == 650.5

    taken = client.post(URL, json={"tray_uuid": UUID_A})
    assert taken.status_code == 409, taken.text

    # The last spool archived leaves the overlay's cache too, though the Schedule's
    # refresh skips an empty shelf.
    assert client.patch(path, json={"archived": True}).json()["archived_at"] is not None
    assert client.get(URL).json() == []
    assert [s["id"] for s in client.get(URL, params={"include_archived": True}).json()] == [
        spool["id"]
    ]
    assert _cached(client) == {}

    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404
    assert client.delete(path).status_code == 404
    assert client.patch(path, json={"name": "x"}).status_code == 404


def test_what_no_spool_has_is_refused(client: TestClient) -> None:
    for body in ({"colour": "red"}, {"remaining_g": -1}, {"tray_uuid": "zz"}, {"weight": 1}):
        assert client.post(URL, json=body).status_code == 422, body
