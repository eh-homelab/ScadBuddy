"""#7150: bulk create and copy of spools (`api/filament.py`); create from slot is in
`test_tray_spools.py`."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.filament.types import PresetUnknownError
from tests.api.test_filament import (
    MISSING,
    SPOOLS,
    _location,
    _material_id,
    _spool,
    _type,
)
from tests.support.printers import bambuddy_printer

pytestmark = pytest.mark.requires_postgres

BULK = f"{SPOOLS}/bulk"
PRESET = {"printer_model": "H2C", "nozzle_diameter": "0.4", "slicer_filament": "P1"}


def _bulk(client: TestClient, body: dict[str, Any], key: str | None = "k-1") -> Any:
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(BULK, json=body, headers=headers)


def _events(client: TestClient, spool_id: str) -> list[str]:
    return [e["kind"] for e in client.get(f"{SPOOLS}/{spool_id}/events").json()]


def test_a_bulk_create_makes_n_spools_each_with_its_own_number(client: TestClient) -> None:
    body = {
        "spool": {"material_id": _material_id(client), "rgba": "#00FF00FF", "grams_left": 900},
        "quantity": 3,
    }
    made = _bulk(client, body)
    assert made.status_code == 201, made.text
    spools = made.json()
    numbers = [s["number"] for s in spools]
    assert len(set(numbers)) == 3
    assert numbers == sorted(numbers)
    assert {s["rgba"] for s in spools} == {"#00FF00FF"}
    assert {s["grams_left"] for s in spools} == {900}
    for spool in spools:
        assert _events(client, spool["id"]) == ["created"]
    assert len(client.get(SPOOLS).json()) == 3


def test_a_bulk_create_is_idempotent_by_its_key(client: TestClient) -> None:
    body = {"spool": {"material_id": _material_id(client)}, "quantity": 2}
    assert _bulk(client, body, key=None).status_code == 428
    assert client.get(SPOOLS).json() == []
    first = _bulk(client, body, key="again")
    again = _bulk(client, body, key="again")
    assert again.status_code == 201, again.text
    assert again.json() == first.json()
    assert len(client.get(SPOOLS).json()) == 2
    other = _bulk(client, body | {"quantity": 3}, key="again")
    assert other.status_code == 422, other.text
    assert len(client.get(SPOOLS).json()) == 2


def test_a_bulk_create_writes_the_place_presets_and_k_values(
    client: TestClient, pg_conninfo: str
) -> None:
    shelf = _location(client, "Shelf A")
    printer = bambuddy_printer(pg_conninfo, 1)
    k = {"printer_id": printer, "extruder": 0, "nozzle_diameter": "0.4", "k_value": "0.02"}
    body = {
        "spool": {"material_id": _material_id(client)},
        "quantity": 2,
        "location_id": shelf["id"],
        "presets": [PRESET],
        "k_values": [k],
    }
    made = _bulk(client, body)
    assert made.status_code == 201, made.text
    for spool in made.json():
        assert spool["location_id"] == shelf["id"]
        one = f"{SPOOLS}/{spool['id']}"
        assert [r["slicer_filament"] for r in client.get(f"{one}/presets").json()] == ["P1"]
        assert [r["printer_id"] for r in client.get(f"{one}/k-values").json()] == [printer]
        assert _events(client, spool["id"]).count("edited") == 2


@pytest.mark.parametrize(
    "change",
    [
        {"quantity": 0},
        {"quantity": 101},
        {"presets": [PRESET, PRESET]},
        {"location_id": MISSING},
        {"spool": {"type_id": MISSING}},
    ],
)
def test_a_refused_bulk_create_writes_nothing(client: TestClient, change: dict[str, Any]) -> None:
    body = {"spool": {"material_id": _material_id(client)}, "quantity": 2} | change
    refused = _bulk(client, body)
    assert refused.status_code == 422, refused.text
    assert client.get(SPOOLS, params={"include_archived": True}).json() == []


def test_a_bulk_create_does_not_go_into_a_tray(client: TestClient, pg_conninfo: str) -> None:
    printer = bambuddy_printer(pg_conninfo, 1)
    unit = _location(client, "AMS-A", printer_id=printer, ams_id=0)
    tray = _location(
        client, "Slot 1", printer_id=printer, ams_id=0, tray_id=0, parent_id=unit["id"]
    )
    body = {"spool": {"material_id": _material_id(client)}, "location_id": tray["id"]}
    refused = _bulk(client, body)
    assert refused.status_code == 422, refused.text
    assert "tray" in refused.json()["detail"]
    assert client.get(SPOOLS).json() == []


def test_a_copy_is_the_draft_of_a_new_spool(client: TestClient) -> None:
    shelf = _location(client, "Shelf B")
    kind = _type(client)
    source = _spool(
        client,
        type_id=kind["id"],
        material_id=None,
        colour_name="Leaf",
        cost_per_kg="19.5",
        note="from the June order",
        weight_locked=True,
        grams_left=120,
    )
    one = f"{SPOOLS}/{source['id']}"
    assert client.post(f"{one}/move", json={"location_id": shelf["id"]}).status_code == 200
    assert client.put(f"{one}/presets", json=[PRESET]).status_code == 200
    tag = {"tag_uid": "AABBCCDD", "kind": "bambu_mifare"}
    assert client.post(f"{one}/tags", json=tag).status_code == 201
    copied = client.get(f"{one}/copy")
    assert copied.status_code == 200, copied.text
    draft = copied.json()
    assert draft["spool"] == {
        "type_id": kind["id"],
        "colour_name": "Leaf",
        "cost_per_kg": "19.5000",
        "note": "from the June order",
        "weight_locked": False,
        "grams_left": None,
    } | {f: None for f in draft["spool"] if f not in _SET}
    assert draft["location_id"] == shelf["id"]
    assert [r["slicer_filament"] for r in draft["presets"]] == ["P1"]
    assert draft["k_values"] == []
    # The draft makes a spool of its own: a new number, no tag, no ledger but its creation.
    made = _bulk(client, draft | {"quantity": 1}, key="copy")
    assert made.status_code == 201, made.text
    (twin,) = made.json()
    assert twin["number"] != source["number"]
    assert twin["colour_name"] == "Leaf"
    assert twin["location_id"] == shelf["id"]
    assert twin["grams_left"] is None
    assert client.get(f"{SPOOLS}/{twin['id']}").json()["tag"] is None


_SET = {"type_id", "colour_name", "cost_per_kg", "note", "weight_locked", "grams_left"}


def test_a_copy_carries_the_spools_own_k_values(client: TestClient, pg_conninfo: str) -> None:
    printer = bambuddy_printer(pg_conninfo, 1)
    k = {
        "printer_id": printer,
        "extruder": 1,
        "nozzle_diameter": "0.4",
        "nozzle_type": "hardened_steel",
        "k_value": "0.025",
        "name": "Leaf HS",
        "cali_idx": 7,
        "setting_id": "GFSA00",
    }
    source = _spool(client, material_id=_material_id(client))
    one = f"{SPOOLS}/{source['id']}"
    assert client.put(f"{one}/k-values", json=[k]).status_code == 200
    same = {f: v for f, v in k.items() if f != "k_value"}
    draft = client.get(f"{one}/copy").json()
    (drafted,) = draft["k_values"]
    assert {f: drafted[f] for f in same} == same
    assert Decimal(drafted["k_value"]) == Decimal("0.025")
    made = _bulk(client, draft | {"quantity": 1}, key="copy-k")
    assert made.status_code == 201, made.text
    (twin,) = made.json()
    (row,) = client.get(f"{SPOOLS}/{twin['id']}/k-values").json()
    assert {f: row[f] for f in same} == same
    assert Decimal(row["k_value"]) == Decimal("0.025")


def test_a_bulk_preset_naming_no_sb_preset_is_refused_as_put_presets_refuses_it(
    client: TestClient,
) -> None:
    unknown = PRESET | {"slicer_filament": "sb-987654"}
    spool = _spool(client)
    put = client.put(f"{SPOOLS}/{spool['id']}/presets", json=[unknown])
    assert put.status_code == 422, put.text
    body = {"spool": {"material_id": _material_id(client)}, "presets": [unknown], "quantity": 2}
    made = _bulk(client, body, key="unknown-preset")
    assert made.status_code == 422, made.text
    assert made.json()["detail"] == put.json()["detail"] == str(PresetUnknownError())


def test_a_copy_leaves_added_full_behind(client: TestClient) -> None:
    """R-7150-10: whether the source was added full is a fact of its own history."""
    source = _spool(client, added_full=True)
    assert source["added_full"] is True
    draft = client.get(f"{SPOOLS}/{source['id']}/copy").json()
    assert draft["spool"]["added_full"] is None
    (twin,) = _bulk(client, draft | {"quantity": 1}, key="copy-full").json()
    assert twin["added_full"] is None


def test_a_copy_keeps_a_migrated_preset_row_migrated(client: TestClient, pg_conninfo: str) -> None:
    """R-7150-11: the copy's first type drops the copied migrated row, as the source's
    would, and keeps the person's own row."""
    source = _spool(client)
    one = f"{SPOOLS}/{source['id']}"
    own = PRESET | {"nozzle_diameter": "0.6", "slicer_filament": "P6"}
    assert client.put(f"{one}/presets", json=[PRESET, own]).status_code == 200
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "UPDATE filament_spool_presets SET migrated = true"
            " WHERE spool_id = %s AND nozzle_diameter = '0.4'",
            (source["id"],),
        )
    draft = client.get(f"{one}/copy").json()
    assert [(r["nozzle_diameter"], r["migrated"]) for r in draft["presets"]] == [
        ("0.4", True),
        ("0.6", False),
    ]
    (twin,) = _bulk(client, draft | {"quantity": 1}, key="copy-migrated").json()
    copied = f"{SPOOLS}/{twin['id']}"
    kind = _type(client)
    assert client.patch(copied, json={"type_id": kind["id"]}).status_code == 200
    left = [r for r in client.get(f"{copied}/presets").json() if r["overridden"]]
    assert [r["slicer_filament"] for r in left] == ["P6"]


def test_a_copy_of_no_spool_is_404(client: TestClient) -> None:
    assert client.get(f"{SPOOLS}/{MISSING}/copy").status_code == 404
