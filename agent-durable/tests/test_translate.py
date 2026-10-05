"""The plugin's live output as panel-protocol events (plan task 10, rulings 7-10). Pure."""

from __future__ import annotations

import json
from typing import Any

import pytest
from scadbuddy_durable.translate import Batch, Translator, bus_kind_of, durable_approval_id

from scadbuddy_durable.models import EXPIRED_BY

S = "11111111-2222-3333-4444-555555555555"
TIERS = {"get_model": "read", "update_source": "write", "print_output": "outward"}
BROWSER = "browser:browser"


def ev(kind: str, offset: int, **fields: Any) -> dict[str, Any]:
    return {"type": kind, "offset": offset, "at": "2026-10-05T00:00:00+00:00", **fields}


def text(offset: int, body: str, segment: int = 0, attempt: int = 1) -> dict[str, Any]:
    return ev("text", offset, text=body, segment=segment, attempt=attempt)


def p(kind: str, **fields: Any) -> dict[str, Any]:
    return {"v": 1, "type": kind, "sessionId": S, **fields}


def status(value: str) -> dict[str, Any]:
    return p("session.status", status=value)


def run(events: list[dict[str, Any]], decided: dict[str, str] | None = None) -> list[Batch]:
    t = Translator(S, TIERS)
    out = []
    for e in events:
        by = (decided or {}).get(e.get("id", "")) if e["type"] == "tool_result" else None
        out.append(t.feed(e, decided_by=by))
    return out


def flat(batches: list[Batch]) -> list[dict[str, Any]]:
    return [e for b in batches for e in b.events]


def test_durable_approval_id() -> None:
    assert durable_approval_id(S, "toolu_1") == f"durable:{S}:toolu_1"


def test_text_is_flushed_at_the_tool_call_with_offset_ids() -> None:
    batches = run(
        [
            text(1, "Hello"),
            text(2, "Looking."),
            ev("tool_call", 3, id="c1", name="get_model", input={"slug": "x"}),
            ev("tool_call", 4, id="c2", name="mystery", input={}),
        ]
    )
    assert batches[0].events == [] and batches[1].events == []
    assert flat(batches) == [
        p("assistant.text.delta", messageId=f"{S}-1", delta="Hello"),
        p("assistant.text.done", messageId=f"{S}-1"),
        p("assistant.text.delta", messageId=f"{S}-2", delta="Looking."),
        p("assistant.text.done", messageId=f"{S}-2"),
        p("tool.call", id="c1", name="get_model", input={"slug": "x"}, risk="read"),
        p("tool.call", id="c2", name="mystery", input={}, risk="outward"),
    ]
    assert all(b.status is None and not b.final for b in batches)


def test_a_retry_drops_the_older_attempts_text() -> None:
    batches = run(
        [
            text(1, "first try"),
            ev("retry", 2, segment=0, attempt=2),
            text(3, "second try", attempt=2),
            ev("done", 4, result="second try"),
        ]
    )
    assert flat(batches) == [
        p("assistant.text.delta", messageId=f"{S}-3", delta="second try"),
        p("assistant.text.done", messageId=f"{S}-3"),
        p("session.result"),
        status("idle"),
    ]
    assert batches[-1].status == "idle" and batches[-1].final


def test_approval_needed_waits() -> None:
    long_input = {"note": "x" * 600}
    [_, b] = run(
        [
            ev("tool_call", 1, id="c1", name="print_output", input=long_input),
            ev("approval_needed", 2, id="c1", name="print_output", input=long_input),
        ]
    )
    summary = f"print_output {json.dumps(long_input, separators=(',', ':'))}"
    assert b.events == [
        p(
            "approval.required",
            id=f"durable:{S}:c1",
            tool="c1",
            summary=summary[:499] + "…",
            risk="outward",
        ),
        status("waiting_approval"),
    ]
    assert len(b.events[0]["summary"]) == 500
    assert b.status == "waiting_approval" and not b.final


def needs(call: str = "c1") -> list[dict[str, Any]]:
    return [
        ev("tool_call", 1, id=call, name="print_output", input={"a": 1}),
        ev("approval_needed", 2, id=call, name="print_output", input={"a": 1}),
    ]


