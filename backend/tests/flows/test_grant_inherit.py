"""A run started from another run (#4252, orders spec §7.2, §7.4): its links, and the
grant it inherits, narrowed to its order, in force only while the parent's is."""

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.api.flows import may_inherit, runs_as
from scadbuddy.auth.models import ANONYMOUS, Service
from scadbuddy.auth.run_tokens import mint_run_token
from scadbuddy.auth.service_credentials import SCOPES
from scadbuddy.flows.grants import CallFacts, GrantSpec, Inside, Outside, inherited, match_grant
from scadbuddy.flows.models import Decision, Run
from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection
from tests.flows.test_grant_store import hash_of, run_row, spec

NOW = datetime.now(UTC)


def test_an_inherited_grant_drops_limited_rules_and_narrows_to_the_order() -> None:
    parent = GrantSpec.model_validate(
        {
            "expires_at": NOW + timedelta(hours=1),
            "rules": [
                {"fn": "queue_print", "orders": "any"},
                {"fn": "queue_print", "max_prints": 4},
                {"fn": "tool", "tool": "order_get"},
                {"fn": "tool", "tool": "order_item_fulfil", "orders": ["o2"]},
            ],
        }
    )
    child = inherited(parent, "o1")
    assert [r.model_dump(exclude_none=True) for r in child.rules] == [
        {"fn": "queue_print", "orders": ["o1"]},
        # A limit is the parent's to spend, never each child's again.
        {"fn": "queue_print", "max_prints": 4, "max_calls": 0},
        {"fn": "tool", "tool": "order_get"},
        {"fn": "tool", "tool": "order_item_fulfil", "orders": ["o1"], "max_calls": 0},
    ]
    assert child.ask_starter_session is False and child.expires_at == parent.expires_at
    assert inherited(parent, None).rules[0].orders == "any"


pg = pytest.mark.requires_postgres


@pytest.fixture
def store(jobs: JobProjection) -> FlowStore:
    return FlowStore(jobs.pool)


def order_facts(order_id: str) -> Any:
    async def facts_of(fn: str, tool: str | None, args: dict[str, Any]) -> CallFacts:
        return CallFacts(fn=fn, tool=tool, args=args, order_id=order_id, prints=1)

    return facts_of


async def match(store: FlowStore, run_id: str, order_id: str) -> Any:
    return await match_grant(
        store,
        order_facts(order_id),
        hash_of,
        run_id=run_id,
        request_id=f"flow:{run_id}:w:{uuid.uuid4()}",
        fn="queue_print",
        tool=None,
        args={"source": {"order_id": order_id, "order_item_id": "i1"}, "request": {}},
        now=datetime.now(UTC),
    )


@pg
async def test_a_run_records_the_run_it_was_started_from_and_its_order(
    store: FlowStore,
) -> None:
    parent = await run_row(store)
    child_id = str(uuid.uuid4())
    assert not await store.is_linked(child_id)
    await store.link_run(child_id, parent_run_id=parent, order_id="o1")
    # A repeat of this start skips the order check (review of #5668).
    assert await store.is_linked(child_id)
    # A repeated start keeps the first record.
    await store.link_run(child_id, parent_run_id=None, order_id="o2")
    child = await run_row(store, run_id=child_id)
    run = await store.get_run(child)
    assert run is not None and (run.parent_run_id, run.order_id) == (parent, "o1")
    assert [r.id for r in await store.list_runs(order_id="o1")] == [child]
    assert await store.list_runs(order_id="o2") == []


@pg
async def test_a_child_inherits_its_parents_grant_for_its_order_until_the_parent_is_revoked(
    store: FlowStore,
) -> None:
    parent = await run_row(store)
    granted = await store.insert_grant(
        parent,
        spec({"fn": "queue_print", "orders": "any"}),
        status="approved",
        requested_by={"kind": "browser"},
    )
    child = await run_row(store)
    grant = await store.inherit_grant(
        child, parent, "o1", requested_by={"kind": "agent"}, approved=True
    )
    assert grant is not None and grant.status == "approved"
    assert grant.approved_by == f"parent-run:{parent}" and grant.parent_grant_id == granted.id
    assert grant.expires_at == granted.expires_at
    assert isinstance(await match(store, child, "o1"), Inside)
    assert isinstance(await match(store, child, "o2"), Outside)
    # A grandchild inherits through it.
    grandchild = await run_row(store)
    assert (
        await store.inherit_grant(grandchild, child, "o1", requested_by={}, approved=True)
        is not None
    )
    assert isinstance(await match(store, grandchild, "o1"), Inside)
    await store.revoke_grant(parent, revoked_by="browser")
    for run_id in (child, grandchild):
        after = await store.get_grant(run_id)
        assert after is not None and after.revoked_by == f"parent-grant:{granted.id}"
        assert isinstance(await match(store, run_id, "o1"), Outside)


