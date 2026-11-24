"""POST /filament/tags/reads (#7273 Task 1)."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response

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


SPOOLS_URL = "/api/v1/filament/spools"
TAGS = "/api/v1/filament/tags"


def _fresh(
    client: TestClient,
    uid: str = "04AA",
    records: Sequence[object] = (),
    reason: str = "spool_write",
) -> str:
    got = client.post(BACKUPS, json={"uid": uid, "records": list(records), "reason": reason})
    assert got.status_code == 201, got.text
    return str(got.json()["backup_id"])


def _records(
    client: TestClient, spool_id: object, uid: str = "04AA", records: Sequence[object] = ()
) -> Response:
    got: Response = client.post(
        f"{SPOOLS_URL}/{spool_id}/tag-records",
        json={"uid": uid, "records": list(records), "backup_id": _fresh(client, uid)},
    )
    return got


def test_a_blank_tag_gets_the_default_format(client: TestClient) -> None:
    spool = _spool(client)
    got = _records(client, spool["id"])
    assert got.status_code == 200, got.text
    assert got.json()["format"] == "openspool" and len(got.json()["records"]) == 1


def test_the_default_format_follows_the_setting(client: TestClient) -> None:
    assert (
        client.put("/api/v1/settings", json={"filament_tag_format": "opentag3d"}).status_code == 200
    )
    got = _records(client, _spool(client)["id"])
    assert got.json()["format"] == "opentag3d"


def test_a_tag_keeps_its_own_format(client: TestClient) -> None:
    spool = _spool(client)
    client.put("/api/v1/settings", json={"filament_tag_format": "opentag3d"})
    held = _records(client, spool["id"]).json()["records"]
    client.put("/api/v1/settings", json={"filament_tag_format": "openspool"})
    assert _records(client, spool["id"], records=held).json()["format"] == "opentag3d"
    assert _records(client, spool["id"]).json()["format"] == "openspool"


def test_tag_records_without_a_backup_are_refused(client: TestClient) -> None:
    spool = _spool(client)
    got = client.post(
        f"{SPOOLS_URL}/{spool['id']}/tag-records", json={"uid": "04AA", "records": []}
    )
    assert got.status_code == 422


def test_tag_records_of_an_unknown_spool_are_404(client: TestClient) -> None:
    assert _records(client, uuid4()).status_code == 404


def test_tag_records_refuse_a_backup_of_another_uid(client: TestClient) -> None:
    spool = _spool(client)
    got = client.post(
        f"{SPOOLS_URL}/{spool['id']}/tag-records",
        json={"uid": "04BB", "records": [], "backup_id": _fresh(client, "04AA")},
    )
    assert got.status_code == 409 and got.json()["code"] == "backup_mismatch"


def test_tag_records_refuse_a_backup_older_than_ten_minutes(
    client: TestClient, pg_conninfo: str
) -> None:
    spool = _spool(client)
    fresh = _fresh(client)
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO filament_tag_backups (tag_id, uid, records, reason, actor, at)"
            " SELECT tag_id, uid, records, reason, actor, now() - interval '11 minutes'"
            " FROM filament_tag_backups WHERE id = %s RETURNING id",
            (fresh,),
        ).fetchone()
    assert row is not None
    got = client.post(
        f"{SPOOLS_URL}/{spool['id']}/tag-records",
        json={"uid": "04AA", "records": [], "backup_id": str(row[0])},
    )
    assert got.status_code == 409 and got.json()["code"] == "backup_mismatch"


def _write(
    client: TestClient, spool_id: object, uid: str = "04AA"
) -> tuple[list[dict[str, object]], str]:
    backup = _fresh(client, uid)
    sent = client.post(
        f"{SPOOLS_URL}/{spool_id}/tag-records",
        json={"uid": uid, "records": [], "backup_id": backup},
    ).json()["records"]
    return sent, backup


def test_a_checked_write_binds_the_tag_and_marks_the_origin(client: TestClient) -> None:
    spool = _spool(client)
    sent, backup = _write(client, spool["id"])
    got = client.post(
        f"{SPOOLS_URL}/{spool['id']}/tag-written",
        json={"uid": "04AA", "backup_id": backup, "sent": sent, "read_back": sent},
    )
    assert got.status_code == 200, got.text
    assert got.json()["spool"]["id"] == spool["id"]
    detail = client.get(f"{SPOOLS_URL}/{spool['id']}").json()
    assert detail["data_origin"] == "openspool" and detail["tag_encoded_at"] is not None
    assert got.json()["tag"]["kind"] == "ntag_openspool"


def test_a_read_back_that_differs_binds_nothing(client: TestClient) -> None:
    spool = _spool(client)
    sent, backup = _write(client, spool["id"])
    read_back = [{**sent[0], "data_hex": str(sent[0]["data_hex"])[:-2] + "00"}]
    got = client.post(
        f"{SPOOLS_URL}/{spool['id']}/tag-written",
        json={"uid": "04AA", "backup_id": backup, "sent": sent, "read_back": read_back},
    )
    assert got.status_code == 409 and got.json()["code"] == "read_back_mismatch"
    assert got.json()["detail"] == "the tag does not hold what was sent"
    assert client.get(f"{SPOOLS_URL}/{spool['id']}").json()["tag"] is None


def test_records_that_are_not_what_the_spool_gives_bind_nothing(client: TestClient) -> None:
    spool = _spool(client)
    sent, backup = _write(client, spool["id"])
    changed = client.patch(f"{SPOOLS_URL}/{spool['id']}", json={"rgba": "#FF0000FF"})
    assert changed.status_code == 200, changed.text
    got = client.post(
        f"{SPOOLS_URL}/{spool['id']}/tag-written",
        json={"uid": "04AA", "backup_id": backup, "sent": sent, "read_back": sent},
    )
    assert got.status_code == 409 and got.json()["code"] == "sent_mismatch"
    assert client.get(f"{SPOOLS_URL}/{spool['id']}").json()["tag"] is None


def test_sent_records_in_no_known_format_are_422(client: TestClient) -> None:
    spool = _spool(client)
    junk = [{"record_type": "mime", "media_type": "text/plain", "data_hex": "00"}]
    got = client.post(
        f"{SPOOLS_URL}/{spool['id']}/tag-written",
        json={"uid": "04AA", "backup_id": _fresh(client), "sent": junk, "read_back": junk},
    )
    assert got.status_code == 422 and got.json()["code"] == "unknown_format"


def test_a_write_of_an_unknown_spool_is_404(client: TestClient) -> None:
    sent, backup = _write(client, _spool(client)["id"])
    got = client.post(
        f"{SPOOLS_URL}/{uuid4()}/tag-written",
        json={"uid": "04AA", "backup_id": backup, "sent": sent, "read_back": sent},
    )
    assert got.status_code == 404


def test_a_read_back_from_another_uid_binds_nothing(client: TestClient) -> None:
    spool = _spool(client)
    sent, backup = _write(client, spool["id"])
    got = client.post(
        f"{SPOOLS_URL}/{spool['id']}/tag-written",
        json={"uid": "04BB", "backup_id": backup, "sent": sent, "read_back": sent},
    )
    assert got.status_code == 409 and got.json()["code"] == "backup_mismatch"
    assert client.get(f"{SPOOLS_URL}/{spool['id']}").json()["tag"] is None


def test_a_restore_answers_the_old_records(client: TestClient) -> None:
    old = _fresh(client, records=[_REC])
    tag_id = client.post(READS, json={"uid": "04AA", "records": []}).json()["tag"]["id"]
    fresh = _fresh(client, records=[], reason="restore")
    got = client.post(
        f"{TAGS}/{tag_id}/backups/{old}/restore",
        json={"uid": "04AA", "records": [], "backup_id": fresh},
    )
    assert got.status_code == 200, got.text
    assert got.json()["records"] == [{**_REC, "id": None, "encoding": None, "lang": None}]
    assert got.json()["format"] is None


def test_a_restore_needs_its_own_fresh_backup(client: TestClient) -> None:
    old = _fresh(client, records=[_REC])
    tag_id = client.post(READS, json={"uid": "04AA", "records": []}).json()["tag"]["id"]
    got = client.post(
        f"{TAGS}/{tag_id}/backups/{old}/restore",
        json={"uid": "04AA", "records": [], "backup_id": old},
    )
    assert got.status_code == 409 and got.json()["code"] == "backup_mismatch"


def test_a_restore_of_an_unknown_backup_or_tag_is_404(client: TestClient) -> None:
    tag_id = client.post(READS, json={"uid": "04AA", "records": []}).json()["tag"]["id"]
    fresh = _fresh(client, reason="restore")
    body = {"uid": "04AA", "records": [], "backup_id": fresh}
    assert client.post(f"{TAGS}/{tag_id}/backups/{uuid4()}/restore", json=body).status_code == 404
    old = _fresh(client, records=[_REC])
    assert client.post(f"{TAGS}/{uuid4()}/backups/{old}/restore", json=body).status_code == 404


def test_a_restore_of_another_tags_backup_is_404(client: TestClient) -> None:
    other = _fresh(client, uid="04CC", records=[_REC])
    tag_id = client.post(READS, json={"uid": "04AA", "records": []}).json()["tag"]["id"]
    fresh = _fresh(client, reason="restore")
    got = client.post(
        f"{TAGS}/{tag_id}/backups/{other}/restore",
        json={"uid": "04AA", "records": [], "backup_id": fresh},
    )
    assert got.status_code == 404
