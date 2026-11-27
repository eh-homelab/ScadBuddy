"""The phone tag routes (#7273): `POST /tags/reads`, `POST /tags/backups`,
`GET /tags/{id}/backups`, `POST /spools/{id}/tag-records`, `POST /spools/{id}/tag-written`,
`POST /tags/{id}/backups/{id}/restore` and the agent's NFC jobs (`/nfc-jobs`)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response

from tests.api.test_filament import SPOOLS, _location, _material_id

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
    # A 4-byte UID is stored in the AMS's form, `<uid>00000100` (#8042).
    assert got.json()["next"] == "bind_or_create"
    assert got.json()["tag"]["tag_uid"] == "04A2B3C400000100"


def test_a_read_of_a_place_label_answers_open_code_and_the_place(client: TestClient) -> None:
    place = _location(client, "Shelf 9")
    href = f"https://scadbuddy.example/x/L-{place['number']}?from=label"
    record = {"record_type": "url", "data_hex": href.encode().hex()}
    got = client.post(READS, json={"uid": "04:a2:b3:c4", "records": [record]})
    assert got.status_code == 200, got.text
    assert got.json()["next"] == "open_code"
    assert got.json()["tag"]["tag_uid"] == "04A2B3C400000100"
    assert got.json()["resolved"] == {
        "kind": "location",
        "id": place["id"],
        "code": f"L-{place['number']}",
        "name": "Shelf 9",
        "archived": False,
    }


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
        json={"uid": "04aa", "records": [], "reason": "agent_write", "job_id": _armed(client)},
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
        json={"uid": uid, "records": list(records), "backup_id": _fresh(client, uid, records)},
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


def test_tag_records_of_an_unknown_spool_are_404_before_the_backup_is_checked(
    client: TestClient,
) -> None:
    got = client.post(
        f"{SPOOLS_URL}/{uuid4()}/tag-records",
        json={"uid": "04BB", "records": [], "backup_id": str(uuid4())},
    )
    assert got.status_code == 404, got.text


def test_tag_written_of_an_unknown_spool_is_404_before_the_backup_is_checked(
    client: TestClient,
) -> None:
    got = client.post(
        f"{SPOOLS_URL}/{uuid4()}/tag-written",
        json={"uid": "04BB", "backup_id": str(uuid4()), "sent": [], "read_back": []},
    )
    assert got.status_code == 404, got.text


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


def test_a_backup_cannot_be_replayed_for_a_second_spool(client: TestClient) -> None:
    first, second = _spool(client), _spool(client)
    sent, backup = _write(client, first["id"])
    got = client.post(
        f"{SPOOLS_URL}/{first['id']}/tag-written",
        json={"uid": "04AA", "backup_id": backup, "sent": sent, "read_back": sent},
    )
    assert got.status_code == 200, got.text
    # The tag now holds `sent`, which the first backup does not.
    again = client.post(
        f"{SPOOLS_URL}/{second['id']}/tag-records",
        json={"uid": "04AA", "records": sent, "backup_id": backup},
    )
    assert again.status_code == 409 and again.json()["code"] == "backup_mismatch"


def test_a_write_in_the_other_format_is_a_sent_mismatch(client: TestClient) -> None:
    spool = _spool(client)
    client.put("/api/v1/settings", json={"filament_tag_format": "opentag3d"})
    other = _records(client, spool["id"]).json()["records"]
    client.put("/api/v1/settings", json={"filament_tag_format": "openspool"})
    got = client.post(
        f"{SPOOLS_URL}/{spool['id']}/tag-written",
        json={"uid": "04AA", "backup_id": _fresh(client), "sent": other, "read_back": other},
    )
    assert got.status_code == 409 and got.json()["code"] == "sent_mismatch"
    assert client.get(f"{SPOOLS_URL}/{spool['id']}").json()["tag"] is None


def test_an_opentag3d_write_marks_its_origin_and_kind(client: TestClient) -> None:
    spool = _spool(client)
    client.put("/api/v1/settings", json={"filament_tag_format": "opentag3d"})
    sent, backup = _write(client, spool["id"])
    got = client.post(
        f"{SPOOLS_URL}/{spool['id']}/tag-written",
        json={"uid": "04AA", "backup_id": backup, "sent": sent, "read_back": sent},
    )
    assert got.status_code == 200, got.text
    assert got.json()["tag"]["kind"] == "ntag_opentag"
    detail = client.get(f"{SPOOLS_URL}/{spool['id']}").json()
    assert detail["data_origin"] == "opentag3d" and detail["tag_encoded_at"] is not None


@pytest.mark.parametrize("reason", ["restore", "agent_write"])
def test_a_backup_of_another_reason_covers_no_spool_write(client: TestClient, reason: str) -> None:
    spool = _spool(client)
    sent, _ = _write(client, spool["id"])
    job = {"job_id": _armed(client)} if reason == "agent_write" else {}
    made = client.post(BACKUPS, json={"uid": "04AA", "records": [], "reason": reason, **job})
    assert made.status_code == 201, made.text
    backup = made.json()["backup_id"]
    records = client.post(
        f"{SPOOLS_URL}/{spool['id']}/tag-records",
        json={"uid": "04AA", "records": [], "backup_id": backup},
    )
    assert records.status_code == 409 and records.json()["code"] == "backup_mismatch"
    written = client.post(
        f"{SPOOLS_URL}/{spool['id']}/tag-written",
        json={"uid": "04AA", "backup_id": backup, "sent": sent, "read_back": sent},
    )
    assert written.status_code == 409 and written.json()["code"] == "backup_mismatch"


def test_a_restore_refuses_records_that_are_not_the_fresh_backups(client: TestClient) -> None:
    old = _fresh(client, records=[_REC])
    tag_id = client.post(READS, json={"uid": "04AA", "records": []}).json()["tag"]["id"]
    fresh = _fresh(client, reason="restore")
    got = client.post(
        f"{TAGS}/{tag_id}/backups/{old}/restore",
        json={"uid": "04AA", "records": [_REC], "backup_id": fresh},
    )
    assert got.status_code == 409 and got.json()["code"] == "backup_mismatch"


JOBS = "/api/v1/filament/nfc-jobs"
_OTHER = {"record_type": "mime", "media_type": "application/json", "data_hex": "5b5d"}


def _armed(client: TestClient, kind: str = "write") -> str:
    body: dict[str, object] = {"kind": kind}
    if kind == "write":
        body["records"] = [_REC]
    made = client.post(JOBS, json=body)
    assert made.status_code == 201, made.text
    job_id: str = made.json()["id"]
    claim = None if kind == "read" else {"records_sha256": _sha256(made.json()["records"])}
    claimed = client.post(f"{JOBS}/{job_id}/claim", json=claim)
    assert claimed.status_code == 200, claimed.text
    return job_id


def _sha256(records: list[dict[str, Any]]) -> str:
    """#7273 R22: the hash the phone page sends, of the records as the job's GET gives them."""
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _agent_backup(client: TestClient, job_id: str) -> str:
    made = client.post(
        BACKUPS,
        json={"uid": "04AA", "records": [_OTHER], "reason": "agent_write", "job_id": job_id},
    )
    assert made.status_code == 201, made.text
    backup: str = made.json()["backup_id"]
    return backup