@pg
async def test_a_deleted_parent_revokes_its_childrens_grants_and_keeps_their_uses(
    store: FlowStore,
) -> None:
    """Review of #5668: deleting the parent's run revokes every grant inherited from it,
    at any depth, and keeps the children's rows (and so their uses) in place."""
    parent = await run_row(store)
    await store.insert_grant(
        parent,
        spec({"fn": "queue_print", "orders": "any"}),
        status="approved",
        requested_by={"kind": "browser"},
    )
    child = await run_row(store)
    await store.inherit_grant(child, parent, "o1", requested_by={}, approved=True)
    grandchild = await run_row(store)
    await store.inherit_grant(grandchild, child, "o1", requested_by={}, approved=True)
    assert isinstance(await match(store, grandchild, "o1"), Inside)
    assert await store.delete_run(parent)
    for run_id in (child, grandchild):
        after = await store.get_grant(run_id)
        assert after is not None and after.revoked_at is not None
        assert isinstance(await match(store, run_id, "o1"), Outside)
    assert await store.grant_uses(grandchild) != []


@pg
async def test_a_child_stops_when_its_parents_grant_ends_however_it_ends(
    store: FlowStore, jobs: JobProjection
) -> None:
    """Expired, or its standing grant revoked: the chain is checked at each call, not
    only through a revoke's cascade."""
    parent = await run_row(store)
    granted = await store.insert_grant(
        parent, spec({"fn": "queue_print"}), status="approved", requested_by={"kind": "browser"}
    )
    child = await run_row(store)
    await store.inherit_grant(child, parent, None, requested_by={}, approved=True)
    assert isinstance(await match(store, child, "o1"), Inside)
    with jobs.pool.connection() as conn:
        conn.execute(
            "UPDATE flow_grants SET expires_at = now() - interval '1 second' WHERE id = %s",
            (granted.id,),
        )
    assert isinstance(await match(store, child, "o1"), Outside)


@pg
async def test_an_ended_chain_shows_out_of_force_and_records_no_decision(
    store: FlowStore, jobs: JobProjection
) -> None:
    """Review of #5668: the run view's `in_force` and a grant decision's record check the
    whole chain, as a call does: a use recorded while the parent's grant stood records no
    approval once that grant has expired."""
    parent = await run_row(store)
    granted = await store.insert_grant(
        parent, spec({"fn": "queue_print"}), status="approved", requested_by={"kind": "browser"}
    )
    child = await run_row(store)
    grant = await store.inherit_grant(child, parent, None, requested_by={}, approved=True)
    assert grant is not None and await store.grant_in_force(grant)
    request_id = f"flow:{child}:w:c"
    inside = await match_grant(
        store,
        order_facts("o1"),
        hash_of,
        run_id=child,
        request_id=request_id,
        fn="queue_print",
        tool=None,
        args={"source": {"order_id": "o1", "order_item_id": "i1"}, "request": {}},
        now=datetime.now(UTC),
    )
    assert isinstance(inside, Inside)
    with jobs.pool.connection() as conn:
        conn.execute(
            "UPDATE flow_grants SET expires_at = now() - interval '1 second' WHERE id = %s",
            (granted.id,),
        )
    assert not await store.grant_in_force(grant)
    decision = Decision(
        request_id=request_id,
        run_id=child,
        workflow_run_id="w",
        call_id="c",
        kind="approval",
        outcome="approved",
        response={},
        responder=f"grant:{inside.grant_id}",
        input_hash=inside.input_hash,
    )
    assert await store.record_grant_decision(decision) is False


@pg
async def test_no_grant_is_inherited_from_a_parent_without_one_in_force(
    store: FlowStore,
) -> None:
    parent = await run_row(store)
    child = await run_row(store)
    assert await store.inherit_grant(child, parent, None, requested_by={}, approved=True) is None
    await store.insert_grant(
        parent, spec({"fn": "queue_print"}), status="requested", requested_by={"kind": "agent"}
    )
    assert await store.inherit_grant(child, parent, None, requested_by={}, approved=True) is None
    assert await store.get_grant(child) is None


