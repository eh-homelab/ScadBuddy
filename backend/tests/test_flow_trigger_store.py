"""`TriggerStore` against Postgres (#2484, spec 2026-10-11 §12): a fire is admitted under
the trigger's row lock, so concurrent fires keep to its overlap and rate cap, and a
standing grant's limits are spent across every run it seeds, never once per run."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest

from scadbuddy.flow_triggers.models import ScheduleTiming, Trigger
from scadbuddy.flow_triggers.store import TriggerStore
from scadbuddy.flows.grants import (
    CallFacts,
    Consumption,
    Grant,
    GrantRule,
    GrantSpec,
    GrantUse,
    choose,
)
from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection

pytestmark = pytest.mark.requires_postgres

BROWSER = {"kind": "browser"}


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=10)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def triggers(jobs: JobProjection) -> TriggerStore:
    return TriggerStore(jobs.pool)


@pytest.fixture
def flows(jobs: JobProjection) -> FlowStore:
    return FlowStore(jobs.pool, events=None)


async def make(triggers: TriggerStore, overlap: str = "skip") -> Trigger:
    return await triggers.insert(
        Trigger.model_validate(
            {
                "id": str(uuid.uuid4()),
                "name": "swap",
                "definition_id": str(uuid.uuid4()),
                "kind": "schedule",
                "schedule": ScheduleTiming(interval_s=3600),
                "overlap": overlap,
                "paused": False,
                "created_by": BROWSER,
                "created_at": datetime.now(UTC),
            }
        )
    )


async def admit_all(triggers: TriggerStore, trigger: Trigger, n: int, cap: int) -> list[str | None]:
    keys = [f"fire-{i}" for i in range(n)]
    for key in keys:
        assert await triggers.claim_fire(trigger.id, key, f"run-{key}", None) is not None
    return list(await asyncio.gather(*(triggers.admit(trigger.id, key, cap) for key in keys)))


async def test_concurrent_fires_keep_to_overlap_skip(triggers: TriggerStore) -> None:
    trigger = await make(triggers)
    verdicts = await admit_all(triggers, trigger, 8, cap=60)
    assert verdicts.count(None) == 1
    assert sorted(v for v in verdicts if v is not None) == ["overlap"] * 7


async def test_concurrent_fires_keep_to_the_rate_cap(triggers: TriggerStore) -> None:
    trigger = await make(triggers, overlap="allow_all")
    verdicts = await admit_all(triggers, trigger, 8, cap=3)
    assert verdicts.count(None) == 3
    assert verdicts.count("rate") == 5


async def test_an_admitted_fire_is_admitted_again_on_a_retry(triggers: TriggerStore) -> None:
    trigger = await make(triggers)
    [first] = await admit_all(triggers, trigger, 1, cap=60)
    assert first is None
    assert await triggers.admit(trigger.id, "fire-0", 60) is None


def arrange_pick(run_id: str, request_id: str) -> Callable[[Grant], Consumption | str]:
    facts = CallFacts(fn="arrange", models=["coaster"])

    def pick(locked: Grant) -> Consumption | str:
        chosen = choose(locked, facts, narrowing=None, narrowed_by=None, now=datetime.now(UTC))
        if isinstance(chosen, str):
            return chosen
        return Consumption(
            rule_index=chosen[0],
            use=GrantUse(
                request_id=request_id,
                grant_id=locked.id,
                run_id=run_id,
                fn="arrange",
                input_hash="h",
                rule_index=chosen[0],
            ),
        )

    return pick


async def test_a_standing_grants_limits_are_spent_across_its_runs(
    triggers: TriggerStore, flows: FlowStore
) -> None:
    trigger = await make(triggers, overlap="allow_all")
    spec = GrantSpec(
        expires_at=datetime.now(UTC) + timedelta(days=10),
        rules=[GrantRule(fn="arrange", max_calls=2)],
    )
    await triggers.attach_grant(trigger.id, spec, "approved", BROWSER)
    runs = [str(uuid.uuid4()) for _ in range(4)]
    for run_id in runs:
        assert await triggers.seed_run_grant(run_id, trigger.id, BROWSER, datetime.now(UTC))
    taken = await asyncio.gather(
        *(
            flows.consume_grant(run_id, f"req-{run_id}", arrange_pick(run_id, f"req-{run_id}"))
            for run_id in runs
        )
    )
    # Two calls across all four runs, not two each.
    assert sum(isinstance(t, GrantUse) for t in taken) == 2
    standing = await triggers.current_grant(trigger.id)
    assert standing is not None and standing.used["0"].calls == 2


async def test_a_revoked_standing_grant_stops_its_runs_and_the_next_fire(
    triggers: TriggerStore, flows: FlowStore
) -> None:
    trigger = await make(triggers, overlap="allow_all")
    spec = GrantSpec(
        expires_at=datetime.now(UTC) + timedelta(days=10), rules=[GrantRule(fn="arrange")]
    )
    await triggers.attach_grant(trigger.id, spec, "approved", BROWSER)
    run_id = str(uuid.uuid4())
    assert await triggers.seed_run_grant(run_id, trigger.id, BROWSER, datetime.now(UTC))
    await triggers.revoke_grant(trigger.id, "browser")
    refused = await flows.consume_grant(run_id, "req", arrange_pick(run_id, "req"))
    assert isinstance(refused, str)
    assert (
        await triggers.seed_run_grant(str(uuid.uuid4()), trigger.id, BROWSER, datetime.now(UTC))
        is None
    )
