# backend/tests/test_trace_relay_forwarding.py
"""telemetry/forwarder.py: the relay's queue and its posts (spec 2026-10-01 §5.2)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine

import httpx
import pytest

from scadbuddy.core.metrics import Metrics
from scadbuddy.telemetry import forwarder as forwarder_module
from scadbuddy.telemetry.forwarder import (
    MAX_QUEUED_BATCHES,
    MAX_QUEUED_BYTES,
    TraceForwarder,
    relay_endpoint,
)

ENDPOINT = "http://collector.test:4318"
BATCH = b'{"resourceSpans":[]}'

type Handler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]


def outcome(metrics: Metrics, name: str) -> float:
    value = metrics.registry.get_sample_value(
        "scadbuddy_trace_relay_batches_total", {"outcome": name}
    )
    assert value is not None
    return value


def make(
    handler: Handler,
    *,
    endpoint: str | None = ENDPOINT,
    forward_timeout: float = 5.0,
    drain_seconds: float = 5.0,
) -> tuple[TraceForwarder, Metrics]:
    metrics = Metrics()
    forwarder = TraceForwarder(
        metrics=metrics,
        endpoint=endpoint,
        transport=httpx.MockTransport(handler),
        forward_timeout=forward_timeout,
        drain_seconds=drain_seconds,
    )
    return forwarder, metrics


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


async def test_an_accepted_batch_is_posted_to_the_traces_path() -> None:
    seen: list[httpx.Request] = []

    async def collector(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    forwarder, metrics = make(collector, endpoint=ENDPOINT + "/")
    async with forwarder.running():
        forwarder.offer(BATCH)
        await until(lambda: outcome(metrics, "forwarded") == 1)
    (request,) = seen
    assert str(request.url) == "http://collector.test:4318/v1/traces"
    assert request.headers["content-type"] == "application/json"
    assert request.content == BATCH
    # Never instrumented: the relay adds no trace context of its own.
    assert "traceparent" not in request.headers


@pytest.mark.parametrize("status", [400, 500, 503])
async def test_a_refused_post_drops_its_batch_and_counts_it(status: int) -> None:
    calls = 0

    async def collector(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    forwarder, metrics = make(collector)
    async with forwarder.running():
        forwarder.offer(BATCH)
        await until(lambda: outcome(metrics, "failed") == 1)
    assert calls == 1  # no retry
    assert outcome(metrics, "forwarded") == 0


async def test_an_unreachable_collector_counts_failed() -> None:
    async def collector(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    forwarder, metrics = make(collector)
    async with forwarder.running():
        forwarder.offer(BATCH)
        await until(lambda: outcome(metrics, "failed") == 1)


async def test_a_collector_that_never_answers_times_out() -> None:
    async def collector(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        return httpx.Response(200)

    forwarder, metrics = make(collector, forward_timeout=0.05)
    async with forwarder.running():
        forwarder.offer(BATCH)
        await until(lambda: outcome(metrics, "failed") == 1)


async def test_failures_warn_once_a_minute(caplog: pytest.LogCaptureFixture) -> None:
    now = [1000.0]

    async def collector(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    metrics = Metrics()
    forwarder = TraceForwarder(
        metrics=metrics,
        endpoint=ENDPOINT,
        transport=httpx.MockTransport(collector),
        clock=lambda: now[0],
    )
    caplog.set_level(logging.WARNING, logger=forwarder_module.__name__)
    async with forwarder.running():
        forwarder.offer(BATCH)
        forwarder.offer(BATCH)
        await until(lambda: outcome(metrics, "failed") == 2)
        now[0] += 61
        forwarder.offer(BATCH)
        await until(lambda: outcome(metrics, "failed") == 3)
    warnings = [r for r in caplog.records if r.name == forwarder_module.__name__]
    assert len(warnings) == 2
    assert all(getattr(r, "reason", None) == "http-502" for r in warnings)


async def test_a_full_queue_drops_the_new_batch() -> None:
    async def collector(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    forwarder, metrics = make(collector)
    # Not running: nothing is posted, so the queue only fills.
    for _ in range(MAX_QUEUED_BATCHES):
        forwarder.offer(BATCH)
    forwarder.offer(BATCH)
    assert outcome(metrics, "queue_full") == 1


async def test_the_queue_is_bounded_in_bytes_too() -> None:
    async def collector(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    forwarder, metrics = make(collector)
    forwarder.offer(b"x" * MAX_QUEUED_BYTES)
    forwarder.offer(b"x")
    assert outcome(metrics, "queue_full") == 1


async def test_shutdown_drains_the_queue_and_refuses_new_batches() -> None:
    seen: list[bytes] = []

    async def collector(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200)

    forwarder, metrics = make(collector)
    for index in range(3):
        forwarder.offer(b"%d" % index)
    # The block awaits nothing, so the forwarding task never ran: the exit drains all three.
    async with forwarder.running():
        assert not forwarder.closing
    assert forwarder.closing
    assert seen == [b"0", b"1", b"2"]
    assert outcome(metrics, "forwarded") == 3
    assert outcome(metrics, "shutdown") == 0


async def test_a_dead_collector_ends_the_drain_at_its_first_failure() -> None:
    calls = 0

    async def collector(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused", request=request)

    forwarder, metrics = make(collector)
    for _ in range(5):
        forwarder.offer(BATCH)
    started = time.monotonic()
    async with forwarder.running():
        pass
    assert time.monotonic() - started < 1
    assert calls == 1
    assert outcome(metrics, "failed") == 1
    assert outcome(metrics, "shutdown") == 4


async def test_a_hung_collector_ends_the_drain_within_the_budget() -> None:
    async def collector(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        return httpx.Response(200)

    forwarder, metrics = make(collector, drain_seconds=0.2)
    for _ in range(3):
        forwarder.offer(BATCH)
    started = time.monotonic()
    async with forwarder.running():
        pass
    assert time.monotonic() - started < 1
    assert outcome(metrics, "failed") == 1
    assert outcome(metrics, "shutdown") == 2


async def test_a_post_cut_off_by_shutdown_counts_as_shutdown() -> None:
    entered = asyncio.Event()

    async def collector(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.sleep(60)
        return httpx.Response(200)

    forwarder, metrics = make(collector, drain_seconds=0.1)
    async with forwarder.running():
        forwarder.offer(BATCH)
        await entered.wait()
    assert outcome(metrics, "shutdown") == 1
    assert outcome(metrics, "failed") == 0


async def test_off_starts_nothing_and_posts_nothing() -> None:
    calls = 0

    async def collector(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    forwarder, _ = make(collector, endpoint=None)
    assert forwarder.off
    async with forwarder.running():
        pass
    assert calls == 0


def test_the_endpoint_follows_the_backend_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert relay_endpoint() is None
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    assert relay_endpoint() == ENDPOINT
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert relay_endpoint() is None


async def test_off_queues_nothing_and_counts_nothing() -> None:
    async def collector(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    forwarder, metrics = make(collector, endpoint=None)
    forwarder.offer(BATCH)
    assert not forwarder._pending
    assert all(
        outcome(metrics, name) == 0 for name in ("forwarded", "failed", "queue_full", "shutdown")
    )


async def test_a_batch_offered_while_closing_counts_shutdown_and_is_not_queued() -> None:
    async def collector(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    forwarder, metrics = make(collector)
    async with forwarder.running():
        pass
    forwarder.offer(BATCH)
    assert not forwarder._pending
    assert outcome(metrics, "shutdown") == 1


async def test_the_client_timeout_is_the_forward_timeout() -> None:
    async def collector(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    forwarder, _ = make(collector, forward_timeout=12.0)
    async with forwarder.running():
        assert forwarder._client is not None
        assert forwarder._client.timeout == httpx.Timeout(12.0)


async def _restart_and_forward() -> tuple[list[bytes], Metrics]:
    seen: list[bytes] = []

    async def collector(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200)

    forwarder, metrics = make(collector)
    async with forwarder.running():
        pass
    async with forwarder.running():
        forwarder.offer(BATCH)
        await until(lambda: outcome(metrics, "forwarded") == 1)
    return seen, metrics


async def test_a_forwarder_restarted_on_the_same_loop_forwards() -> None:
    seen, _ = await _restart_and_forward()
    assert seen == [BATCH]


def test_a_forwarder_restarted_on_a_fresh_loop_forwards() -> None:
    forwarder_seen: list[bytes] = []

    async def collector(request: httpx.Request) -> httpx.Response:
        forwarder_seen.append(request.content)
        return httpx.Response(200)

    forwarder, metrics = make(collector)

    async def first() -> None:
        async with forwarder.running():
            pass

    async def second() -> None:
        async with forwarder.running():
            forwarder.offer(BATCH)
            await until(lambda: outcome(metrics, "forwarded") == 1)

    asyncio.run(first())
    asyncio.run(second())
    assert forwarder_seen == [BATCH]
