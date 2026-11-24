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


BACKUPS = "/api/v1/filament/tags/backups"
_REC = {"record_type": "mime", "media_type": "application/json", "data_hex": "7b7d"}


def test_a_backup_answers_its_id_and_lists(client: TestClient) -> None:
    made = client.post(BACKUPS, json={"uid": "04:aa", "records": [_REC], "reason": "spool_write"})
    assert made.status_code == 201, made.text
    tag = client.post(READS, json={"uid": "04aa", "records": []}).json()["tag"]
    listed = client.get(f"/api/v1/filament/tags/{tag['id']}/backups")
    assert listed.status_code == 200, listed.text
    [row] = listed.json()
    assert row["id"] == made.json()["backup_id"] and row["uid"] == "04AA"
    assert row["records"][0]["data_hex"] == "7b7d" and row["reason"] == "spool_write"


@pytest.mark.parametrize("uid", ["", "::"])
def test_a_backup_with_no_uid_is_422_no_uid(client: TestClient, uid: str) -> None:
    got = client.post(BACKUPS, json={"uid": uid, "records": [], "reason": "spool_write"})
    assert got.status_code == 422 and got.json()["code"] == "no_uid"


def test_backups_of_an_unknown_tag_are_404(client: TestClient) -> None:
    assert client.get(f"/api/v1/filament/tags/{uuid4()}/backups").status_code == 404


def test_an_agent_write_backup_needs_a_job_id(client: TestClient) -> None:
    got = client.post(BACKUPS, json={"uid": "04aa", "records": [], "reason": "agent_write"})
    assert got.status_code == 422
    other = client.post(
        BACKUPS,
        json={"uid": "04aa", "records": [], "reason": "restore", "job_id": str(uuid4())},
    )
    assert other.status_code == 422
    ok = client.post(
        BACKUPS,
        json={"uid": "04aa", "records": [], "reason": "agent_write", "job_id": str(uuid4())},
    )
    assert ok.status_code == 201, ok.text


def test_a_backup_over_888_bytes_is_422(client: TestClient) -> None:
    half = {"record_type": "mime", "data_hex": "00" * 500}
    got = client.post(BACKUPS, json={"uid": "04aa", "records": [half, half], "reason": "restore"})
    assert got.status_code == 422


def test_backups_are_paged_by_limit_and_before(client: TestClient) -> None:
    ids = [
        client.post(BACKUPS, json={"uid": "04bb", "records": [], "reason": "spool_write"}).json()[
            "backup_id"
        ]
        for _ in range(3)
    ]
    tag = client.post(READS, json={"uid": "04bb", "records": []}).json()["tag"]
    path = f"/api/v1/filament/tags/{tag['id']}/backups"
    assert [r["id"] for r in client.get(path).json()] == ids[::-1]
    page = client.get(path, params={"limit": 1}).json()
    assert [r["id"] for r in page] == [ids[2]]
    older = client.get(path, params={"limit": 5, "before": ids[1]}).json()
    assert [r["id"] for r in older] == [ids[0]]
    assert client.get(path, params={"limit": 0}).status_code == 422
    assert client.get(path, params={"limit": 201}).status_code == 422
    assert client.get(path, params={"limit": 200}).status_code == 200
