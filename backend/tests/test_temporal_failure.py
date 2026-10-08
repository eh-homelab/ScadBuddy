"""`temporal_failure` (review #1316 (11) 5, (12) 1): the one reading of a failed
Temporal call the routes and the reconcilers share, and what `start_command` and
`namespace_retention` raise for it. Stub clients only, so it runs without a Temporal
(review #1316 (13) 5a)."""

from __future__ import annotations

from typing import Any, cast

import pytest
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.workflows.commands import (
    TemporalBusyError,
    TemporalRefusedError,
    TemporalUnavailableError,
    TemporalUnreachableError,
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
