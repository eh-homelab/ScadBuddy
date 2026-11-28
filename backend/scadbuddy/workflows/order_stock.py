"""The orders' stock workflows (#6949 K1 Task 16, design §6.1a "Nothing manual, no sweeps"):
a change that crosses the Python-Go line starts its workflow before it commits anything,
and only the workflow's own activities commit. Python, on the ``library`` queue, whose
worker runs in the API process (its commit and record steps write Python-owned rows).

- `FulfilOrderItem` (a fulfil, an accept that fulfils, an unfulfil): `orders_fulfil_commit`
  commits the order change (or answers another open workflow of the item to wait for,
  `orders_await_workflow`, then runs again); then Go's `inventory.Take` (or
  `inventory.GiveBack`) of each part, in part order; then `orders_parts_record`.
- `RecostLot`: `orders_recost_orders` (Go's read), Go's `inventory.Recost`, then
  `orders_recost_record`. Nothing is committed before Go answers. ``recost_orders_moved``
  runs the first two again: `RECOST_ROUNDS` times, and after that for as long as each
  refusal names an order its round did not lock (`RECOST_UNTIL_NO_NEW`), at most
  `RECOST_MAX_ROUNDS` rounds in all.
- `MergePrinter`: Go's `inventory.Repoint` of the gone printer's stock, then
  `printers_merge_commit`, the caller's whole transaction; a stock row written meanwhile
  runs both again after a sleep (1 s doubling to 60 s), with `continue_as_new` after
  `MERGE_ROUNDS` rounds. A refusal of step 2 first moves back what every round moved.

A Go step is Go's activity by its type name on the queue ``inventory``, its activity id its
idempotency key (Go's `crud_requests` key and operation row id; `store.ReadOperation`
asks Temporal by workflow id and that id). Go's refusal is an answer, never a failure. A
refusal of a Python commit step fails the workflow with a non-retryable `ApplicationError`
whose first detail is the problem (R-K1-32), and nothing was written.

On the versioned ``library`` worker (#4090) each is AUTO_UPGRADE through its ``Versioned*``
class, so it replays `tests/fixtures/order_stock_histories/`: a change to its commands goes
behind ``workflow.patched``."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError

with workflow.unsafe.imports_passed_through():
    from scadbuddy.orders.stock_activities import (
        ORDERS_AWAIT_WORKFLOW,
        ORDERS_FULFIL_COMMIT,
        ORDERS_PARTS_RECORD,
        ORDERS_RECOST_ORDERS,
        ORDERS_RECOST_RECORD,
    )
    from scadbuddy.orders.stock_start import FULFIL_ORDER_ITEM, MERGE_PRINTER, RECOST_LOT
    from scadbuddy.printers.merge_activities import PRINTERS_MERGE_COMMIT

INVENTORY_QUEUE = "inventory"
GO_SCHEDULE_TO_CLOSE = timedelta(hours=24)
STEP_TIMEOUT = timedelta(seconds=30)
RETRIES = RetryPolicy(maximum_interval=timedelta(seconds=30))
#: The wait for another open workflow of the item: as long as its Go step may take.
AWAIT_TIMEOUT = timedelta(hours=24)
AWAIT_HEARTBEAT = timedelta(seconds=30)
#: The pause before each wait from the second on: 1 s, doubling to a minute.
AWAIT_BACKOFF_FIRST = 1.0
AWAIT_BACKOFF_MAX = 60.0
#: How often a re-cost runs again after Go's ``recost_orders_moved``, whatever it names.
#: Past it, a round runs again only while Go's refusal names an order the round did not lock.
RECOST_ROUNDS = 3
#: The patch that lets a re-cost go on past `RECOST_ROUNDS` (PR #8323): a run recorded before
#: it failed after `RECOST_ROUNDS` refusals, and its history replays that way.
RECOST_UNTIL_NO_NEW = "recost-until-no-new-orders"
#: The most rounds a re-cost runs under that patch: a lot whose orders keep moving fails with
#: Go's last ``recost_orders_moved``, which says so, rather than growing one history for good.
RECOST_MAX_ROUNDS = 50
#: Rounds of a merge before it continues as new, which keeps its history small.
MERGE_ROUNDS = 100
MERGE_SLEEP_FIRST = 1.0
MERGE_SLEEP_MAX = 60.0


async def _python(name: str, arg: Any) -> Any:
    return await workflow.execute_activity(
        name, arg, start_to_close_timeout=STEP_TIMEOUT, retry_policy=RETRIES
    )


async def _go(
    input: dict[str, Any],
    name: str,
    key: str,
    *,
    params: dict[str, str] | None = None,
    body: Any = None,
) -> dict[str, Any]:
    """Go's ``inventory.<name>`` for the idempotency ``key``, on the queue the start named
    (``inventory_queue``, else `INVENTORY_QUEUE`): its answer ``{status, body}``."""
    answer: dict[str, Any] = await workflow.execute_activity(
        f"inventory.{name}",
        {"key": key, "actor": input["actor"], "params": params or {}, "body": body},
        task_queue=input.get("inventory_queue") or INVENTORY_QUEUE,
        activity_id=key,
        schedule_to_close_timeout=GO_SCHEDULE_TO_CLOSE,
        start_to_close_timeout=STEP_TIMEOUT,
        retry_policy=RETRIES,
    )
    return answer


def _refused(error: ActivityError) -> ApplicationError | None:
    """The refusal a Python commit step failed with, as the workflow's own failure."""
    cause = error.cause
    if isinstance(cause, ApplicationError) and cause.non_retryable:
        return ApplicationError(cause.message, *cause.details, type=cause.type, non_retryable=True)
    return None


