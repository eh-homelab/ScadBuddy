"""The Bambuddy sync's wire models and the one shape of a spool's ``bb_seen`` (#6265
Task 3)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament.bb_sync_models import (
    SEEN_FIELDS,
    BbSnapshot,
    BbWrite,
    PlanRequest,
    SyncPassResult,
    SyncPlan,
    seen_row,
)
from tests.bambuddy.conftest import recording

_AT = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


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
    assert set(seen) == set(SEEN_FIELDS)
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
