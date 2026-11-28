"""`InventoryCore` (#6949 K1, R-K1-4, R-K1-24): reads over Go's CRUD routes, writes as
standalone activities on the queue `inventory`, 202 when slow. Go and Temporal are fakes
(R-K1-21); the API is not built."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from temporalio.client import ActivityFailureError
from temporalio.common import ActivityIDConflictPolicy, ActivityIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter

from scadbuddy.core.problems import ApiError
from scadbuddy.inventory.core_client import Done, activity_id, operation_key
from scadbuddy.inventory.models import UseInput
from scadbuddy.operations.store import OperationAccepted
from scadbuddy.orders.models import Actor
from tests.support.inventory_core import HANG, FakeTemporal, core, go_answer

ROOT = Path(__file__).resolve().parents[3]
#: The payload Python sends for an activity's input, which Go's `TestThePythonInputDecodes`
#: reads with the SDK's own converter (`activities/wire_test.go`).
PYTHON_INPUT = ROOT / "tools/go/core/internal/inventory/activities/testdata/python_input.json"

BY = Actor(kind="browser", principal="p1")
ITEM = "00000000-0000-4000-8000-000000000001"
LOT = UUID("00000000-0000-4000-8000-000000000002")
PROBLEM = {
    "type": "https://scadbuddy.dev/problems/inventory/step_not_applicable",
    "title": "Conflict",
    "status": 409,
    "detail": "the item's class has no receive step: x",
    "code": "step_not_applicable",
    "class": {"nature": "service"},
    "step": "receive",
}


async def test_a_write_starts_its_activity_on_the_inventory_queue() -> None:
    temporal = FakeTemporal(answers=[{"status": 201, "body": {"id": "x"}}])

    done = await core(temporal).write(
        "ReceiveLot",
        key="browser:p1:k1",
        by=BY,
        subject=f"item:{ITEM}",
        params={"item_id": ITEM},
        body={"qty": "2"},
    )

    assert done == Done({"id": "x"}, 201)
    [start] = temporal.starts
    assert start.name == "inventory.ReceiveLot"
    assert start.arg == {
        "key": "browser:p1:k1",
        "actor": {"kind": "browser", "principal": "p1", "session": None, "run_id": None},
        "params": {"item_id": ITEM},
        "body": {"qty": "2"},
    }
    options = start.options
    assert options["id"] == "inventory.ReceiveLot:browser:p1:k1"
    assert options["task_queue"] == "inventory"
    assert options["id_conflict_policy"] == ActivityIDConflictPolicy.USE_EXISTING
    assert options["id_reuse_policy"] == ActivityIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
    assert options["schedule_to_close_timeout"] == timedelta(hours=24)
    assert options["start_to_close_timeout"] == timedelta(seconds=30)
    assert options["retry_policy"].maximum_interval == timedelta(seconds=30)
    assert options["summary"] == f"ReceiveLot item:{ITEM}"


async def test_a_go_problem_is_passed_through() -> None:
    temporal = FakeTemporal(answers=[{"status": 409, "body": PROBLEM}])

    with pytest.raises(ApiError) as refused:
        await core(temporal).write("Take", key="k", by=BY, subject="item:x", body={"qty": "1"})

    error = refused.value
    assert (error.status, error.type, error.detail) == (409, PROBLEM["type"], PROBLEM["detail"])
    assert error.extensions == {
        "code": "step_not_applicable",
        "class": {"nature": "service"},
        "step": "receive",
    }


async def test_a_slow_write_answers_202_and_a_repeat_202_again() -> None:
    temporal = FakeTemporal(answers=[HANG, HANG])
    inventory = core(temporal)

    first = await inventory.write("Take", key="browser:p1:k", by=BY, subject="item:x")
    again = await inventory.write("Take", key="browser:p1:k", by=BY, subject="item:x")

    for accepted in (first, again):
        assert isinstance(accepted, OperationAccepted)
        assert (accepted.id, accepted.kind, accepted.subject, accepted.status) == (
            "browser:p1:k",
            "inventory.Take",
            "item:x",
            "running",
        )
    assert [s.options["id"] for s in temporal.starts] == ["inventory.Take:browser:p1:k"] * 2
    assert [s.arg["key"] for s in temporal.starts] == ["browser:p1:k"] * 2


async def test_one_key_on_two_writes_starts_two_activities_and_go_refuses_the_second() -> None:
    """The activity id names the activity and the key, so a key reused on another write never
    attaches to the first (Use Existing attaches by id). Both runs carry the same `key`, so
    Go's `crud_requests` ledger answers the second 422 idempotency-key-reused."""
    reused = {
        "type": "https://scadbuddy.dev/problems/crud/idempotency-key-reused",
        "title": "Unprocessable Content",
        "status": 422,
        "detail": "the key was used for another request",
    }
    temporal = FakeTemporal(
        answers=[{"status": 201, "body": {"id": "lot"}}, {"status": 422, "body": reused}]
    )
    inventory = core(temporal)

    done = await inventory.write("ReceiveLot", key="browser:p1:k", by=BY, subject="item:x")
    with pytest.raises(ApiError) as refused:
        await inventory.write("Take", key="browser:p1:k", by=BY, subject="item:x")

    assert done == Done({"id": "lot"}, 201)
    assert [s.options["id"] for s in temporal.starts] == [
        "inventory.ReceiveLot:browser:p1:k",
        "inventory.Take:browser:p1:k",
    ]
    assert [s.arg["key"] for s in temporal.starts] == ["browser:p1:k"] * 2
    assert refused.value.status == 422
    assert refused.value.type == reused["type"]