def test_a_result_after_approval_runs_again() -> None:
    batches = run([*needs(), ev("tool_result", 3, id="c1", name="print_output", status="done")])
    assert batches[-1].events == [p("tool.result", id="c1", ok=True, summary="done"), status("running")]
    assert batches[-1].status == "running"


def test_a_result_without_an_approval_leaves_the_status() -> None:
    [_, b] = run(
        [
            ev("tool_call", 1, id="c1", name="get_model", input={}),
            ev("tool_result", 2, id="c1", name="get_model", status="failed"),
        ]
    )
    assert b.events == [p("tool.result", id="c1", ok=False, summary="failed")]
    assert b.status is None


def test_an_expired_rejection_is_resolved_without_by() -> None:
    batches = run(
        [*needs(), ev("tool_result", 3, id="c1", name="print_output", status="rejected")],
        {"c1": EXPIRED_BY},
    )
    assert batches[-1].events == [
        p("approval.resolved", id=f"durable:{S}:c1", approved=False),
        p("tool.result", id="c1", ok=False, summary="rejected"),
        status("running"),
    ]


def test_a_rejection_by_a_person_was_resolved_by_the_route() -> None:
    batches = run(
        [*needs(), ev("tool_result", 3, id="c1", name="print_output", status="rejected")],
        {"c1": BROWSER},
    )
    assert batches[-1].events == [p("tool.result", id="c1", ok=False, summary="rejected"), status("running")]


def test_running_again_waits_for_every_open_approval() -> None:
    batches = run(
        [
            *needs("c1"),
            *needs("c2"),
            ev("tool_result", 5, id="c1", name="print_output", status="done"),
            ev("tool_result", 6, id="c2", name="print_output", status="done"),
        ]
    )
    assert batches[-2].status is None and status("running") not in batches[-2].events
    assert batches[-1].status == "running"


def test_an_error_flushes_and_settles() -> None:
    [_, b] = run([text(1, "partial"), ev("error", 2, error="Claude run failed: boom")])
    assert b.events == [
        p("assistant.text.delta", messageId=f"{S}-1", delta="partial"),
        p("assistant.text.done", messageId=f"{S}-1"),
        p("error", message="Claude run failed: boom"),
        status("idle"),
    ]
    assert b.status == "idle" and b.final


def test_cancelled_resolves_each_open_approval() -> None:
    batches = run([*needs("c1"), *needs("c2"), ev("cancelled", 5)])
    assert batches[-1].events == [
        p("approval.resolved", id=f"durable:{S}:c1", approved=False, reason="the turn was stopped"),
        p("approval.resolved", id=f"durable:{S}:c2", approved=False, reason="the turn was stopped"),
        status("idle"),
    ]
    assert batches[-1].status == "idle" and batches[-1].final


def test_cancelled_resolves_approvals_seeded_from_the_log() -> None:
    t = Translator(S, TIERS, open_approvals=["c9"])
    b = t.feed(ev("cancelled", 7))
    assert b.events[0] == p(
        "approval.resolved", id=f"durable:{S}:c9", approved=False, reason="the turn was stopped"
    )


@pytest.mark.parametrize("kind", ["prompt", "continued_as_new"])
def test_quiet_events(kind: str) -> None:
    [b] = run([ev(kind, 1, text="hi", run=2)])
    assert b == Batch(events=[], status=None, final=False)


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([p("session.started", origin="chat"), status("running")], ("session.started", "running")),
        ([p("session.owner", owner={}), p("tool.call", id="x")], ("session.owner", None)),
        (
            [p("approval.required", id="a"), status("waiting_approval")],
            ("session.waiting", "waiting_approval"),
        ),
        ([p("session.result"), status("idle")], ("session.done", "idle")),
        ([p("tool.call", id="x")], ("session.message", None)),
        ([p("tool.result", id="x"), status("running")], ("session.message", "running")),
    ],
)
def test_bus_kind_of(events: list[dict[str, Any]], expected: tuple[str, str | None]) -> None:
    assert bus_kind_of(events) == expected
