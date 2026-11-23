"""The Bambuddy sync's wire models and the one shape of a spool's ``bb_seen`` (#6265
Task 3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament.bb_sync_models import (
    BbSnapshot,
    BbWrite,
    PlanRequest,
    SyncPassResult,
    SyncPlan,
    json_form,
    seen_row,
    seen_value,
)
from scadbuddy.filament.bb_sync_plan import field_hash
from tests.bambuddy.conftest import recording

_AT = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


#: Decision 14's fields of a spool row, by BB's names, written out (not `SEEN_FIELDS`, so a
#: dropped field fails).
_DECISION_14_FIELDS = {
    "material", "subtype", "brand", "category", "effect_type", "color_name", "rgba",
    "extra_colors", "label_weight", "core_weight", "core_weight_catalog_id",
    "nozzle_temp_min", "nozzle_temp_max", "note", "cost_per_kg", "low_stock_threshold_pct",
    "material_number", "archived_at", "weight_used", "weight_locked", "last_dried_at",
    "slicer_filament", "slicer_filament_name", "suppliers", "location_id",
    # #7149 R-7149-1: the one new field Bambuddy's update takes.
    "data_origin",
}  # fmt: skip


def _seen(**fields: object) -> dict[str, object]:
    row = next(r for r in recording("inventory-spools.json") if r["id"] == 9)
    return seen_row(BbSpool.model_validate(row | fields))


def _snapshot(**lists: object) -> BbSnapshot:
    return BbSnapshot.model_validate(
        {
            "instance": "https://bambuddy.test",
            "full": True,
            "spools": [],
            "spools_complete": True,
            "suppliers": None,
            "locations": None,
            "catalog": None,
            "filaments": None,
            "read_at": _AT,
            **lists,
        }
    )


def test_a_none_list_is_not_read() -> None:
    """A ``None`` list means "not read", never "empty": a round trip keeps them apart."""
    snap = _snapshot(suppliers=[], locations=None, catalog=[], filaments=None)
    back = BbSnapshot.model_validate_json(snap.model_dump_json())
    assert back.suppliers == [] and back.catalog == []
    assert back.locations is None and back.filaments is None
    assert back == snap


def test_a_snapshot_refuses_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        _snapshot(extra=1)


def test_seen_row_keeps_only_decision_14_fields() -> None:
    """A recorded spool row reads as BB-named keys only, with its colours canonical."""
    row = next(r for r in recording("inventory-spools.json") if r["id"] == 9)
    row = row | {
        "rgba": "68819780",
        "extra_colors": "ff0000,00FF0080",
        "note": "shelf 2",
        "low_stock_threshold_pct": 15,
        "suppliers": [
            {
                "id": 3,
                "supplier_id": 7,
                "supplier_name": "Shop",
                "supplier_article_number": "A-1",
                "quoted_price_per_kg": 19.5,
                "is_purchase_source": True,
            }
        ],
    }
    seen = seen_row(BbSpool.model_validate(row))
    assert set(seen) == _DECISION_14_FIELDS
    assert {"id", "tag_uid", "tray_uuid", "k_profiles", "updated_at", "created_at"}.isdisjoint(seen)
    assert seen["rgba"] == "#68819780"
    assert seen["extra_colors"] == ["#FF0000FF", "#00FF0080"]
    assert seen["color_name"] == "Misty Blue"
    assert seen["label_weight"] == 1000
    assert seen["material_number"] is None
    assert seen["suppliers"] == [
        {
            "supplier_id": 7,
            "supplier_article_number": "A-1",
            "quoted_price_per_kg": 19.5,
            "is_purchase_source": True,
        }
    ]


def test_seen_row_is_json() -> None:
    """``bb_seen`` is stored as jsonb: every value is JSON, times as ISO text."""
    row = next(r for r in recording("inventory-spools.json") if r["id"] == 9)
    seen = seen_row(BbSpool.model_validate(row | {"last_dried_at": "2026-11-01T10:00:00"}))
    assert seen["last_dried_at"] == "2026-11-01T10:00:00Z"
    assert seen["archived_at"] is None
    assert seen["rgba"] == "#688197FF"
    assert seen["extra_colors"] is None


def test_a_pass_result_carries_its_error() -> None:
    """Decision 19, F7: a failed pass carries its ``error_label``; a pass that ran clean
    carries none."""
    failed = SyncPassResult(
        written=2,
        failed=1,
        deferred=0,
        more=False,
        instance_changed=False,
        interval_s=300.0,
        error="Bambuddy is unavailable",
    )
    assert SyncPassResult.model_validate_json(failed.model_dump_json()).error == (
        "Bambuddy is unavailable"
    )
    clean = SyncPassResult(
        written=0, failed=0, deferred=0, more=False, instance_changed=False, interval_s=300.0
    )
    assert clean.error is None and clean.skipped is None
    with pytest.raises(ValidationError):
        SyncPassResult.model_validate(clean.model_dump() | {"skipped": "bored"})


def test_a_plan_request_limit_is_bounded() -> None:
    """Decision 11: a round asks for 1 to 200 writes."""
    snap = _snapshot()
    assert PlanRequest(snapshot=snap, limit=200).limit == 200
    for limit in (0, 201):
        with pytest.raises(ValidationError):
            PlanRequest(snapshot=snap, limit=limit)


def test_a_plan_round_trips() -> None:
    write = BbWrite(
        key="spool:1:update",
        kind="spool",
        sb_id=uuid4(),
        op="probe",
        bb_id=12,
        body={},
        hashes={},
    )
    plan = SyncPlan(writes=[write], deferred=1, directions=FilamentSync(), interval_s=60.0)
    assert SyncPlan.model_validate_json(plan.model_dump_json()) == plan
    with pytest.raises(ValidationError):
        BbWrite.model_validate(write.model_dump() | {"kind": "tray"})


def test_a_decimal_price_is_the_seen_price() -> None:
    seen = _seen(cost_per_kg=19.99)["cost_per_kg"]
    sb = seen_value("cost_per_kg", Decimal("19.9900"))
    assert sb == seen == 19.99
    assert field_hash(sb) == field_hash(seen)


def test_a_drying_time_is_the_seen_text() -> None:
    seen = _seen(last_dried_at="2026-11-01T10:00:00")["last_dried_at"]
    sb = seen_value(
        "last_dried_at", datetime(2026, 11, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    )
    assert sb == seen == "2026-11-01T10:00:00Z"
    assert field_hash(sb) == field_hash(seen)


def test_an_int_weight_used_is_the_seen_float() -> None:
    seen = _seen(weight_used=0.0)["weight_used"]
    sb = seen_value("weight_used", 0)
    assert sb == seen == 0.0 and isinstance(sb, float)
    assert field_hash(sb) == field_hash(seen)
    assert seen_value("note", None) is None


def test_seen_value_is_seen_row_for_every_field() -> None:
    """One BB row and its SB-side values (SB's types: Decimal, tz-aware times, ints for
    floats, SB's colour forms): ``seen_value`` equals ``seen_row`` for all 25 fields, and
    so do the hashes."""
    ist = timezone(timedelta(hours=5, minutes=30))
    bb = next(r for r in recording("inventory-spools.json") if r["id"] == 9) | {
        "rgba": "68819780",
        "extra_colors": "ff0000,00FF0080",
        "cost_per_kg": 19.5,
        "weight_used": 12.0,
        "archived_at": "2026-11-02T08:00:00",
        "last_dried_at": "2026-11-01T10:00:00",
        "note": "shelf 2",
        "low_stock_threshold_pct": 15,
        "suppliers": [
            {
                "id": 4,
                "supplier_id": 9,
                "supplier_name": "Other",
                "supplier_article_number": None,
                "quoted_price_per_kg": 21.0,
                "is_purchase_source": False,
            },
            {
                "id": 3,
                "supplier_id": 7,
                "supplier_name": "Shop",
                "supplier_article_number": "A-1",
                "quoted_price_per_kg": 19.5,
                "is_purchase_source": True,
            },
        ],
    }
    seen = seen_row(BbSpool.model_validate(bb))
    sb = {f: bb.get(f) for f in _DECISION_14_FIELDS}
    sb |= {
        "rgba": "#68819780",
        "extra_colors": ["#FF0000FF", "#00FF0080"],
        "cost_per_kg": Decimal("19.5000"),
        "weight_used": 12,
        "archived_at": datetime(2026, 11, 2, 13, 30, tzinfo=ist),
        "last_dried_at": datetime(2026, 11, 1, 15, 30, tzinfo=ist),
        "suppliers": [
            {
                "supplier_id": 7,
                "supplier_article_number": "A-1",
                "quoted_price_per_kg": Decimal("19.50"),
                "is_purchase_source": True,
                "name": "ignored",
            },
            {
                "supplier_id": 9,
                "supplier_article_number": None,
                "quoted_price_per_kg": 21,
                "is_purchase_source": False,
            },
        ][::-1],
    }
    assert set(sb) == _DECISION_14_FIELDS == set(seen)
    for field in sorted(_DECISION_14_FIELDS):
        assert seen_value(field, sb[field]) == seen[field], field
        assert field_hash(seen_value(field, sb[field])) == field_hash(seen[field]), field


def test_json_form_turns_ids_dates_and_enums_into_json_values() -> None:
    from datetime import date
    from enum import Enum
    from uuid import UUID

    class Colour(Enum):
        RED = "red"

    uid = UUID("12345678-1234-5678-1234-567812345678")
    form = json_form({"a": [uid, date(2026, 11, 2)], "b": (Colour.RED,)})
    assert form == {"a": [str(uid), "2026-11-02"], "b": ["red"]}
    assert json.dumps(form)


def test_whole_number_amounts_are_floats_on_both_sides() -> None:
    for field in ("cost_per_kg", "label_weight", "core_weight", "weight_used"):
        sb = seen_value(field, 250)
        assert sb == 250.0 and isinstance(sb, float), field
    assert seen_value("weight_locked", True) is True
    link = {"supplier_id": 1, "quoted_price_per_kg": 21}
    price = seen_value("suppliers", [link])[0]["quoted_price_per_kg"]  # type: ignore[index]
    assert isinstance(price, float)
    seen = _seen(label_weight=1000, core_weight=250)
    assert isinstance(seen["label_weight"], float) and isinstance(seen["core_weight"], float)


def test_no_supplier_links_is_an_empty_list() -> None:
    assert seen_value("suppliers", None) == []
    assert _seen(suppliers=[])["suppliers"] == []


def test_a_link_missing_a_key_gets_it_as_none() -> None:
    sb = seen_value("suppliers", [{"supplier_id": 7}])
    assert sb == [
        {
            "supplier_id": 7,
            "supplier_article_number": None,
            "quoted_price_per_kg": None,
            "is_purchase_source": None,
        }
    ]
