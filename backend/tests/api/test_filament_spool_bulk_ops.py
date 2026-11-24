"""#7151: bulk edit, bulk archive and bulk restore of spools (`api/filament.py`)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_filament import MISSING, SPOOLS, _material_id, _spool, _type

pytestmark = pytest.mark.requires_postgres

UPDATE = f"{SPOOLS}/bulk-update"
ARCHIVE = f"{SPOOLS}/bulk-archive"
RESTORE = f"{SPOOLS}/bulk-restore"


def _events(client: TestClient, spool_id: object) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = client.get(f"{SPOOLS}/{spool_id}/events").json()
    return events


def _kinds(client: TestClient, spool_id: object) -> list[str]:
    return [e["kind"] for e in _events(client, spool_id)]


def _one(client: TestClient, spool_id: object) -> dict[str, Any]:
    found: dict[str, Any] = client.get(f"{SPOOLS}/{spool_id}").json()
    return found


def test_a_bulk_edit_sets_every_field_sent_on_each_spool(client: TestClient) -> None:
    """R-7151-1: each spool takes the fields, with its own `edited` event; an id that names
    no spool is answered, not refused. A field not sent stays; `null` clears an override."""
    a = _spool(client, colour_name="Red", note="keep me", cost_per_kg="20")
    b = _spool(client, colour_name="Blue")
    patch = {"colour_name": "Teal", "cost_per_kg": None, "low_stock_pct": 15, "added_full": True}
    done = client.post(UPDATE, json={"spool_ids": [b["id"], a["id"], MISSING], "patch": patch})
    assert done.status_code == 200, done.text
    assert done.json() == {
        "changed": sorted([str(a["id"]), str(b["id"])]),
        "unchanged": [],
        "not_found": [MISSING],
    }
    for spool in (a, b):
        row = _one(client, spool["id"])
        assert row["colour_name"] == "Teal"
        assert row["cost_per_kg"] is None
        assert row["low_stock_pct"] == 15
        assert row["added_full"] is True
        assert _kinds(client, spool["id"]).count("edited") == 1
    assert _one(client, a["id"])["note"] == "keep me"


def test_a_bulk_edit_sent_again_books_nothing(client: TestClient) -> None:
    """R-7151-1: the write sets values, so a resend changes nothing and books no event."""
    a = _spool(client)
    body = {"spool_ids": [a["id"]], "patch": {"note": "same"}}
    first = client.post(UPDATE, json=body)
    again = client.post(UPDATE, json=body)
    assert first.json()["changed"] == [str(a["id"])]
    assert again.json() == {"changed": [], "unchanged": [str(a["id"])], "not_found": []}
    assert _kinds(client, a["id"]).count("edited") == 1


def test_a_bulk_edit_one_spool_refuses_changes_none(client: TestClient) -> None:
    """R-7151-1: all or none. A spool the patch refuses refuses the request, by its number;
    the other spools keep their values."""
    ok = _spool(client, note="before")
    hot = _spool(client, nozzle_temp_max=200)
    body = {"spool_ids": [ok["id"], hot["id"]], "patch": {"nozzle_temp_min": 210, "note": "x"}}
    refused = client.post(UPDATE, json=body)
    assert refused.status_code == 422, refused.text
    assert f"#{hot['number']}" in refused.json()["detail"]
    assert _one(client, ok["id"])["note"] == "before"
    assert _kinds(client, ok["id"]).count("edited") == 0


@pytest.mark.parametrize(
    "body",
    [
        {"spool_ids": [], "patch": {"note": "x"}},
        {"spool_ids": [MISSING] * 501, "patch": {"note": "x"}},
        {"spool_ids": [MISSING], "patch": {}},
        {"spool_ids": [MISSING], "patch": {"archived": True}},
        {"spool_ids": [MISSING], "patch": {"weight_locked": None}},
        {"spool_ids": [MISSING], "patch": {"grams_left": 10}},
    ],
)
def test_a_bulk_edit_refuses_a_bad_body(client: TestClient, body: dict[str, Any]) -> None:
    """R-7151-1: 1 to 500 ids, at least one field, and no `archived` (the archive routes
    do that)."""
    assert client.post(UPDATE, json=body).status_code == 422


def test_a_bulk_edit_naming_no_catalog_row_is_refused(client: TestClient) -> None:
    a = _spool(client)
    refused = client.post(UPDATE, json={"spool_ids": [a["id"]], "patch": {"brand_id": MISSING}})
    assert refused.status_code == 422, refused.text
    assert _kinds(client, a["id"]).count("edited") == 0


def test_a_bulk_edit_sets_a_first_type(client: TestClient) -> None:
    """The type is a field like any other; a spool that needed one takes it."""
    kind = _type(client)
    a = _spool(client, material_id=_material_id(client))
    done = client.post(UPDATE, json={"spool_ids": [a["id"]], "patch": {"type_id": kind["id"]}})
    assert done.status_code == 200, done.text
    assert _one(client, a["id"])["type_id"] == kind["id"]


def test_bulk_archive_and_restore_report_each_spool_once(client: TestClient) -> None:
    """R-7151-2: one request; each spool its own `archived` or `restored` event; a spool
    already so is `unchanged`, an id that names none `not_found`."""
    a, b = _spool(client), _spool(client)
    assert client.post(f"{SPOOLS}/{b['id']}/archive", json={"archived": True}).status_code == 200
    archived = client.post(ARCHIVE, json={"spool_ids": [a["id"], b["id"], MISSING]})
    assert archived.status_code == 200, archived.text
    assert archived.json() == {"changed": [a["id"]], "unchanged": [b["id"]], "not_found": [MISSING]}
    assert _one(client, a["id"])["archived_at"] is not None
    assert _kinds(client, a["id"]).count("archived") == 1
    assert _kinds(client, b["id"]).count("archived") == 1
    restored = client.post(RESTORE, json={"spool_ids": [a["id"], b["id"]]})
    assert restored.status_code == 200, restored.text
    assert restored.json() == {
        "changed": sorted([str(a["id"]), str(b["id"])]),
        "unchanged": [],
        "not_found": [],
    }
    assert _one(client, a["id"])["archived_at"] is None
    again = client.post(RESTORE, json={"spool_ids": [a["id"]]}).json()
    assert again == {"changed": [], "unchanged": [a["id"]], "not_found": []}


def test_a_bulk_archive_closes_each_tag_binding(client: TestClient) -> None:
    """As one archive does: the spool's open binding closes, with a `tag_unbound` event."""
    a = _spool(client)
    tag = {"tag_uid": "AABBCCDD", "kind": "bambu_mifare"}
    assert client.post(f"{SPOOLS}/{a['id']}/tags", json=tag).status_code == 201
    assert client.post(ARCHIVE, json={"spool_ids": [a["id"]]}).status_code == 200
    assert _one(client, a["id"])["tag"] is None
    assert "tag_unbound" in _kinds(client, a["id"])


@pytest.mark.parametrize("route", [ARCHIVE, RESTORE])
def test_a_bulk_archive_takes_1_to_500_ids(client: TestClient, route: str) -> None:
    assert client.post(route, json={"spool_ids": []}).status_code == 422
    assert client.post(route, json={"spool_ids": [MISSING] * 501}).status_code == 422
