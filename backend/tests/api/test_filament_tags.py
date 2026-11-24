"""POST /filament/tags/reads (#7273 Task 1)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.api.test_filament import SPOOLS, _material_id

pytestmark = pytest.mark.requires_postgres

READS = "/api/v1/filament/tags/reads"


def _spool(client: TestClient) -> dict[str, object]:
    made = client.post(SPOOLS, json={"material_id": _material_id(client)})
    assert made.status_code == 201, made.text
    row: dict[str, object] = made.json()
    return row


def test_a_phone_read_answers_the_tag_and_next(client: TestClient) -> None:
    got = client.post(READS, json={"uid": "04:a2:b3:c4", "records": []})
    assert got.status_code == 200, got.text
    assert got.json()["next"] == "bind_or_create" and got.json()["tag"]["tag_uid"] == "04A2B3C4"


def test_a_read_binds_with_bind_spool_id(client: TestClient) -> None:
    spool = _spool(client)
    got = client.post(READS, json={"uid": "04a2", "records": [], "bind_spool_id": spool["id"]})
    assert got.status_code == 200, got.text
    assert got.json()["spool"]["id"] == spool["id"] and got.json()["next"] == "open_spool"


def test_a_bind_without_a_uid_is_422_no_uid(client: TestClient) -> None:
    got = client.post(
        READS, json={"uid": None, "records": [], "bind_spool_id": _spool(client)["id"]}
    )
    assert got.status_code == 422 and got.json()["code"] == "no_uid"


def test_a_bind_to_an_unknown_spool_is_404(client: TestClient) -> None:
    got = client.post(READS, json={"uid": "04a2", "records": [], "bind_spool_id": str(uuid4())})
    assert got.status_code == 404


def test_records_past_888_bytes_are_refused(client: TestClient) -> None:
    big = {"record_type": "mime", "media_type": "application/json", "data_hex": "00" * 889}
    assert client.post(READS, json={"uid": "04a2", "records": [big]}).status_code == 422


def test_an_unknown_spool_with_no_uid_is_422_before_404(client: TestClient) -> None:
    got = client.post(READS, json={"uid": None, "records": [], "bind_spool_id": str(uuid4())})
    assert got.status_code == 422 and got.json()["code"] == "no_uid"
