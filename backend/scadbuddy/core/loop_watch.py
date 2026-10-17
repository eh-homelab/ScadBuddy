"""A watch on a worker's event loop (#3788).

Every Temporal workflow task and every async activity a worker runs starts on its
event loop. A loop held by a blocking call times workflow tasks out at 10 s while the
SDK's deadlock detector, which runs on that same loop, says nothing; CPU can read idle
throughout. The watch is a thread that posts a no-op to the loop every ``interval``.
How late each one runs is ``scadbuddy_event_loop_lag_seconds``. One that has not run
after ``threshold`` is a stall: it is counted, and logged at once, while the loop is
still held, with every thread's stack, so the log names the call that holds it.

A watch thread that itself wakes late was kept from running too: something held the
GIL, not only the loop. Its stall log says how late it woke.

The loop also hands a no-op to its default executor, the pool behind every
``asyncio.to_thread``, one at a time. How long it waited for a thread is
``scadbuddy_default_executor_wait_seconds``; one still waiting after ``threshold`` is
logged the same way. An activity whose first await is a ``to_thread`` (``project``)
does not start while every thread of that pool is held, though the loop runs on.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from types import TracebackType

from scadbuddy.core.metrics import Metrics

logger = logging.getLogger(__name__)

#: How often a no-op is posted, and how late one runs before it is a stall. The
#: threshold sits under the SDK's 2 s deadlock bound and far under the 10 s
#: workflow-task timeout, so a stall is logged before either fires.
INTERVAL = 0.5
THRESHOLD = 1.0


def _stacks(loop_thread: int) -> str:
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    frames = sys._current_frames()
    # The loop's own thread first: it is the one that is held.
    order = sorted(frames, key=lambda ident: ident != loop_thread)
    return "\n".join(
        f"--- thread {names.get(ident, ident)}"
        f"{' (event loop)' if ident == loop_thread else ''}:\n"
        + "".join(traceback.format_stack(frames[ident]))
        for ident in order
    )


class LoopWatch:
    """``async with LoopWatch(metrics):`` watches the running loop until it exits."""

    def __init__(
        self, metrics: Metrics, *, threshold: float = THRESHOLD, interval: float = INTERVAL
    ) -> None:
        self.metrics = metrics
        self.threshold = threshold
        self.interval = interval
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        #: The executor no-op in flight: when it was handed over, and whether it ran.
        self._probe: tuple[float, threading.Event] | None = None
        self._probe_reported = False

    async def __aenter__(self) -> LoopWatch:
        loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._watch, args=(loop, threading.get_ident()), name="loop-watch", daemon=True
        )
        self._thread.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stopping.set()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join)

    def _watch(self, loop: asyncio.AbstractEventLoop, loop_thread: int) -> None:
        while True:
            asleep = time.monotonic()
            if self._stopping.wait(self.interval):
                return
            woke_late = time.monotonic() - asleep - self.interval
            ran = threading.Event()
            posted = time.monotonic()

            def beat(posted: float = posted, ran: threading.Event = ran) -> None:
                self.metrics.event_loop_lag.observe(time.monotonic() - posted)
                ran.set()
                self._hand_probe(loop)

            try:
                loop.call_soon_threadsafe(beat)
            except RuntimeError:
                return  # the loop is closed
            if ran.wait(self.threshold):
                self._check_probe(loop_thread)
                continue
            self.metrics.event_loop_stalls.inc()
            logger.warning(
                "the event loop has not run for %.1f s (this watch woke %.1f s late);"
                " every thread's stack:\n%s",
                time.monotonic() - posted,
                max(0.0, woke_late),
                _stacks(loop_thread),
            )
            while not ran.wait(self.interval):
                if self._stopping.is_set() or loop.is_closed():
                    return
            logger.warning("the event loop ran again after %.1f s", time.monotonic() - posted)

    def _hand_probe(self, loop: asyncio.AbstractEventLoop) -> None:
        """On the loop: a no-op to the default executor, unless one still waits."""
        if self._probe is not None:
            return
        handed, started = time.monotonic(), threading.Event()

        def probe() -> None:
            self.metrics.default_executor_wait.observe(time.monotonic() - handed)
            started.set()

        self._probe = (handed, started)
        try:
            loop.run_in_executor(None, probe)
        except RuntimeError:
            self._probe = None  # the executor is shut down

    def _check_probe(self, loop_thread: int) -> None:
        if self._probe is None:
            return
        handed, started = self._probe
        if started.is_set():
            if self._probe_reported:
                logger.warning(
                    "the default executor started a task again after %.1f s",
                    time.monotonic() - handed,
                )
            self._probe, self._probe_reported = None, False
            return
        if self._probe_reported or time.monotonic() - handed < self.threshold:
            return
        self._probe_reported = True
        self.metrics.default_executor_stalls.inc()
        logger.warning(
            "the default executor has not started a task for %.1f s (asyncio.to_thread"
            " waits for a free thread); every thread's stack:\n%s",
            time.monotonic() - handed,
            _stacks(loop_thread),
        )
