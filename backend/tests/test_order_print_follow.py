"""``order_item_print`` follows a start that is still being accepted (#6138).

The Bambuddy path re-slices, so the print run's start outlived the accept budget and
answered 503 "still checking". The operation failed, nobody sent the start again, and
the item's claim stayed `queued` with no run for good. ``print_item`` now sends the
same start again (the same request id, so the same run) on a backoff, for a bound, and
settles the claim when the bound runs out. No database: the lifecycle is a fake that
records what ``print_item`` asks of it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from scadbuddy.api.operations import (
    STILL_ACCEPTING_PROBLEM,
    TEMPORAL_REFUSED_PROBLEM,
    TEMPORAL_UNAVAILABLE_PROBLEM,
)
from scadbuddy.api.printing import (
    STILL_CHECKING_DETAIL,
    TEMPORAL_BUSY_DETAIL,
    TEMPORAL_DOWN_DETAIL,
    TEMPORAL_REFUSED_DETAIL,
)
from scadbuddy.bambuddy.runs import PrintRun
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.lifecycle import Lifecycle
from scadbuddy.orders.models import Actor, Fulfilment
from scadbuddy.orders.operations import PRINT_TIMEOUT
from scadbuddy.orders.printing import (
    FOLLOW_MAX_WAIT_S,
    FOLLOWED_PROBLEMS,
    START_FOLLOW_S,
    print_item,
    send_until_answered,
)

BY = Actor(kind="browser")
STILL_CHECKING = ApiError(503, STILL_CHECKING_DETAIL, type_=STILL_ACCEPTING_PROBLEM)
BUSY = ApiError(503, TEMPORAL_BUSY_DETAIL, type_=TEMPORAL_UNAVAILABLE_PROBLEM)
DOWN = ApiError(503, TEMPORAL_DOWN_DETAIL, type_=TEMPORAL_UNAVAILABLE_PROBLEM)
REFUSED = ApiError(500, TEMPORAL_REFUSED_DETAIL, type_=TEMPORAL_REFUSED_PROBLEM)


def fresh(error: Exception) -> Exception:
    """A new copy of ``error`` to raise: one object raised again and again would chain
    its tracebacks and contexts into each other."""
    if isinstance(error, ApiError):
        return ApiError(error.status, error.detail, type_=error.type)
    return type(error)(*error.args)


RUN = PrintRun.model_validate(
    {
        "id": "run-1",
        "subject": "output:o",
        "status": "running",
        "created_at": "2026-11-01T00:00:00Z",
    }
)


def claim() -> Fulfilment:
    return Fulfilment(
        id="f1",
        order_item_id="i1",
        attempt=3,
        quantity=0,
        outcome="unknown",
        failure_reason=None,
        started_at=None,
        finished_at=None,
        notes="",
        accepted_at=None,
        created_by=BY,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        copies=2,
        print_request_id="k",
    )


class FakeLifecycle:
    """What ``print_item`` asks of the lifecycle, recorded."""

    def __init__(self) -> None:
        self.claim = claim()
        self.released: list[str] = []
        self.attached: list[str] = []

    def claim_print(self, *args: Any, **kwargs: Any) -> Fulfilment:
        return self.claim

    def release_print(self, claim: Fulfilment, reason: str) -> None:
        self.released.append(reason)

    def attach_print(self, claim: Fulfilment, run_id: str, printer: str | None) -> Fulfilment:
        self.attached.append(run_id)
        return claim.model_copy(update={"print_run_id": run_id})

    def follow_print(self, run_id: str) -> Fulfilment | None:
        return None


class Clock:
    """A clock that moves only when ``sleep`` or a send says so."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def answers(*steps: ApiError | PrintRun) -> Any:
    """A start that answers ``steps`` in order, then the last one for good."""
    calls: list[int] = []

    async def start(copies: int) -> PrintRun:
        calls.append(copies)
        step = steps[min(len(calls), len(steps)) - 1]
        if isinstance(step, ApiError):
            raise fresh(step)
        return step

    start.calls = calls  # type: ignore[attr-defined]
    return start


def run_print(
    lifecycle: FakeLifecycle,
    start: Any,
    existing: Any,
    *,
    follow_for: float = 60.0,
    clock: Clock | None = None,
) -> Fulfilment:
    sleeper = clock or Clock()
    return asyncio.run(
        print_item(
            cast(Lifecycle, lifecycle),
            start,
            existing,
            "o1",
            "i1",
            BY,
            request_id="k",
            sliced_sha256=None,
            sliced_blob_key=None,
            follow_for=follow_for,
            sleep=sleeper.sleep,
            clock=sleeper,
        )
    )