def _problem(answer: dict[str, Any]) -> ApplicationError:
    """Go's refusal of a step nothing was committed before, as the workflow's failure."""
    problem = dict(answer.get("body") or {})
    problem.setdefault("status", answer["status"])
    code = str(problem.get("code") or "inventory_refused")
    return ApplicationError(
        str(problem.get("detail", code)), problem, type=code, non_retryable=True
    )


@workflow.defn(name=FULFIL_ORDER_ITEM)
class FulfilOrderItem:
    """Input ``{order_id, op: "fulfil" | "accept" | "unfulfil", lines: [{item_id, key,
    quantity?, fulfilment_id?}], actor}``; its answer is step 3's (the attempts, or the
    unfulfilled item)."""

    @workflow.run
    async def run(self, input: dict[str, Any]) -> Any:
        workflow_id = workflow.info().workflow_id
        step_input = {**input, "workflow_id": workflow_id}
        backoff = 0.0
        while True:
            try:
                step = await _python(ORDERS_FULFIL_COMMIT, step_input)
            except ActivityError as error:
                refused = _refused(error)
                if refused is None:
                    raise
                raise refused from None
            if step.get("wait_for") is None:
                break
            # From the second wait on, a pause first (1 s doubling to a minute), so a run
            # the wait cannot end is never asked for in a tight loop.
            if backoff:
                await workflow.sleep(backoff)
            backoff = min(backoff * 2 or AWAIT_BACKOFF_FIRST, AWAIT_BACKOFF_MAX)
            await workflow.execute_activity(
                ORDERS_AWAIT_WORKFLOW,
                {
                    "workflow_id": step["wait_for"],
                    "order_id": input["order_id"],
                    "inventory_queue": input.get("inventory_queue"),
                },
                start_to_close_timeout=AWAIT_TIMEOUT,
                heartbeat_timeout=AWAIT_HEARTBEAT,
                retry_policy=RETRIES,
            )
        unfulfil = input["op"] == "unfulfil"
        results: list[dict[str, Any]] = []
        for call in step["calls"]:
            answer = await _go_answer(input, call, unfulfil)
            results.append({**call, "status": answer["status"], "body": answer["body"]})
        return await _python(
            ORDERS_PARTS_RECORD,
            {
                "workflow_id": workflow_id,
                "op": input["op"],
                "order_id": input["order_id"],
                "item_ids": [line["item_id"] for line in input["lines"]],
                "attempts": step.get("attempts") or [],
                "results": results,
                "actor": input["actor"],
            },
        )