@pg
async def test_a_grant_not_passed_on_is_only_requested(store: FlowStore) -> None:
    """A caller `may_inherit` refuses (security review of #4252): the parent's rules,
    narrowed, as an ordinary request a person decides, naming the parent's grant."""
    parent = await run_row(store)
    await store.insert_grant(
        parent,
        spec({"fn": "queue_print", "orders": "any"}),
        status="approved",
        requested_by={"kind": "browser"},
    )
    child = await run_row(store)
    grant = await store.inherit_grant(child, parent, "o1", requested_by={}, approved=False)
    assert grant is not None and grant.status == "requested"
    assert (grant.approved_by, grant.approved_at, grant.expires_at) == (None, None, None)
    granted = await store.get_grant(parent)
    assert granted is not None and grant.parent_grant_id == granted.id
    assert grant.spec.rules[0].orders == ["o1"]
    assert isinstance(await match(store, child, "o1"), Outside)


# -- who may inherit (security review of #4252) -------------------------------------------

KEY = os.urandom(32)
PARENT = "7d1c1a0e-8a1b-4c55-9b7e-3f2f3f2f3f2f"
PROJECTS = Service("projects", SCOPES["projects"])


def parent(status: str = "waiting") -> Run:
    return cast(Run, SimpleNamespace(id=PARENT, status=status))


def test_only_the_parents_own_projects_worker_call_inherits() -> None:
    """A run inherits its parent's grant only when the projects worker starts it with a
    run token for that very parent, named as the flow run, and the parent is open."""
    token = mint_run_token(KEY, PARENT)
    assert may_inherit(PROJECTS, token, KEY, PARENT, parent())


def test_an_agent_credential_never_inherits() -> None:
    token = mint_run_token(KEY, PARENT)
    assert not may_inherit(Service("agent", frozenset({"api"})), token, KEY, PARENT, parent())


def test_a_projects_call_without_the_parents_token_never_inherits() -> None:
    assert not may_inherit(PROJECTS, None, KEY, PARENT, parent())
    other = mint_run_token(KEY, "another-run")
    assert not may_inherit(PROJECTS, other, KEY, PARENT, parent())
    forged = mint_run_token(os.urandom(32), PARENT)
    assert not may_inherit(PROJECTS, forged, KEY, PARENT, parent())
    # No key to check it with: nothing is believed.
    assert not may_inherit(PROJECTS, mint_run_token(KEY, PARENT), None, PARENT, parent())


def test_a_flow_run_header_other_than_the_tokens_never_inherits() -> None:
    token = mint_run_token(KEY, PARENT)
    assert not may_inherit(PROJECTS, token, KEY, "another-run", parent())
    assert not may_inherit(PROJECTS, token, KEY, None, parent())


def test_a_closed_parent_never_passes_its_grant_on() -> None:
    token = mint_run_token(KEY, PARENT)
    for status in ("succeeded", "failed", "terminated"):
        assert not may_inherit(PROJECTS, token, KEY, PARENT, parent(status))


def test_a_run_s_calls_are_vouched_for_by_the_agent_or_match_its_recorded_principal() -> None:
    """Review of #5668: the API reads no `ai_sessions` row to check a parent (#5295). The
    agent's credential vouches for a call from a run whose session was handed off and
    archived (the agent makes it as the owner now); any other caller must be the
    principal the run records."""
    agent = Service("agent", frozenset({"api"}))
    started = {"kind": "agent", "principal": "token:t1", "session": str(uuid.uuid4())}
    assert runs_as(agent, started, "token:new-owner")
    assert not runs_as(agent, started, None)
    assert runs_as(ANONYMOUS, started, "token:t1")
    assert not runs_as(ANONYMOUS, started, "token:new-owner")
    assert not runs_as(PROJECTS, started, "token:new-owner")
    # The session that started it, under its owner now.
    assert runs_as(ANONYMOUS, started, "token:new-owner", started["session"])
    assert not runs_as(ANONYMOUS, started, "token:new-owner", str(uuid.uuid4()))
    browser = {"kind": "browser", "principal": "user:a"}
    assert runs_as(ANONYMOUS, browser, "user:a")
    assert not runs_as(ANONYMOUS, browser, "user:b")
    assert runs_as(ANONYMOUS, {"kind": "browser"}, "browser")
