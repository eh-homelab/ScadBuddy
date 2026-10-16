# backend/scadbuddy/telemetry/forwarder.py
"""The browser trace relay's forwarding to the collector (spec 2026-10-01 §5.2).

An accepted batch goes on a bounded in-memory queue and the browser is answered at
once; one task posts the queue to the collector, so the browser never waits on it.

- **No retries.** A failed post (unreachable, a timeout, any non-2xx, any error the
  transport raises) drops its batch.
  Retrying and buffering are alloy's job, and the browser has already gone.
- **A full queue** drops the new batch at once; the browser still gets its 204.
- **Shutdown** (`running` exiting, in the app's lifespan): new batches are refused
  (`closing`, a 503), then the queue is drained for at most :data:`DRAIN_SECONDS` in
  all. A post already in flight is left to finish within that budget and cancelled
  only past it. Each later post's timeout is whatever remains of that budget, the first post that
  fails ends the drain (an unreachable collector would fail every later post the same
  way), and the rest is dropped as ``shutdown``.
  A shutdown whose own wait is cancelled (a second SIGTERM) cancels the post in flight
  and counts what is left as ``shutdown`` too.
- **A dead task** (an error outside a post) is logged when it dies, and from then on the
  route answers 503 and counts the batch ``failed`` rather than letting the queue fill.
- **Visibility:** every outcome is counted in ``scadbuddy_trace_relay_batches_total``,
  and failures log one warning a minute at most, naming the status or the error class.

The httpx client here is never passed to ``HTTPXClientInstrumentor.instrument_client``
(spec §4, §6): traced, the relay would trace its own exports.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Final

import httpx

from scadbuddy.core.metrics import Metrics, TraceRelayOutcome
from scadbuddy.telemetry.payload import MAX_PREPARED_BYTES

logger = logging.getLogger(__name__)

#: 64 batches of at most `payload.prepare`'s cap each, what is queued (#1137).
MAX_QUEUED_BATCHES: Final = 64
MAX_QUEUED_BYTES: Final = MAX_QUEUED_BATCHES * MAX_PREPARED_BYTES
FORWARD_TIMEOUT: Final = 5.0
DRAIN_SECONDS: Final = 5.0
WARNING_INTERVAL: Final = 60.0


class TraceForwarder:
    def __init__(
        self,
        *,
        metrics: Metrics,
        target: tuple[str, dict[str, str]] | None,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: ssl.SSLContext | bool = True,
        clock: Callable[[], float] = time.monotonic,
        forward_timeout: float = FORWARD_TIMEOUT,
        drain_seconds: float = DRAIN_SECONDS,
    ) -> None:
        self.metrics = metrics
        self.target = target
        self._transport = transport
        self._verify = verify
        self._clock = clock
        self._forward_timeout = forward_timeout
        self._drain_seconds = drain_seconds
        #: Set once shutdown has begun: the route answers 503 from then on.
        self.closing = False
        #: Set when the forwarding task died: the route answers 503 from then on.
        self.dead = False
        self._pending: deque[bytes] = deque()
        self._pending_bytes = 0
        #: Set when a batch is queued; made per run, so it is bound to that run's loop.
        self._wake: asyncio.Event | None = None
        self._client: httpx.AsyncClient | None = None
        self._warned_at: float | None = None

    @property
    def off(self) -> bool:
        return self.target is None

    @property
    def url(self) -> str:
        if self.target is None:
            raise RuntimeError("tracing is off: there is no collector to forward to")
        return self.target[0]

    @property
    def headers(self) -> dict[str, str]:
        if self.target is None:
            raise RuntimeError("tracing is off: there is no collector to forward to")
        return self.target[1]

    def offer(self, batch: bytes) -> None:
        """Queue ``batch`` for the collector. When the queue is full it is dropped and
        counted; the caller answers the browser 204 either way. Nothing is queued when
        tracing is off (the route answers that case itself) or once shutdown has begun,
        which is counted ``shutdown``."""
        if self.off:
            return
        if self.closing:
            self.count("shutdown")
            return
        if self.dead:
            self.count("failed")
            return
        if (
            len(self._pending) >= MAX_QUEUED_BATCHES
            or self._pending_bytes + len(batch) > MAX_QUEUED_BYTES
        ):
            self.count("queue_full")
            return
        self._pending.append(batch)
        self._pending_bytes += len(batch)
        if self._wake is not None:
            self._wake.set()

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """The forwarding task, for as long as the app runs (`Component.run`)."""
        if self.off:
            yield
            return
        self.closing = False
        self.dead = False
        self._wake = asyncio.Event()
        self._client = httpx.AsyncClient(
            transport=self._transport, timeout=self._forward_timeout, verify=self._verify
        )
        task = asyncio.create_task(self._forward(self._wake))
        task.add_done_callback(self._died)
        try:
            yield
        finally:
            self.closing = True
            deadline = self._clock() + self._drain_seconds
            self._wake.set()
            try:
                # The post in flight finishes within the budget; cut off only past it.
                # Not `await task`: that would swallow a cancellation of the lifespan.
                await asyncio.wait({task}, timeout=max(deadline - self._clock(), 0))
                if not task.done():
                    task.cancel()
                    await asyncio.wait({task})
                await self._drain(deadline)
            except asyncio.CancelledError:
                # This wait was cut short: end the post in flight (`_post` counts it
                # ``shutdown``), count what is still queued, and go.
                task.cancel()
                await asyncio.wait({task})
                self._drop_pending()
                raise
            finally:
                await self._client.aclose()
                self._client = None
                self._wake = None
            failure = None if task.cancelled() else task.exception()
            if failure is not None:
                raise failure

    def _died(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is None:
            return
        self.dead = True
        logger.error(
            "the browser trace forwarder died; the relay refuses batches until the process"
            " restarts",
            exc_info=task.exception(),
        )

    def _pop(self) -> bytes:
        batch = self._pending.popleft()
        self._pending_bytes -= len(batch)
        return batch

    async def _forward(self, wake: asyncio.Event) -> None:
        while True:
            # What was queued before the run started goes first. Once closing, the
            # drain posts what is left, within its budget.
            while self._pending and not self.closing:
                await self._post(self._pop(), self._forward_timeout)
            if self.closing:
                return
            wake.clear()
            await wake.wait()

    async def _drain(self, deadline: float) -> None:
        while self._pending:
            remaining = deadline - self._clock()
            if remaining <= 0 or not await self._post(self._pop(), remaining):
                break
        self._drop_pending()

    def _drop_pending(self) -> None:
        while self._pending:
            self._pop()
            self.count("shutdown")

    async def _post(self, batch: bytes, timeout: float) -> bool:
        """One post, bounded by ``timeout`` whatever the transport does; True when the
        collector took it. A post cancelled by shutdown counts as ``shutdown``."""
        client = self._client
        if client is None:
            raise RuntimeError("the forwarder is not running")
        # Case-insensitive, so a ``content-type`` among the variable's headers is
        # replaced rather than sent beside this one.
        headers = httpx.Headers(self.headers)
        headers["Content-Type"] = "application/json"
        try:
            async with asyncio.timeout(timeout):
                response = await client.post(self.url, content=batch, headers=headers)
        except asyncio.CancelledError:
            self.count("shutdown")
            raise
        except Exception as error:
            # Anything else a transport raises too: one bad post must not end the task,
            # or every later batch would wait in the queue until it is full.
            self._failed(type(error).__name__)
            return False
        if not response.is_success:
            self._failed(f"http-{response.status_code}")
            return False
        self.count("forwarded")
        return True

    def count(self, outcome: TraceRelayOutcome) -> None:
        self.metrics.trace_relay_batches.labels(outcome).inc()

    def _failed(self, reason: str) -> None:
        self.count("failed")
        now = self._clock()
        if self._warned_at is None or now - self._warned_at >= WARNING_INTERVAL:
            self._warned_at = now
            logger.warning(
                "could not forward browser spans to the collector; dropped the batch",
                extra={"reason": reason},
            )


__all__ = [
    "DRAIN_SECONDS",
    "FORWARD_TIMEOUT",
    "MAX_QUEUED_BATCHES",
    "MAX_QUEUED_BYTES",
    "TraceForwarder",
]
