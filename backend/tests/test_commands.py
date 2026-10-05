"""`start_command` (#1052, spec 2026-10-01 §4.2): every command starts with
update-with-start, attaches to a running execution and keeps the reuse policy."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any, cast

import pytest
from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from scadbuddy.workflows.commands import (
    CONNECT_MARGIN_SECONDS,
    AlreadyClosedError,
    CommandClosedError,
    CommandStillAcceptingError,
    TemporalBusyError,
    TemporalRefusedError,
    TemporalUnavailableError,
    TemporalUnreachableError,
    start_command,
    temporal_failure,
)
from tests.support.temporal import current_address, namespace_not_found_error, temporal_client

pytestmark = pytest.mark.requires_temporal


class EchoInput(BaseModel):
    #: How long the `accepted` Update takes to answer.
    delay_s: float = 0.0
    #: Complete as soon as the first Update has answered.
    finish_at_once: bool = False


class EchoAnswer(BaseModel):
    updates: int
    run_id: str


@workflow.defn(name="EchoCommand")
class EchoCommand:
    @workflow.init
    def __init__(self, arg: EchoInput) -> None:
        self.arg = arg
        self.updates = 0
        self.finished = False

    @workflow.run
    async def run(self, arg: EchoInput) -> int:
        await workflow.wait_condition(
            lambda: self.finished or (arg.finish_at_once and self.updates > 0)
        )
        await workflow.wait_condition(workflow.all_handlers_finished)
        return self.updates

    @workflow.update(name="accepted")
    async def accepted(self) -> EchoAnswer:
        self.updates += 1
        if self.arg.delay_s:
            await workflow.sleep(self.arg.delay_s)
        return EchoAnswer(updates=self.updates, run_id=workflow.info().run_id)

    @workflow.signal
    def finish(self) -> None:
        self.finished = True


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.fixture
def queue() -> str:
    return f"commands-{uuid.uuid4().hex[:8]}"


async def echo(
    client: Client,
    queue: str,
    workflow_id: str,
    arg: EchoInput | None = None,
    *,
    reuse: WorkflowIDReusePolicy = WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
    deadline: timedelta = timedelta(seconds=10),
) -> EchoAnswer:
    return await start_command(
        client,
        "EchoCommand",
        arg or EchoInput(),
        id=workflow_id,
        task_queue=queue,
        update="accepted",
        result_type=EchoAnswer,
        reuse=reuse,
        deadline=deadline,
    )


async def test_the_first_call_starts_and_answers_from_the_update(
    client: Client, queue: str
) -> None:
    async with Worker(client, task_queue=queue, workflows=[EchoCommand]):
        answer = await echo(client, queue, f"echo-{uuid.uuid4().hex}")
    assert answer.updates == 1


async def test_a_second_call_while_running_attaches_to_the_same_execution(
    client: Client, queue: str
) -> None:
    workflow_id = f"echo-{uuid.uuid4().hex}"
    async with Worker(client, task_queue=queue, workflows=[EchoCommand]):
        first = await echo(client, queue, workflow_id)
        second = await echo(client, queue, workflow_id)
        await client.get_workflow_handle(workflow_id).signal("finish")
    assert (first.updates, second.updates) == (1, 2)
    assert first.run_id == second.run_id


async def test_failed_only_refuses_a_new_start_after_a_completed_execution(
    client: Client, queue: str
) -> None:
    workflow_id = f"echo-{uuid.uuid4().hex}"
    async with Worker(client, task_queue=queue, workflows=[EchoCommand]):
        await echo(client, queue, workflow_id, EchoInput(finish_at_once=True))
        await client.get_workflow_handle(workflow_id).result()
        with pytest.raises(AlreadyClosedError):
            await echo(client, queue, workflow_id, EchoInput(finish_at_once=True))


async def test_allow_duplicate_starts_a_new_execution_after_one_completed(
    client: Client, queue: str
) -> None:
    workflow_id = f"echo-{uuid.uuid4().hex}"
    allow = WorkflowIDReusePolicy.ALLOW_DUPLICATE
    async with Worker(client, task_queue=queue, workflows=[EchoCommand]):
        first = await echo(client, queue, workflow_id, EchoInput(finish_at_once=True), reuse=allow)
        await client.get_workflow_handle(workflow_id).result()
        second = await echo(client, queue, workflow_id, EchoInput(finish_at_once=True), reuse=allow)
    assert second.updates == 1 and second.run_id != first.run_id


async def test_an_update_slower_than_the_deadline_is_still_accepting(
    client: Client, queue: str
) -> None:
    workflow_id = f"echo-{uuid.uuid4().hex}"
    async with Worker(client, task_queue=queue, workflows=[EchoCommand]):
        # Slower than the outer bound: `rpc_timeout` alone does not end the call, since
        # the SDK polls again when the server answers a poll with no outcome (#1095 CI).
        slow = EchoInput(delay_s=CONNECT_MARGIN_SECONDS + 2)
        with pytest.raises(CommandStillAcceptingError):
            await echo(client, queue, workflow_id, slow, deadline=timedelta(seconds=0.3))
        # The execution goes on, and the same request attaches to it.
        again = await echo(client, queue, workflow_id, slow)
        await client.get_workflow_handle(workflow_id).signal("finish")
    assert again.updates == 2


async def test_an_unreachable_temporal_is_unavailable_within_the_deadline(queue: str) -> None:
    """The lazy client's first connect retries for minutes; a route must not (§4.2)."""
    # Here, not at the top: the workflow sandbox re-imports this module.
    from scadbuddy.workflows.client import connect_lazily

    began = time.monotonic()
    with pytest.raises(TemporalUnavailableError):
        await echo(
            connect_lazily("127.0.0.1:1", "default"),
            queue,
            "echo-unreachable",
            deadline=timedelta(seconds=1),
        )
    assert time.monotonic() - began < 10


