"""`temporal_failure` (review #1316 (11) 5, (12) 1): the one reading of a failed
Temporal call the routes and the reconcilers share, and what `start_command` and
`namespace_retention` raise for it. Stub clients only, so it runs without a Temporal
(review #1316 (13) 5a)."""

from __future__ import annotations

import asyncio
import gc
from typing import Any, cast

import pytest
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.workflows.commands import (
    CommandClosingError,
    TemporalBusyError,
    TemporalRefusedError,
    TemporalUnavailableError,
    TemporalUnreachableError,
    _retrieve_start_error,
    namespace_retention,
    start_command,
    temporal_failure,
)
from tests.support.temporal import namespace_not_found_error


async def call(client: object) -> object:
    return await start_command(
        cast(Client, client),
        "EchoCommand",
        {},
        id="echo-stub",
        task_queue="unserved",
        update="accepted",
        result_type=dict,
        reuse=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
    )


async def test_an_unavailable_response_may_follow_a_start() -> None:
    """A connected client's `UNAVAILABLE` is a `tonic::Status` (temporalio 1.33.0,
    `temporalio/bridge/src/client.rs` `rpc_resp`), the same for a refused reconnect and
    a stream reset after the start was persisted, and sdk-core retries it
    (`crates/client/src/retry.rs` `RETRYABLE_ERROR_CODES`): Temporal is unavailable, but
    a start may exist (review #1316 (9) 1a)."""

    class Refusing:
        async def execute_update_with_start_workflow(self, *args: Any, **kwargs: Any) -> Any:
            raise RPCError("connection reset", RPCStatusCode.UNAVAILABLE, b"")

    with pytest.raises(TemporalUnavailableError) as raised:
        await call(Refusing())
    assert not isinstance(raised.value, TemporalUnreachableError)


async def test_only_a_failed_connect_says_nothing_started() -> None:
    """A lazy client's first connect runs before any request is written
    (`_BridgeServiceClient._rpc_call` in temporalio 1.33.0's `service.py`), and fails as
    the bridge's `Failed client connect` (`bridge/src/client.rs` `connect_client`): the
    one failure that proves nothing started (review #1316 (9) 1a)."""

    class NeverConnects:
        async def execute_update_with_start_workflow(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Failed client connect: connection refused")

    with pytest.raises(TemporalUnreachableError):
        await call(NeverConnects())


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
    code: RPCStatusCode, expected: type[Exception]
) -> None:
    class Failing:
        async def execute_update_with_start_workflow(self, *args: Any, **kwargs: Any) -> Any:
            raise RPCError("no", code, b"")

    with pytest.raises(expected):
        await call(Failing())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            RPCError(
                "workflow update was aborted by closing workflow", RPCStatusCode.NOT_FOUND, b""
            ),
            CommandClosingError,
        ),
        (namespace_not_found_error(), TemporalBusyError),
    ],
)
async def test_start_command_reads_an_execution_not_found_as_closing(
    error: RPCError, expected: type[Exception]
) -> None:
    """#1799: under ``USE_EXISTING`` an execution NOT_FOUND is the execution closing
    under the Update, never a refusal; a missing namespace stays what
    `temporal_failure` reads."""

    class Failing:
        async def execute_update_with_start_workflow(self, *args: Any, **kwargs: Any) -> Any:
            raise error

    with pytest.raises(expected):
        await call(Failing())


async def test_a_failed_start_leaves_no_unretrieved_future_behind() -> None:
    """#2065: temporalio sets a failed start's error on the operation's handle future
    as well as raising it (``client/_client.py`` ``on_start_error``). The raised one is
    handled, so the future's copy must be read too, or asyncio logs it as "Future
    exception was never retrieved" with nobody's traceback."""
    logged: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: logged.append(context))

    class TimingOut:
        async def execute_update_with_start_workflow(
            self, *args: Any, start_workflow_operation: Any, **kwargs: Any
        ) -> Any:
            error = RPCError("Timeout expired", RPCStatusCode.UNAVAILABLE, b"")
            start_workflow_operation._workflow_handle.set_exception(error)
            raise error

    try:
        with pytest.raises(TemporalUnavailableError):
            await call(TimingOut())
        gc.collect()
    finally:
        loop.set_exception_handler(previous)
    assert [c["message"] for c in logged] == []


async def test_a_start_error_set_after_the_command_stopped_waiting_is_read_too() -> None:
    """#2190: when the command's own timeout fires before temporalio sets the start's
    error, the future is not done yet in the ``finally``; its error is still read when it
    arrives, so asyncio logs nothing."""
    logged: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: logged.append(context))
    started: list[Any] = []

    class TimingOutFirst:
        async def execute_update_with_start_workflow(
            self, *args: Any, start_workflow_operation: Any, **kwargs: Any
        ) -> Any:
            started.append(start_workflow_operation)
            raise RPCError("Timeout expired", RPCStatusCode.UNAVAILABLE, b"")

    try:
        with pytest.raises(TemporalUnavailableError):
            await call(TimingOutFirst())
        started[0]._workflow_handle.set_exception(RPCError("late", RPCStatusCode.UNAVAILABLE, b""))
        started.clear()
        await asyncio.sleep(0)
        gc.collect()
    finally:
        loop.set_exception_handler(previous)
    assert [c["message"] for c in logged] == []


def test_an_sdk_without_the_private_handle_is_skipped() -> None:
    """#2190: the handle is private; a temporalio that renames it must not break every
    command with an AttributeError from the ``finally``."""
    _retrieve_start_error(cast(Any, object()))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (RPCStatusCode.UNAVAILABLE, TemporalUnavailableError),
        (RPCStatusCode.PERMISSION_DENIED, TemporalRefusedError),
    ],
)
async def test_namespace_retention_raises_what_temporal_failure_reads(
    code: RPCStatusCode, expected: type[Exception]
) -> None:
    """Review #1316 (12) 1: the settings check reads a failed `DescribeNamespace` as the
    routes and the reconcilers read a failed call."""

    class Service:
        async def describe_namespace(self, *args: Any, **kwargs: Any) -> Any:
            raise RPCError("no", code, b"")

    class Failing:
        namespace = "default"
        workflow_service = Service()

    with pytest.raises(expected) as raised:
        await namespace_retention(cast(Client, Failing()))
    assert type(raised.value) is expected
