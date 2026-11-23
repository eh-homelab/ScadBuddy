"""The spool fields Bambuddy keeps and the consumed counter, over HTTP (#7149, plan
`docs/superpowers/plans/2026-11-23-spool-bb-fields.md`)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.api.test_filament import SPOOLS, _spool

pytestmark = pytest.mark.requires_postgres


def test_added_full_is_set_on_create_and_patch(client: TestClient) -> None:
    """R-7149-2: a person sets it; ``null`` is not known."""
    made = _spool(client, added_full=True)
    assert made["added_full"] is True
    one = f"{SPOOLS}/{made['id']}"
    patched = client.patch(one, json={"added_full": None})
    assert patched.status_code == 200, patched.text
    assert patched.json()["added_full"] is None
    edited = client.get(f"{one}/events").json()[-1]
    assert edited["data"] == {"changes": {"added_full": [True, None]}}


def test_the_origin_and_the_encode_time_are_read_only(client: TestClient) -> None:
    """R-7149-1, R-7149-3: no person claims how a tag was written."""
    made = _spool(client)
    assert made["data_origin"] is None
    assert made["tag_encoded_at"] is None
    one = f"{SPOOLS}/{made['id']}"
    assert client.patch(one, json={"data_origin": "opentag3d"}).status_code == 422
    assert client.patch(one, json={"tag_encoded_at": "2026-11-23T00:00:00Z"}).status_code == 422


def test_a_persons_tag_bind_marks_the_spool_nfc_link(client: TestClient) -> None:
    """R-7149-1: a hand link, in Bambuddy's word for it; a bind that changes nothing
    writes nothing."""
    made = _spool(client)
    one = f"{SPOOLS}/{made['id']}"
    tag = {"tag_uid": "04AABBCC", "kind": "ntag_opentag"}
    assert client.post(f"{one}/tags", json=tag).status_code == 201
    assert client.get(one).json()["data_origin"] == "nfc_link"
    edits = [e for e in client.get(f"{one}/events").json() if e["kind"] == "edited"]
    assert [e["data"]["changes"] for e in edits] == [{"data_origin": [None, "nfc_link"]}]
    assert client.post(f"{one}/tags", json=tag).status_code == 201
    assert len([e for e in client.get(f"{one}/events").json() if e["kind"] == "edited"]) == 1


def test_a_consumed_reset_keeps_the_grams_and_starts_the_counter(client: TestClient) -> None:
    """R-7149-4."""
    made = _spool(client, grams_left=600, label_weight_g=1000)
    one = f"{SPOOLS}/{made['id']}"
    reset = client.post(f"{one}/events", json={"kind": "consumed_reset"})
    assert reset.status_code == 201, reset.text
    assert reset.json()["source"] == "person"
    assert client.get(one).json()["grams_left"] == 600
    metrics = client.get(f"{one}/history").json()["metrics"]
    assert metrics["consumed_g"] == 0
    assert metrics["consumed_since"] == reset.json()["at"]
    grams = client.post(f"{one}/events", json={"kind": "consumed_reset", "grams": 5})
    assert grams.status_code == 422


def test_the_bulk_reset_takes_an_explicit_list(client: TestClient) -> None:
    """R-7149-4: 1 to 500 ids, no "reset all"; an unknown id is named, not a failure."""
    a, b = _spool(client), _spool(client)
    gone = str(uuid4())
    url = f"{SPOOLS}/reset-consumed"
    done = client.post(url, json={"spool_ids": [a["id"], b["id"], gone]})
    assert done.status_code == 200, done.text
    assert done.json() == {"reset": 2, "not_found": [gone]}
    kinds = [e["kind"] for e in client.get(f"{SPOOLS}/{a['id']}/events").json()]
    assert kinds[-1] == "consumed_reset"
    assert client.post(url, json={"spool_ids": []}).status_code == 422
    too_many = [str(uuid4()) for _ in range(501)]
    assert client.post(url, json={"spool_ids": too_many}).status_code == 422