async def test_an_unavailable_response_may_follow_a_start(queue: str) -> None:
    """A connected client's `UNAVAILABLE` is a `tonic::Status` (temporalio 1.33.0,
    `temporalio/bridge/src/client.rs` `rpc_resp`), the same for a refused reconnect and
    a stream reset after the start was persisted, and sdk-core retries it
    (`crates/client/src/retry.rs` `RETRYABLE_ERROR_CODES`): Temporal is unavailable, but
    a start may exist (review #1316 (9) 1a)."""

    class Refusing:
        async def execute_update_with_start_workflow(self, *args: Any, **kwargs: Any) -> Any:
            raise RPCError("connection reset", RPCStatusCode.UNAVAILABLE, b"")

    with pytest.raises(TemporalUnavailableError) as raised:
        await echo(cast(Client, Refusing()), queue, "echo-unavailable")
    assert not isinstance(raised.value, TemporalUnreachableError)


async def test_only_a_failed_connect_says_nothing_started(queue: str) -> None:
    """A lazy client's first connect runs before any request is written
    (`_BridgeServiceClient._rpc_call` in temporalio 1.33.0's `service.py`), and fails as
    the bridge's `Failed client connect` (`bridge/src/client.rs` `connect_client`): the
    one failure that proves nothing started (review #1316 (9) 1a)."""

    class NeverConnects:
        async def execute_update_with_start_workflow(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Failed client connect: connection refused")

    with pytest.raises(TemporalUnreachableError):
        await echo(cast(Client, NeverConnects()), queue, "echo-never-connects")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError(), TemporalUnavailableError),
        (RuntimeError("Failed client connect: connection refused"), TemporalUnreachableError),
        (RPCError("reset", RPCStatusCode.UNAVAILABLE, b""), TemporalUnavailableError),
        (RPCError("slow", RPCStatusCode.DEADLINE_EXCEEDED, b""), TemporalBusyError),
        (RPCError("limit", RPCStatusCode.RESOURCE_EXHAUSTED, b""), TemporalBusyError),
        (RPCError("cancelled", RPCStatusCode.CANCELLED, b""), TemporalBusyError),
        (RPCError("internal", RPCStatusCode.INTERNAL, b""), TemporalBusyError),
        (namespace_not_found_error(), TemporalBusyError),
        (RPCError("no execution", RPCStatusCode.NOT_FOUND, b""), TemporalRefusedError),
        (RPCError("denied", RPCStatusCode.PERMISSION_DENIED, b""), TemporalRefusedError),
        (RPCError("who", RPCStatusCode.UNAUTHENTICATED, b""), TemporalRefusedError),
        (RPCError("bad", RPCStatusCode.INVALID_ARGUMENT, b""), TemporalRefusedError),
    ],
)
def test_temporal_failure_is_the_one_reading_of_a_failed_call(
    error: BaseException, expected: type[Exception]
) -> None:
    """Review #1316 (11) 2, 5; (12) 1: the routes and the reconcilers read a failed call
    the same way: unreachable, unavailable or busy (each worth sending again) or refused."""
    failure = temporal_failure(error, "x")
    assert type(failure) is expected