def test_a_read_job_runs_through_the_read_function(client: TestClient) -> None:
    job = client.post(JOBS, json={"kind": "read"}).json()
    assert job["state"] == "pending" and job["records"] is None and job["created_by"]
    pending = client.get(JOBS, params={"state": "pending"}).json()
    assert [j["id"] for j in pending] == [job["id"]]
    armed = client.post(f"{JOBS}/{job['id']}/claim").json()
    assert armed["state"] == "armed" and armed["armed_by"]
    assert client.get(JOBS, params={"state": "pending"}).json() == []
    done = client.post(f"{JOBS}/{job['id']}/result", json={"uid": "04:aa", "records": [_REC]})
    assert done.status_code == 200, done.text
    result = done.json()["result"]
    assert done.json()["state"] == "done" and result["next"] == "bind_or_create"
    assert result["uid"] == "04AA" and result["records"][0]["data_hex"] == "7b7d"
    assert result["tag"]["last_seen_source"] == "agent"
    assert client.get(f"{JOBS}/{job['id']}").json() == done.json()


def test_pending_jobs_come_oldest_first(client: TestClient) -> None:
    made = [client.post(JOBS, json={"kind": "read"}).json()["id"] for _ in range(3)]
    assert [j["id"] for j in client.get(JOBS, params={"state": "pending"}).json()] == made


