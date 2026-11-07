"""The blob index's Idempotency-Key across a real Temporal activity's attempts (#5298
Task 16, plan D5): attempt 1 writes and then fails; attempt 2 sends its first write under
the same key (`WriteKeys`: the ordinal in the attempt, the same run). The same write is
answered as attempt 1's was; another write takes the ledger's 422 and runs under a key of
its own, and the activity succeeds."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.crud.render import RemoteBlobIndex
from scadbuddy.store.content_models import BlobRef
from scadbuddy.store.index import BlobIndex, Pool
from tests.crud.crud_support import served_render_crud
from tests.support.store import store_pool
from tests.support.temporal import temporal_client

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]


def _ref(name: str) -> BlobRef:
    return BlobRef(sha256=name * 64, kind="piece", backend="bambuddy", backend_id=name, size=1)


class PutTwice:
    """An activity that puts ``k`` and fails after the write on its first attempt; on its
    second it puts ``k`` again, at ``second`` (the same ref, or another)."""

    def __init__(self, index: RemoteBlobIndex) -> None:
        self.index = index
        self.answers: dict[int, BlobRef | None] = {}

    @activity.defn(name="put_twice")
    async def put_twice(self, second: str) -> None:
        attempt = activity.info().attempt
        ref = _ref("b") if attempt == 1 else _ref(second)
        self.answers[attempt] = await asyncio.to_thread(
            self.index.put, "k", ref, slug="demo", meta={}
        )
        if attempt == 1:
            raise RuntimeError("failed after the write")


@workflow.defn(name="PutTwiceWorkflow")
class PutTwiceWorkflow:
    @workflow.run
    async def run(self, second: str) -> None:
        await workflow.execute_activity(
            "put_twice",
            second,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(
                maximum_attempts=2, initial_interval=timedelta(milliseconds=100)
            ),
        )


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened


def _ledger(pool: Pool) -> int:
    with pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM crud_requests").fetchone()
    assert row is not None
    return int(row["n"])


@pytest.mark.parametrize(
    ("second", "previous", "named", "ledger"),
    [
        # The same write: replayed, attempt 1's `previous`, not its own `b`.
        ("b", "a", "b", 1),
        # Another write: 422, then a new key; it runs and replaces attempt 1's `b`.
        ("c", "b", "c", 2),
    ],
    ids=["same-write-replays", "another-write-runs-anew"],
)
async def test_a_retried_activity_attempts_write_under_its_ordinal_key(
    pool: Pool, second: str, previous: str, named: str, ledger: int
) -> None:
    sql = BlobIndex(pool)
    sql.put("k", _ref("a"), slug="demo", meta={})
    queue = f"t-{uuid.uuid4().hex[:8]}"
    async with (
        served_render_crud(pool, unavailable_budget=2) as crud,
        temporal_client() as client,
    ):
        remote = crud.blocking()
        activities = PutTwice(RemoteBlobIndex(remote))
        try:
            async with Worker(
                client,
                task_queue=queue,
                workflows=[PutTwiceWorkflow],
                activities=[activities.put_twice],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                await client.execute_workflow(
                    PutTwiceWorkflow.run,
                    second,
                    id=f"put-twice-{uuid.uuid4().hex}",
                    task_queue=queue,
                    execution_timeout=timedelta(seconds=120),
                )
        finally:
            remote.close()
    assert activities.answers == {1: _ref("a"), 2: _ref(previous)}
    row = sql.get("k")
    assert row is not None and row.ref == _ref(named)
    assert _ledger(pool) == ledger