def test_temporal_failure_leaves_a_failure_not_about_temporal() -> None:
    assert temporal_failure(RuntimeError("a bug"), "x") is None
    assert temporal_failure(ValueError("a bug"), "x") is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (RPCStatusCode.RESOURCE_EXHAUSTED, TemporalBusyError),
        (RPCStatusCode.PERMISSION_DENIED, TemporalRefusedError),
    ],
)
async def test_start_command_raises_what_temporal_failure_reads(
    queue: str, code: RPCStatusCode, expected: type[Exception]
) -> None:
    class Failing:
        async def execute_update_with_start_workflow(self, *args: Any, **kwargs: Any) -> Any:
            raise RPCError("no", code, b"")

    with pytest.raises(expected):
        await echo(cast(Client, Failing()), queue, "echo-failing")


class Proxy:
    """A TCP proxy to Temporal that can be cut, as a frontend going down would be."""

    def __init__(self, target: str) -> None:
        host, port = target.rsplit(":", 1)
        self.target = (host, int(port))
        self.writers: list[asyncio.StreamWriter] = []

    async def start(self) -> str:
        self.server = await asyncio.start_server(self._pipe, "127.0.0.1", 0)
        return f"127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_reader, up_writer = await asyncio.open_connection(*self.target)
        self.writers += [writer, up_writer]

        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                dst.close()

        await asyncio.gather(copy(reader, up_writer), copy(up_reader, writer))

    async def cut(self) -> None:
        self.server.close()
        for writer in self.writers:
            writer.close()
        await self.server.wait_closed()


async def test_temporal_lost_after_connecting_is_unavailable_not_still_accepting(
    client: Client, queue: str
) -> None:
    """Once connected, an outage surfaces as the Update's RPC timeout: the route must say
    Temporal is unavailable (#1052 review). The connection had carried requests, so it
    is not the connect failure that proves nothing started (review #1316 (9) 1a)."""
    proxy = Proxy(current_address(client))
    via = await Client.connect(await proxy.start(), data_converter=pydantic_data_converter)
    async with Worker(client, task_queue=queue, workflows=[EchoCommand]):
        await echo(via, queue, f"echo-{uuid.uuid4().hex}", EchoInput(finish_at_once=True))
        await proxy.cut()
        with pytest.raises(TemporalUnavailableError) as raised:
            await echo(via, queue, f"echo-{uuid.uuid4().hex}", deadline=timedelta(seconds=4))
    assert not isinstance(raised.value, TemporalUnreachableError)


async def test_an_update_slower_than_the_default_deadline_is_still_accepting(
    client: Client, queue: str
) -> None:
    """At the default deadline the Update's RPC may outlive `rpc_timeout`, so the outer
    bound fires first: an execution that exists is still accepting, not Temporal down."""
    workflow_id = f"echo-{uuid.uuid4().hex}"
    async with Worker(client, task_queue=queue, workflows=[EchoCommand]):
        with pytest.raises(CommandStillAcceptingError):
            await echo(client, queue, workflow_id, EchoInput(delay_s=20))
        await client.get_workflow_handle(workflow_id).terminate()


async def test_an_execution_ended_before_its_update_answered_is_a_closed_command(
    client: Client, queue: str
) -> None:
    """Review #1061 1c: terminated (or cancelled) while the Update waits, the command
    is a typed error the route answers as a problem, never a bare 500."""
    workflow_id = f"echo-{uuid.uuid4().hex}"
    async with Worker(client, task_queue=queue, workflows=[EchoCommand]):
        pending = asyncio.create_task(echo(client, queue, workflow_id, EchoInput(delay_s=30)))
        await asyncio.sleep(1)
        await client.get_workflow_handle(workflow_id).terminate("an operator ended it")
        with pytest.raises(CommandClosedError):
            await pending
