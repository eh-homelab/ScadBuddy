"""``FollowPrint`` (#1053, spec 2026-10-01 §4.4) on a dev server, its activity faked."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.bambuddy.follow import FOLLOW_ACTIVITY, MAX_AGE, FollowInput
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows import follow as follow_module
from scadbuddy.workflows.follow import FollowPrint, follow, follow_id, resume_followed
from tests.support.temporal import temporal_client

pytestmark = pytest.mark.requires_temporal


class FakeFollow:
    """`follow_print`: each attempt recorded; it ends when ``end`` is set, else it
    waits until cancelled."""

    def __init__(self) -> None:
        self.attempts: list[FollowInput] = []
        self.end = asyncio.Event()
        self.cancelled = 0

    @activity.defn(name=FOLLOW_ACTIVITY)
    async def follow_print(self, input: FollowInput) -> str:
        self.attempts.append(input)
        try:
            while not self.end.is_set():
                activity.heartbeat()
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return "settled"


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


async def _until(predicate: object, timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():  # type: ignore[operator]
            await asyncio.sleep(0.02)


async def test_it_completes_when_the_follow_ends(client: Client) -> None:
    queue, output = f"follow-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    fake = FakeFollow()
    fake.end.set()
    async with Worker(
        client, task_queue=queue, workflows=[FollowPrint], activities=[fake.follow_print]
    ):
        await follow(client, queue, output)
        result = await client.get_workflow_handle(follow_id(output)).result()
    assert result == "settled"
    assert [a.fresh for a in fake.attempts] == [False]


async def test_a_poke_restarts_the_follow_fresh(client: Client) -> None:
    queue, output = f"follow-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    fake = FakeFollow()
    async with Worker(
        client, task_queue=queue, workflows=[FollowPrint], activities=[fake.follow_print]
    ):
        await follow(client, queue, output)
        await _until(lambda: len(fake.attempts) == 1)
        handle = client.get_workflow_handle(follow_id(output))
        await handle.signal("poke")
        await _until(lambda: len(fake.attempts) == 2)
        fake.end.set()
        await handle.result()
    # The old attempt hears of its cancel at a later heartbeat; the fresh one reads now.
    assert [a.fresh for a in fake.attempts] == [False, True]


async def test_pokes_past_the_bound_continue_as_new(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A print poked over and over does not grow one history without bound."""
    monkeypatch.setattr(follow_module, "MAX_POKES", 2)
    queue, output = f"follow-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    fake = FakeFollow()
    async with Worker(
        client,
        task_queue=queue,
        workflows=[FollowPrint],
        activities=[fake.follow_print],
        # Unsandboxed, so the patched bound is the one the workflow reads.
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        await follow(client, queue, output)
        handle = client.get_workflow_handle(follow_id(output))
        first = (await handle.describe()).run_id
        for n in (2, 3):
            await _until(lambda: len(fake.attempts) == n - 1)  # noqa: B023
            await handle.signal("poke")
        await _until(lambda: len(fake.attempts) == 3)
        fake.end.set()
        assert await handle.result() == "settled"
        last = (await handle.describe()).run_id
    assert last != first
    assert [a.fresh for a in fake.attempts] == [False, True, True]


async def test_a_finished_follow_can_start_again(client: Client) -> None:
    queue, output = f"follow-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    fake = FakeFollow()
    fake.end.set()
    async with Worker(
        client, task_queue=queue, workflows=[FollowPrint], activities=[fake.follow_print]
    ):
        await follow(client, queue, output)
        await client.get_workflow_handle(follow_id(output)).result()
        await follow(client, queue, output)
        await client.get_workflow_handle(follow_id(output)).result()
    assert len(fake.attempts) == 2


async def test_following_with_temporal_unreachable_is_only_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lazy = await Client.connect("127.0.0.1:1", lazy=True)
    await follow(lazy, "nowhere", uuid.uuid4().hex)
    assert any("could not follow a print" in r.message for r in caplog.records)


@pytest.mark.requires_postgres
async def test_resume_followed_starts_recent_prints_and_clears_the_old_log(
    client: Client, pg_conninfo: str
) -> None:
    projection = JobProjection(pg_conninfo, pool_size=2)
    await asyncio.to_thread(projection.open)
    now = datetime.now(UTC)
    recent, stale = uuid.uuid4().hex, uuid.uuid4().hex
    with projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_watches (output_id, printed_at) VALUES (%s, %s), (%s, %s)",
            (recent, now - timedelta(hours=1), stale, now - MAX_AGE - timedelta(hours=1)),
        )
    queue = f"follow-{uuid.uuid4().hex[:8]}"
    fake = FakeFollow()
    fake.end.set()
    try:
        async with Worker(
            client, task_queue=queue, workflows=[FollowPrint], activities=[fake.follow_print]
        ):
            resumed = await resume_followed(projection.pool, client, queue, now)
            await client.get_workflow_handle(follow_id(recent)).result()
        with projection.pool.connection() as conn:
            left = conn.execute("SELECT count(*) AS n FROM print_watches").fetchone()
    finally:
        await asyncio.to_thread(projection.close)
    assert resumed == [recent]
    assert left is not None and left["n"] == 0
    assert [a.output_id for a in fake.attempts] == [recent]


@pytest.mark.requires_postgres
async def test_resume_followed_keeps_a_print_whose_follow_did_not_start(
    pg_conninfo: str,
) -> None:
    """A start that fails leaves its row for the next boot's hand-over."""
    projection = JobProjection(pg_conninfo, pool_size=2)
    await asyncio.to_thread(projection.open)
    output = uuid.uuid4().hex
    now = datetime.now(UTC)
    with projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_watches (output_id, printed_at) VALUES (%s, %s)",
            (output, now - timedelta(hours=1)),
        )
    lazy = await Client.connect("127.0.0.1:1", lazy=True)
    try:
        resumed = await resume_followed(projection.pool, lazy, "nowhere", now)
        with projection.pool.connection() as conn:
            left = conn.execute("SELECT output_id FROM print_watches").fetchall()
    finally:
        await asyncio.to_thread(projection.close)
    assert resumed == []
    assert [row["output_id"] for row in left] == [output]


@pytest.mark.requires_postgres
async def test_resume_followed_once_the_old_log_is_dropped(pg_conninfo: str) -> None:
    projection = JobProjection(pg_conninfo, pool_size=2)
    await asyncio.to_thread(projection.open)
    lazy = await Client.connect("127.0.0.1:1", lazy=True)
    try:
        with projection.pool.connection() as conn:
            conn.execute("DROP TABLE print_watches")
        resumed = await resume_followed(projection.pool, lazy, "nowhere", datetime.now(UTC))
    finally:
        await asyncio.to_thread(projection.close)
    assert resumed == []


async def test_follow_starts_one_execution_and_leaves_it_running(client: Client) -> None:
    """A progress read only makes sure the print is followed: restarting the attempt on
    every read would grow its history and overlap its reads. A new print pokes, from
    `PrintRun`."""
    queue, output = f"follow-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    fake = FakeFollow()
    async with Worker(
        client, task_queue=queue, workflows=[FollowPrint], activities=[fake.follow_print]
    ):
        await follow(client, queue, output)
        await _until(lambda: len(fake.attempts) == 1)
        first = (await client.get_workflow_handle(follow_id(output)).describe()).run_id
        await follow(client, queue, output)
        await asyncio.sleep(0.5)
        second = (await client.get_workflow_handle(follow_id(output)).describe()).run_id
        fake.end.set()
        await client.get_workflow_handle(follow_id(output)).result()
    assert first == second
    assert len(fake.attempts) == 1
