"""A run grant in Postgres (#2481, plan 2026-10-11-flow-grants-and-git.md Task A1):
approving, declining and revoking it, and `match_grant` consuming its counters once per
request id, under concurrent calls."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scadbuddy.flows.grants import (
    CallFacts,
    GrantSpec,
    Inside,
    Narrowing,
    Outside,
    match_grant,
)
from scadbuddy.flows.store import FlowStore, GrantStateError
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.flow_models import FlowRecord, FlowStep, ProjectionWrite
from tests.flows.flows_support import define

pytestmark = pytest.mark.requires_postgres

NOW = datetime.now(UTC)


@pytest.fixture
def store(jobs: JobProjection) -> FlowStore:
    return FlowStore(jobs.pool)


def spec(*rules: dict[str, Any], **fields: Any) -> GrantSpec:
    return GrantSpec.model_validate(
        {"expires_at": NOW + timedelta(hours=1), "rules": list(rules), **fields}
    )


async def run_row(store: FlowStore, *, session: str | None = None) -> str:
    definition = await define(store, "grants")
    run_id = str(uuid.uuid4())
    author: dict[str, Any] = {"kind": "browser"}
    if session is not None:
        author = {"kind": "agent", "principal": "token:t", "session": session}
    await store.insert_run(
        FlowRecord(
            run_id=run_id,
            definition_id=definition.id,
            version=definition.version,
            name=definition.name,
            workflow_id=f"flow-{run_id}",
            workflow_run_id="wr1",
            started_by=author,
            approval_timeout_s=600,
        )
    )
    return run_id


def print_facts(prints: int = 1) -> Any:
    async def facts_of(fn: str, tool: str | None, args: dict[str, Any]) -> CallFacts:
        return CallFacts(
            fn=fn,
            tool=tool,
            args=args,
            pins={"/request/printer_id": 3},
            printer_id=3,
            models=["coaster"],
            prints=prints,
            filament_g=10.0,
        )

    return facts_of


def hash_of(fn: str, tool: str | None, args: dict[str, Any]) -> str:
    return f"h:{fn}:{sorted(args.get('request', {}).items())}"


async def match(store: FlowStore, run_id: str, request_id: str, **kw: Any) -> Any:
    return await match_grant(
        store,
        kw.pop("facts_of", print_facts()),
        hash_of,
        run_id=run_id,
        request_id=request_id,
        fn=kw.pop("fn", "queue_print"),
        tool=None,
        args={"source": {"output_id": "o"}, "request": {}},
        now=kw.pop("now", datetime.now(UTC)),
        **kw,
    )


async def test_a_requested_grant_is_approved_once_and_revoked_for_good(store: FlowStore) -> None:
    run_id = await run_row(store)
    requested = await store.insert_grant(
        run_id, spec({"fn": "queue_print"}), status="requested", requested_by={"kind": "agent"}
    )
    assert requested.status == "requested" and requested.expires_at is None
    # Inserting again for the same run (a repeated start) keeps the first.
    again = await store.insert_grant(
        run_id, spec({"fn": "arrange"}), status="approved", requested_by={"kind": "browser"}
    )
    assert again.id == requested.id and again.status == "requested"
    edited = spec({"fn": "queue_print", "max_prints": 2})
    approved = await store.approve_grant(run_id, edited, approved_by="browser")
    assert approved is not None and approved.status == "approved" and approved.spec == edited
    assert approved.expires_at == edited.expires_at and approved.approved_at is not None
    with pytest.raises(GrantStateError):
        await store.approve_grant(run_id, edited, approved_by="browser")
    with pytest.raises(GrantStateError):
        await store.decline_grant(run_id)
    revoked = await store.revoke_grant(run_id, revoked_by="browser")
    assert (
        revoked is not None and revoked.revoked_at is not None and revoked.revoked_by == "browser"
    )
    with pytest.raises(GrantStateError):
        await store.revoke_grant(run_id, revoked_by="browser")


async def test_a_declined_grant_is_final(store: FlowStore) -> None:
    run_id = await run_row(store)
    await store.insert_grant(
        run_id, spec({"fn": "queue_print"}), status="requested", requested_by={"kind": "agent"}
    )
    declined = await store.decline_grant(run_id)
    assert declined is not None and declined.status == "declined"
    with pytest.raises(GrantStateError):
        await store.approve_grant(run_id, None, approved_by="browser")
    assert isinstance(await match(store, run_id, "flow:r:w:c"), Outside)


async def test_a_match_pins_its_defaults_and_records_its_use(store: FlowStore) -> None:
    run_id = await run_row(store)
    grant = await store.insert_grant(
        run_id,
        spec({"fn": "arrange"}, {"fn": "queue_print"}),
        status="approved",
        requested_by={"kind": "browser"},
    )
    verdict = await match(store, run_id, "flow:r:w:c1")
    assert isinstance(verdict, Inside)
    assert (verdict.grant_id, verdict.rule_index) == (grant.id, 1)
    assert verdict.args["request"] == {"printer_id": 3}
    assert verdict.input_hash == hash_of("queue_print", None, verdict.args)
    [use] = await store.grant_uses(run_id)
    assert (use.request_id, use.rule_index, use.prints, use.input_hash) == (
        "flow:r:w:c1",
        1,
        1,
        verdict.input_hash,
    )
    after = await store.get_grant(run_id)
    assert after is not None and after.used["1"].calls == 1


async def test_a_repeated_request_id_answers_its_first_verdict_and_consumes_once(
    store: FlowStore,
) -> None:
    run_id = await run_row(store)
    await store.insert_grant(
        run_id,
        spec({"fn": "queue_print", "max_calls": 5}),
        status="approved",
        requested_by={"kind": "browser"},
    )
    first = await match(store, run_id, "flow:r:w:same")
    again = await match(store, run_id, "flow:r:w:same")
    assert first == again
    grant = await store.get_grant(run_id)
    assert grant is not None and grant.used["0"].calls == 1
    # Review of #2643: another run naming the same request id never repeats its verdict.
    other = await run_row(store)
    await store.insert_grant(
        other, spec({"fn": "queue_print"}), status="approved", requested_by={"kind": "browser"}
    )
    assert isinstance(await match(store, other, "flow:r:w:same"), Outside)


async def test_concurrent_calls_for_the_last_print_admit_one(store: FlowStore) -> None:
    run_id = await run_row(store)
    await store.insert_grant(
        run_id,
        spec({"fn": "queue_print", "max_prints": 3}),
        status="approved",
        requested_by={"kind": "browser"},
    )
    assert isinstance(await match(store, run_id, "flow:r:w:a", facts_of=print_facts(2)), Inside)
    verdicts = await asyncio.gather(*(match(store, run_id, f"flow:r:w:{i}") for i in range(4)))
    assert sum(isinstance(v, Inside) for v in verdicts) == 1
    grant = await store.get_grant(run_id)
    assert grant is not None and grant.used["0"].prints == 3


async def test_revoked_or_expired_or_absent_is_outside(store: FlowStore) -> None:
    run_id = await run_row(store, session="11111111-1111-4111-8111-111111111111")
    nothing = await match(store, run_id, "flow:r:w:0")
    # Its starter session is asked first by default (Part B's seam).
    assert nothing == Outside(reason="no grant in force", timeout_s=600, parent_hop=True)
    await store.insert_grant(
        run_id,
        spec({"fn": "queue_print"}, ask_starter_session=False),
        status="approved",
        requested_by={"kind": "browser"},
    )
    later = datetime.now(UTC) + timedelta(hours=2)
    expired = await match(store, run_id, "flow:r:w:1", now=later)
    assert isinstance(expired, Outside) and not expired.parent_hop
    assert isinstance(await match(store, run_id, "flow:r:w:2"), Inside)
    await store.revoke_grant(run_id, revoked_by="browser")
    assert isinstance(await match(store, run_id, "flow:r:w:3"), Outside)


async def test_a_lookup_that_fails_is_outside(store: FlowStore) -> None:
    run_id = await run_row(store)
    await store.insert_grant(
        run_id, spec({"fn": "queue_print"}), status="approved", requested_by={"kind": "browser"}
    )

    async def failing(fn: str, tool: str | None, args: dict[str, Any]) -> CallFacts:
        raise OSError("api away")

    verdict = await match(store, run_id, "flow:r:w:0", facts_of=failing)
    assert isinstance(verdict, Outside) and "OSError" in verdict.reason
    assert await store.grant_uses(run_id) == []


async def test_a_child_call_needs_sessions_and_a_flow_turn_of_the_run(store: FlowStore) -> None:
    session = "22222222-2222-4222-8222-222222222222"
    run_id = await run_row(store)
    await store.project(
        ProjectionWrite(
            run_id=run_id,
            workflow_run_id="wr1",
            step=FlowStep(
                seq=1,
                fn="agent",
                call_id="c-agent",
                status="running",
                outward=False,
                started_at=NOW,
                history_length=5,
                session_id=session,
            ),
        )
    )

    async def tool_facts(fn: str, tool: str | None, args: dict[str, Any]) -> CallFacts:
        return CallFacts(fn=fn, tool=tool, args=args)

    async def child(request_id: str, **kw: Any) -> Any:
        return await match_grant(
            store,
            tool_facts,
            hash_of,
            run_id=run_id,
            request_id=request_id,
            fn="tool",
            tool="send_to_bambuddy",
            args={"printer_id": 3},
            now=datetime.now(UTC),
            **kw,
        )

    await store.insert_grant(
        run_id,
        spec({"fn": "tool", "tool": "send_to_bambuddy", "max_calls": 3}, sessions=True),
        status="approved",
        requested_by={"kind": "browser"},
    )
    assert isinstance(await child("d:1", session_id=session, flow_call_id="c-agent"), Inside)
    assert isinstance(await child("d:2", session_id=session, flow_call_id="other"), Outside)
    other_session = "33333333-3333-4333-8333-333333333333"
    assert isinstance(await child("d:3", session_id=other_session, flow_call_id="c-agent"), Outside)
    # A narrowing admits only what it and the grant both admit, and keeps its own count.
    narrow = Narrowing.model_validate(
        {"rules": [{"fn": "tool", "tool": "send_to_bambuddy", "max_calls": 1}]}
    )
    assert isinstance(
        await child("d:4", session_id=session, flow_call_id="c-agent", narrowing=narrow), Inside
    )
    assert isinstance(
        await child("d:5", session_id=session, flow_call_id="c-agent", narrowing=narrow), Outside
    )
    wider = Narrowing.model_validate({"rules": [{"fn": "tool", "tool": "delete_model"}]})
    assert isinstance(
        await child("d:6", session_id=session, flow_call_id="c-agent", narrowing=wider), Outside
    )


async def test_without_sessions_a_child_call_is_outside(store: FlowStore) -> None:
    run_id = await run_row(store)
    await store.insert_grant(
        run_id,
        spec({"fn": "tool", "tool": "send_to_bambuddy"}),
        status="approved",
        requested_by={"kind": "browser"},
    )
    verdict = await match_grant(
        store,
        print_facts(),
        hash_of,
        run_id=run_id,
        request_id="d:x",
        fn="tool",
        tool="send_to_bambuddy",
        args={},
        session_id="s",
        flow_call_id="c",
        now=datetime.now(UTC),
    )
    assert isinstance(verdict, Outside) and "sessions" in verdict.reason


async def test_an_argument_bound_is_stored_as_json_names_it(
    store: FlowStore, jobs: JobProjection
) -> None:
    run_id = await run_row(store)
    bounded = spec(
        {"fn": "tool", "tool": "send_to_bambuddy", "args": [{"path": "/printer_id", "in": [3]}]}
    )
    await store.insert_grant(run_id, bounded, status="approved", requested_by={})
    with jobs.pool.connection() as conn:
        row = conn.execute("SELECT spec FROM flow_grants WHERE run_id = %s", (run_id,)).fetchone()
    assert row is not None and row["spec"]["rules"][0]["args"] == [
        {"path": "/printer_id", "in": [3]}
    ]
    grant = await store.get_grant(run_id)
    assert grant is not None and grant.spec == bounded


async def test_deleting_the_run_deletes_its_grant(store: FlowStore) -> None:
    run_id = await run_row(store)
    await store.insert_grant(
        run_id, spec({"fn": "queue_print"}), status="approved", requested_by={"kind": "browser"}
    )
    await match(store, run_id, "flow:r:w:0")
    assert await store.delete_run(run_id)
    assert await store.get_grant(run_id) is None
    assert await store.grant_uses(run_id) == []


async def test_orphan_grants_are_swept(store: FlowStore) -> None:
    """A start that failed after its grant was inserted leaves one with no run row."""
    orphan = str(uuid.uuid4())
    await store.insert_grant(
        orphan, spec({"fn": "queue_print"}), status="approved", requested_by={"kind": "browser"}
    )
    kept = await run_row(store)
    await store.insert_grant(
        kept, spec({"fn": "queue_print"}), status="approved", requested_by={"kind": "browser"}
    )
    assert await store.orphan_grants(timedelta(0)) == [orphan]
    assert await store.orphan_grants(timedelta(hours=1)) == []
    await store.delete_grant(orphan)
    assert await store.get_grant(orphan) is None