async def no_run(copies: int) -> PrintRun | None:
    return None


async def nothing_expected(copies: int) -> PrintRun | None:
    raise AssertionError("existing was asked, but the start answered")


def test_the_followed_problems_are_the_apis() -> None:
    assert {STILL_ACCEPTING_PROBLEM, TEMPORAL_UNAVAILABLE_PROBLEM} == FOLLOWED_PROBLEMS


def test_the_bound_and_the_last_send_fit_in_the_runs_timeout() -> None:
    # The last send waits up to `ACCEPT_BUDGET` (14 s); the attach is short.
    assert PRINT_TIMEOUT.total_seconds() > START_FOLLOW_S + 30


@pytest.mark.parametrize("answer", [STILL_CHECKING, BUSY, DOWN])
def test_a_start_that_may_still_run_is_sent_again_and_its_run_attached(answer: ApiError) -> None:
    lifecycle = FakeLifecycle()
    start = answers(answer, answer, RUN)
    clock = Clock()
    attempt = run_print(lifecycle, start, nothing_expected, clock=clock)
    assert attempt.print_run_id == RUN.id
    assert lifecycle.attached == [RUN.id]
    assert lifecycle.released == []
    # The claim's copies every time: the same request, so the same run.
    assert start.calls == [2, 2, 2]
    assert clock.slept == [1.0, 2.0]


def test_a_refused_temporal_start_is_not_sent_again_and_keeps_its_claim() -> None:
    lifecycle = FakeLifecycle()
    start = answers(REFUSED, RUN)
    with pytest.raises(ApiError) as caught:
        run_print(lifecycle, start, nothing_expected)
    assert caught.value.status == 500
    assert start.calls == [2]
    assert lifecycle.released == []


def test_a_4xx_start_still_releases_the_claim() -> None:
    lifecycle = FakeLifecycle()
    lifecycle.store = type("S", (), {"forget_projects_seen": lambda self, order_id: None})()  # type: ignore[attr-defined]
    start = answers(ApiError(409, "no spool"))
    with pytest.raises(ApiError):
        run_print(lifecycle, start, nothing_expected)
    assert start.calls == [2]
    assert lifecycle.released == ["no spool"]


def test_at_the_bound_a_run_found_is_attached() -> None:
    lifecycle = FakeLifecycle()

    async def found(copies: int) -> PrintRun | None:
        return RUN

    attempt = run_print(lifecycle, answers(STILL_CHECKING), found, follow_for=10)
    assert attempt.print_run_id == RUN.id
    assert lifecycle.released == []


def test_at_the_bound_no_run_releases_the_claim() -> None:
    lifecycle = FakeLifecycle()
    with pytest.raises(ApiError) as caught:
        run_print(lifecycle, answers(DOWN), no_run, follow_for=10)
    assert caught.value.status == 503
    assert "released" in caught.value.detail
    assert len(lifecycle.released) == 1
    assert lifecycle.attached == []


@pytest.mark.parametrize(
    "unknown",
    [
        ApiError(503, "this item's print is still being accepted; try again"),
        RuntimeError("Temporal is away"),
    ],
)
def test_at_the_bound_a_run_that_may_exist_keeps_the_claim(unknown: Exception) -> None:
    lifecycle = FakeLifecycle()

    async def cannot_tell(copies: int) -> PrintRun | None:
        raise fresh(unknown)

    with pytest.raises(ApiError) as caught:
        run_print(lifecycle, answers(STILL_CHECKING), cannot_tell, follow_for=10)
    assert caught.value.status == 503
    # Never released: a new print could then print the item a second time.
    assert lifecycle.released == []
    assert "Attempt 3 stays queued" in caught.value.detail


def test_send_until_answered_backs_off_up_to_its_bound() -> None:
    clock = Clock()
    sends: list[float] = []

    async def send() -> str:
        sends.append(clock.now)
        raise fresh(STILL_CHECKING)

    with pytest.raises(ApiError):
        asyncio.run(send_until_answered(send, follow_for=240, sleep=clock.sleep, clock=clock))
    assert clock.slept[:6] == [1, 2, 4, 8, 16, FOLLOW_MAX_WAIT_S]
    assert max(clock.slept) == FOLLOW_MAX_WAIT_S
    # It stops at the bound: the last send is at 240 s, never past it.
    assert sends[-1] == 240
    assert sum(clock.slept) == 240


def test_send_until_answered_raises_any_other_answer_at_once() -> None:
    clock = Clock()

    async def send() -> str:
        raise ApiError(502, "gateway")

    with pytest.raises(ApiError):
        asyncio.run(send_until_answered(send, follow_for=240, sleep=clock.sleep, clock=clock))
    assert clock.slept == []