async def test_a_done_write_answers_the_stored_result() -> None:
    temporal = FakeTemporal(done={"inventory.ReceiveLot:k": {"status": 201, "body": {"id": "lot"}}})

    done = await core(temporal).write("ReceiveLot", key="k", by=BY, subject="item:x")

    assert done == Done({"id": "lot"}, 201)
    assert temporal.handles == ["inventory.ReceiveLot:k"]


async def test_a_done_write_still_running_answers_202_repeated() -> None:
    temporal = FakeTemporal(done={"inventory.ReceiveLot:k": HANG})

    accepted = await core(temporal).write("ReceiveLot", key="k", by=BY, subject="item:x")

    assert isinstance(accepted, OperationAccepted)
    assert accepted.repeated is True
    assert accepted.id == "k"


async def test_a_failed_activity_runs_again() -> None:
    """Allow Duplicate Failed Only: a key whose run failed is not in `done`, so Temporal
    takes the start, and the route answers the new run."""
    temporal = FakeTemporal(answers=[{"status": 200, "body": {"moves": []}}])

    done = await core(temporal).write("Take", key="k", by=BY, subject="item:x")

    assert done == Done({"moves": []}, 200)
    assert temporal.handles == []


async def test_an_activity_that_failed_for_good_is_a_500() -> None:
    temporal = FakeTemporal(answers=[ActivityFailureError(cause=RuntimeError("bad actor"))])

    with pytest.raises(ApiError) as failed:
        await core(temporal).write("Take", key="k", by=BY, subject="item:x")

    assert failed.value.status == 500
    assert failed.value.extensions["code"] == "inventory_activity_failed"


async def test_temporal_out_of_reach_is_a_503() -> None:
    with pytest.raises(ApiError) as down:
        await core(FakeTemporal(refuse=True)).write("Take", key="k", by=BY, subject="item:x")

    assert down.value.status == 503
    assert down.value.extensions["code"] == "inventory_unavailable"