async def _go_answer(input: dict[str, Any], call: dict[str, Any], unfulfil: bool) -> dict[str, Any]:
    """Go's Take (or GiveBack) of one part. A step that failed rather than answered (past its
    day's schedule-to-close, or a non-retryable error) is an answer too, 503
    `inventory_step_failed`, so the record still writes `parts.stock_refused` and clears
    the item's `parts_workflow_id` (fix round 1). A cancel of the run is not caught.

    This 503 does not prove that Go wrote nothing (#8256). An attempt can commit in Go and
    lose its answer, and then the step can end at its schedule-to-close. The stock move is
    then made, but the record says the take was refused."""
    try:
        if unfulfil:
            return await _go(
                input,
                "GiveBack",
                call["key"],
                params={"item_id": call["item_id"]},
                body={"order_item_id": call["order_item_id"]},
            )
        return await _go(
            input,
            "Take",
            call["key"],
            params={"item_id": call["item_id"]},
            body={
                "qty": call["qty"],
                "order_id": input["order_id"],
                "order_item_id": call["order_item_id"],
            },
        )
    except ActivityError as error:
        if isinstance(error.cause, CancelledError):
            raise
        return {
            "status": 503,
            "body": {
                "code": "inventory_step_failed",
                "status": 503,
                "detail": f"the stock step failed: {error.cause or error}",
            },
        }


@workflow.defn(name=FULFIL_ORDER_ITEM, versioning_behavior=VersioningBehavior.AUTO_UPGRADE)
class VersionedFulfilOrderItem(FulfilOrderItem):
    """``FulfilOrderItem`` on the versioned ``library`` worker (#4090). AUTO_UPGRADE: a Go
    step may wait up to a day for scadbuddy-core, across any number of rollouts; Pinned, the
    old build's last replica would strand it. Its own class because an unversioned worker
    cannot complete a workflow that declares a behavior."""

    @workflow.run
    async def run(self, input: dict[str, Any]) -> Any:
        return await super().run(input)


@workflow.defn(name=RECOST_LOT)
class RecostLot:
    """Input ``{lot_id, actor}``; its answer is Go's `RecostResult`."""

    @workflow.run
    async def run(self, input: dict[str, Any]) -> Any:
        workflow_id = workflow.info().workflow_id
        also: list[str] = []
        answer: dict[str, Any] = {}
        attempt = 0
        while True:
            attempt += 1
            orders = await _python(ORDERS_RECOST_ORDERS, {"lot_id": input["lot_id"], "also": also})
            # A new key per round: the same key would answer the first refusal again.
            answer = await _go(
                input,
                "Recost",
                f"{workflow_id}:{attempt}",
                params={"lot_id": input["lot_id"]},
                body={"orders": orders},
            )
            if int(answer["status"]) < 400:
                return await _python(
                    ORDERS_RECOST_RECORD,
                    {
                        "workflow_id": workflow_id,
                        "lot_id": input["lot_id"],
                        "result": answer["body"],
                        "actor": input["actor"],
                    },
                )
            body = answer.get("body") or {}
            if body.get("code") != "recost_orders_moved":
                break
            named = set(body.get("orders") or ())
            also = sorted(set(also) | named)
            if attempt >= RECOST_ROUNDS and (
                not named - set(orders) or not workflow.patched(RECOST_UNTIL_NO_NEW)
            ):
                break
            if attempt >= RECOST_MAX_ROUNDS:
                # Only under the patch: without it the check above ended the loop at round 3.
                detail = (
                    f"the lot's moves still named new orders after {attempt} rounds;"
                    " re-cost it again once its orders settle"
                )
                answer = {**answer, "body": {**body, "detail": detail}}
                break
        raise _problem(answer)