def test_the_job_list_takes_only_the_pending_state(client: TestClient) -> None:
    assert client.get(JOBS, params={"state": "armed"}).status_code == 422
    assert client.get(JOBS).status_code == 422


def test_an_unknown_job_is_404(client: TestClient) -> None:
    unknown = f"{JOBS}/{uuid4()}"
    assert client.get(unknown).status_code == 404
    assert client.post(f"{unknown}/claim").status_code == 404
    assert client.post(f"{unknown}/result", json={"error": "x"}).status_code == 404
    assert client.post(f"{unknown}/cancel").status_code == 404


def test_a_write_claim_carries_the_hash_of_the_records_shown(client: TestClient) -> None:
    # #7273 R22: a missing or wrong hash is 409 `records_mismatch`, and the job stays pending.
    job = client.post(JOBS, json={"kind": "write", "records": [_REC]}).json()
    shown = client.get(f"{JOBS}/{job['id']}").json()["records"]
    for claim in (None, {}, {"records_sha256": _sha256([_OTHER])}):
        got = client.post(f"{JOBS}/{job['id']}/claim", json=claim)
        assert got.status_code == 409 and got.json()["code"] == "records_mismatch", claim
        assert client.get(f"{JOBS}/{job['id']}").json()["state"] == "pending"
    assert client.post(f"{JOBS}/{job['id']}/claim", json={"records_sha256": "x"}).status_code == 422
    armed = client.post(f"{JOBS}/{job['id']}/claim", json={"records_sha256": _sha256(shown)})
    assert armed.status_code == 200, armed.text
    assert armed.json()["state"] == "armed"


def test_a_read_claim_with_a_records_hash_is_422(client: TestClient) -> None:
    job_id = client.post(JOBS, json={"kind": "read"}).json()["id"]
    got = client.post(f"{JOBS}/{job_id}/claim", json={"records_sha256": _sha256([])})
    assert got.status_code == 422 and got.json()["code"] == "records_sha256_not_taken"
    assert client.get(f"{JOBS}/{job_id}").json()["state"] == "pending"


def test_cancel_ends_an_open_job_and_answers_an_ended_one_as_it_is(client: TestClient) -> None:
    # #7273 M1: the agent cancels a job it stopped waiting for; no page can arm it then.
    pending = client.post(JOBS, json={"kind": "read"}).json()["id"]
    armed = _armed(client)
    for job_id in (pending, armed):
        got = client.post(f"{JOBS}/{job_id}/cancel")
        assert got.status_code == 200, got.text
        assert got.json()["state"] == "cancelled" and got.json()["result"] is None
    assert client.get(JOBS, params={"state": "pending"}).json() == []
    claim = client.post(f"{JOBS}/{pending}/claim")
    assert claim.status_code == 409 and claim.json()["code"] == "job_not_pending"
    late = client.post(f"{JOBS}/{armed}/result", json={"error": "late"})
    assert late.status_code == 409 and late.json()["code"] == "job_not_armed"
    ended = _armed(client, "read")
    failed = client.post(f"{JOBS}/{ended}/result", json={"error": "declined"}).json()
    again = client.post(f"{JOBS}/{ended}/cancel")
    assert again.status_code == 200 and again.json() == failed


def test_cancel_refuses_a_begun_write(client: TestClient) -> None:
    job_id = _armed(client)
    assert client.get(f"{JOBS}/{job_id}").json()["write_begun"] is False
    _agent_backup(client, job_id)
    assert client.get(f"{JOBS}/{job_id}").json()["write_begun"] is True
    got = client.post(f"{JOBS}/{job_id}/cancel")
    assert got.status_code == 409 and got.json()["code"] == "write_begun"
    assert client.get(f"{JOBS}/{job_id}").json()["state"] == "armed"