async def test_a_read_answers_gos_json() -> None:
    seen: list[httpx.Request] = []

    def reads(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return go_answer(200, {"kinds": []})

    got = await core(reads=reads).read("/kinds", params={"archived": True, "q": None})

    assert got == {"kinds": []}
    [request] = seen
    assert request.url.path == "/api/v1/internal/crud/inventory/kinds"
    assert dict(request.url.params) == {"archived": "true"}


async def test_a_read_refusal_is_passed_through() -> None:
    def reads(request: httpx.Request) -> httpx.Response:
        return go_answer(
            404,
            {
                "type": "https://scadbuddy.dev/problems/inventory/item_not_found",
                "title": "Not Found",
                "status": 404,
                "detail": "no item 'x'",
                "code": "item_not_found",
            },
        )

    with pytest.raises(ApiError) as missing:
        await core(reads=reads).read(f"/items/{ITEM}")

    assert (missing.value.status, missing.value.extensions) == (404, {"code": "item_not_found"})


async def test_go_out_of_reach_is_a_503() -> None:
    def reads(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ApiError) as down:
        await core(reads=reads).read("/kinds")

    assert down.value.status == 503
    assert down.value.extensions["code"] == "inventory_unavailable"


async def test_no_core_url_is_a_503() -> None:
    with pytest.raises(ApiError) as down:
        await core(url=None).read("/kinds")

    assert down.value.status == 503
    assert down.value.extensions["code"] == "inventory_unavailable"


def test_a_persons_key_is_scoped_to_the_actor() -> None:
    first = operation_key(Actor(kind="browser", principal="p1"), "k")
    other = operation_key(Actor(kind="browser", principal="p2"), "k")
    agent = operation_key(Actor(kind="agent", principal="p1"), "k")

    assert (first, other, agent) == ("browser:p1:k", "browser:p2:k", "agent:p1:k")
    assert operation_key(Actor(kind="browser"), "k") == "browser::k"
    made = operation_key(BY, None)
    assert made.startswith("req:") and made != operation_key(BY, None)


async def test_the_python_input_reads_in_go() -> None:
    """The input as the pydantic converter sends it: Go's `TestThePythonInputDecodes`
    decodes this same file into `activities.Input` (params a map of strings, the actor
    and the body raw JSON). `SCADBUDDY_UPDATE_GOLDEN=1` writes it again."""
    temporal = FakeTemporal(answers=[{"status": 200, "body": {"moves": []}}])
    body = UseInput(qty=Decimal("2.5"), lot_id=LOT, reason="fit a hotend")
    await core(temporal).write(
        "Take",
        key="browser:p1:k1",
        by=BY,
        subject=f"item:{ITEM}",
        params={"item_id": ITEM},
        body=body,
    )
    [payload] = await pydantic_data_converter.encode([temporal.starts[0].arg])
    data = payload.data.decode()
    got = {"metadata": {k: v.decode() for k, v in sorted(payload.metadata.items())}, "data": data}

    if os.environ.get("SCADBUDDY_UPDATE_GOLDEN") == "1":
        PYTHON_INPUT.write_text(json.dumps(got, indent=2) + "\n", encoding="utf-8")
    assert got == json.loads(PYTHON_INPUT.read_text(encoding="utf-8"))
    assert json.loads(data) == {
        "key": "browser:p1:k1",
        "actor": {"kind": "browser", "principal": "p1", "session": None, "run_id": None},
        "params": {"item_id": ITEM},
        "body": {"qty": "2.5", "lot_id": str(LOT), "reason": "fit a hotend"},
    }


GO_ACTIVITY_IDS = (
    Path(__file__).resolve().parents[3]
    / "tools/go/core/internal/inventory/store/testdata/activity_ids.json"
)


def test_the_activity_id_is_gos() -> None:
    """The ids Go's `store.ActivityID` asks Temporal for (its test reads the same file)."""
    cases = json.loads(GO_ACTIVITY_IDS.read_text())["cases"]
    assert cases
    for case in cases:
        name = case["kind"].removeprefix("inventory.")
        assert activity_id(name, case["key"]) == case["id"], case