@workflow.defn(name=RECOST_LOT, versioning_behavior=VersioningBehavior.AUTO_UPGRADE)
class VersionedRecostLot(RecostLot):
    """``RecostLot`` on the versioned ``library`` worker (#4090): AUTO_UPGRADE, as
    `VersionedFulfilOrderItem`."""

    @workflow.run
    async def run(self, input: dict[str, Any]) -> Any:
        return await super().run(input)


def _union(moved: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Every id any round's Repoint moved."""
    return {
        "moves": sorted({i for m in moved for i in m.get("moves") or ()}),
        "units": sorted({i for m in moved for i in m.get("units") or ()}),
    }


@workflow.defn(name=MERGE_PRINTER)
class MergePrinter:
    """Input ``{gone, kept, then: "link" | "ensure", args, actor}`` (and, carried through
    `continue_as_new`, ``round``, ``moved`` and ``sleep``); its answer is step 2's
    (``{row, wrote}``)."""

    @workflow.run
    async def run(self, input: dict[str, Any]) -> Any:
        gone, kept = input["gone"], input["kept"]
        chain = workflow.info().first_execution_run_id
        base = f"entity_merge:printer:{gone}:{kept}:{chain}"
        round_ = int(input.get("round", 0))
        # Only the union of what every round moved is carried through continue_as_new,
        # so the input stays as small as the stock it names.
        moved: list[dict[str, Any]] = [input["moved"]] if input.get("moved") else []
        sleep = float(input.get("sleep", MERGE_SLEEP_FIRST))
        for _ in range(MERGE_ROUNDS):
            round_ += 1
            key = f"{base}:{round_}"
            answer = await _go(
                input,
                "Repoint",
                key,
                body={"kind": "printer", "gone": gone, "kept": kept},
            )
            if int(answer["status"]) >= 400:
                raise _problem(answer)
            moved.append((answer.get("body") or {}).get("moved") or {})
            try:
                step = await _python(
                    PRINTERS_MERGE_COMMIT,
                    {
                        "gone": gone,
                        "kept": kept,
                        "then": input["then"],
                        "args": input["args"],
                        "actor": input["actor"],
                    },
                )
            except ActivityError as error:
                refused = _refused(error)
                if refused is None:
                    raise
                # The stock names the gone printer again; then the run fails (R-K1-32).
                back = await _go(
                    input,
                    "Repoint",
                    f"{key}:back",
                    body={"kind": "printer", "gone": kept, "kept": gone, "only": _union(moved)},
                )
                if int(back["status"]) >= 400:
                    # Go never refuses it (`TestABackRepointNeverRefuses`). A refusal leaves
                    # the stock on the kept printer: say so, and fail with step 2's refusal.
                    workflow.logger.warning(
                        "the back Repoint of merge %s -> %s was refused (%s): its stock"
                        " stays on %s",
                        gone,
                        kept,
                        back["status"],
                        kept,
                    )
                raise refused from None
            if not step.get("again"):
                return step
            await workflow.sleep(sleep)
            sleep = min(sleep * 2, MERGE_SLEEP_MAX)
        workflow.continue_as_new({**input, "round": round_, "moved": _union(moved), "sleep": sleep})


@workflow.defn(name=MERGE_PRINTER, versioning_behavior=VersioningBehavior.AUTO_UPGRADE)
class VersionedMergePrinter(MergePrinter):
    """``MergePrinter`` on the versioned ``library`` worker (#4090): AUTO_UPGRADE, as
    `VersionedFulfilOrderItem`."""

    @workflow.run
    async def run(self, input: dict[str, Any]) -> Any:
        return await super().run(input)