def test_a_write_job_needs_its_own_agent_write_backup(client: TestClient) -> None:
    job_id = _armed(client)
    spool_write = client.post(
        BACKUPS, json={"uid": "04AA", "records": [_OTHER], "reason": "spool_write"}
    ).json()["backup_id"]
    other_job = _agent_backup(client, _armed(client))
    for backup in (spool_write, other_job, None):
        got = client.post(
            f"{JOBS}/{job_id}/result",
            json={"uid": "04AA", "records": [_REC], "backup_id": backup},
        )
        assert got.status_code == 409 and got.json()["code"] == "backup_mismatch", backup
    assert client.get(f"{JOBS}/{job_id}").json()["state"] == "armed"


def test_a_write_job_is_done_only_when_the_read_back_matches(client: TestClient) -> None:
    job_id = _armed(client)
    backup = _agent_backup(client, job_id)
    done = client.post(
        f"{JOBS}/{job_id}/result",
        json={"uid": "04:aa", "records": [_REC], "backup_id": backup},
    )
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "done"
    result = done.json()["result"]
    assert result["uid"] == "04AA" and result["backup_id"] == backup
    assert [r["data_hex"] for r in result["records"]] == ["7b7d"] and "error" not in result

    other = _armed(client)
    failed = client.post(
        f"{JOBS}/{other}/result",
        json={"uid": "04AA", "records": [_OTHER], "backup_id": _agent_backup(client, other)},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["state"] == "failed"
    assert failed.json()["result"]["error"] == "the tag does not hold what was sent"
    assert failed.json()["result"]["records"][0]["data_hex"] == "5b5d"


def test_an_agent_write_backup_needs_an_armed_write_job(client: TestClient) -> None:
    pending = client.post(JOBS, json={"kind": "write", "records": [_REC]}).json()["id"]
    read = _armed(client, "read")
    for job_id in (pending, read, str(uuid4())):
        got = client.post(
            BACKUPS,
            json={"uid": "04AA", "records": [], "reason": "agent_write", "job_id": job_id},
        )
        assert got.status_code == 409 and got.json()["code"] == "job_not_armed", job_id


def _expire(pg_conninfo: str, job_id: str) -> None:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE filament_nfc_jobs SET expires_at = now() - interval '1 second' WHERE id = %s",
            (job_id,),
        )


def test_a_job_expires_after_its_ttl(client: TestClient, pg_conninfo: str) -> None:
    job = client.post(JOBS, json={"kind": "read", "ttl_s": 30}).json()
    ttl = datetime.fromisoformat(job["expires_at"]) - datetime.fromisoformat(job["created_at"])
    assert 29 <= ttl.total_seconds() <= 31
    _expire(pg_conninfo, job["id"])
    assert client.get(f"{JOBS}/{job['id']}").json()["state"] == "expired"
    assert client.get(JOBS, params={"state": "pending"}).json() == []
    got = client.post(f"{JOBS}/{job['id']}/claim")
    assert got.status_code == 409 and got.json()["code"] == "job_not_pending"


def test_an_armed_job_expires_too(client: TestClient, pg_conninfo: str) -> None:
    job_id = _armed(client)
    _expire(pg_conninfo, job_id)
    backup = client.post(
        BACKUPS, json={"uid": "04AA", "records": [], "reason": "agent_write", "job_id": job_id}
    )
    assert backup.status_code == 409 and backup.json()["code"] == "job_not_armed"
    got = client.post(f"{JOBS}/{job_id}/result", json={"error": "late"})
    assert got.status_code == 409 and got.json()["code"] == "job_not_armed"
    assert client.get(f"{JOBS}/{job_id}").json()["state"] == "expired"


def test_a_job_is_claimed_once(client: TestClient) -> None:
    job_id = _armed(client, "read")
    got = client.post(f"{JOBS}/{job_id}/claim")
    assert got.status_code == 409 and got.json()["code"] == "job_not_pending"


def test_only_an_armed_job_takes_a_result(client: TestClient) -> None:
    job_id = client.post(JOBS, json={"kind": "read"}).json()["id"]
    got = client.post(f"{JOBS}/{job_id}/result", json={"uid": "04AA", "records": []})
    assert got.status_code == 409 and got.json()["code"] == "job_not_armed"
    client.post(f"{JOBS}/{job_id}/claim")
    assert client.post(f"{JOBS}/{job_id}/result", json={"error": "x"}).status_code == 200
    again = client.post(f"{JOBS}/{job_id}/result", json={"error": "x"})
    assert again.status_code == 409 and again.json()["code"] == "job_not_armed"


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "read", "records": [_REC]},
        {"kind": "read", "records": []},
        {"kind": "write"},
        {"kind": "write", "records": []},
        {"kind": "write", "records": [{"record_type": "mime", "data_hex": "00" * 889}]},
        {"kind": "write", "records": [_REC] * 17},
        {"kind": "erase"},
        {"kind": "read", "ttl_s": 0},
        {"kind": "read", "ttl_s": 601},
    ],
)
def test_a_read_job_takes_no_records_and_a_write_job_needs_them(
    client: TestClient, body: dict[str, object]
) -> None:
    assert client.post(JOBS, json=body).status_code == 422


def test_a_failed_job_keeps_the_pages_error(client: TestClient) -> None:
    job_id = _armed(client)
    got = client.post(f"{JOBS}/{job_id}/result", json={"error": "The person declined."})
    assert got.status_code == 200, got.text
    assert got.json()["state"] == "failed"
    assert got.json()["result"] == {"error": "The person declined."}
    too_long = client.post(f"{JOBS}/{_armed(client)}/result", json={"error": "x" * 501})
    assert too_long.status_code == 422


def test_the_spoolbuddy_routes_are_gone(client: TestClient) -> None:
    spool = _spool(client)
    base = f"{SPOOLS}/{spool['id']}/ntag"
    for path in (
        "/api/v1/filament/rfid/scan",
        f"{base}/write",
        f"{base}/confirm",
        f"{base}/cancel",
    ):
        assert client.post(path, json={}).status_code in (404, 405), path


@pytest.mark.parametrize("field", ["media_type", "id", "encoding", "lang"])
def test_a_record_with_a_lone_surrogate_is_422(client: TestClient, field: str) -> None:
    # JSON can carry a lone UTF-16 surrogate, which no UTF-8 holds: it is refused at the
    # edge, never stored or hashed into a 500.
    body = '{"kind": "write", "records": [{"record_type": "text", "data_hex": "00", '
    body += f'"{field}": "a\\ud800"' + "}]}"
    got = client.post(JOBS, content=body, headers={"content-type": "application/json"})
    assert got.status_code == 422, got.text


def test_a_job_error_with_a_lone_surrogate_is_422(client: TestClient) -> None:
    got = client.post(
        f"{JOBS}/{_armed(client)}/result",
        content='{"error": "a\\ud800"}',
        headers={"content-type": "application/json"},
    )
    assert got.status_code == 422, got.text


def test_a_mifare_read_that_reaches_python_needs_core(client: TestClient) -> None:
    """#8148: core's area `filament-tag-reads` decodes a MIFARE read; Python never does."""
    block = {"index": 1, "data_hex": "4131372d503100004746413137000000"}
    body = {
        "uid": "9CC69CE3",
        "tag_type": "mifare_classic",
        "records": [],
        "mifare_blocks": [block],
    }
    got = client.post(READS, json=body)
    assert got.status_code == 503, got.text
    assert got.json()["code"] == "mifare_needs_core"
    body["mifare_blocks"] = [{"index": 64, "data_hex": "00"}]
    assert client.post(READS, json=body).status_code == 422


def test_python_checks_a_reads_photo_and_position_and_leaves_them_to_core(
    client: TestClient,
) -> None:
    body: dict[str, Any] = {
        "uid": "04:a2:b3:c4",
        "records": [],
        "image": {"key": "scan_image-" + "ab" * 32, "media_type": "image/jpeg"},
        "location": {"lat": 51.5, "lon": -0.12, "accuracy_m": 12.5, "at": "2026-11-27T04:00:00Z"},
    }
    got = client.post(READS, json=body)
    assert got.status_code == 200, got.text
    body["location"] = {"lat": 91, "lon": 0, "at": "2026-11-27T04:00:00Z"}
    assert client.post(READS, json=body).status_code == 422
