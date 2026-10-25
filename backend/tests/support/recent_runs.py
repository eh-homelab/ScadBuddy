"""Whether a process's `RecentRuns` hears its bus (#4541): a run kept for a subject is
read again once a ``print.run`` event of it is published through the bus."""

from __future__ import annotations

import asyncio
import time
from types import ModuleType
from typing import Any

import pytest

from scadbuddy.bambuddy.runs import PrintRun, RecentRuns
from scadbuddy.core.events import EventBus, PrintRunEvent

SUBJECT = "o" * 32


class _CountingRuns:
    def __init__(self) -> None:
        self.calls = 0

    async def latest_for_output(self, output_id: str) -> PrintRun | None:
        self.calls += 1
        return None


def record_recent_runs(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> list[RecentRuns]:
    """Every `RecentRuns` ``module`` builds from now on, in order."""
    built: list[RecentRuns] = []

    class Recorded(RecentRuns):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(module, "RecentRuns", Recorded)
    return built


async def assert_hears(recent: RecentRuns, events: EventBus, timeout: float = 5.0) -> None:
    """``recent``, primed and holding its run, reads it again once ``events`` (started)
    carries the subject's ``print.run``."""
    # Swaps in a counting store: this checks the bus wiring, not the production store.
    store = _CountingRuns()
    recent.runs = store  # type: ignore[assignment]
    recent.ttl = timeout * 10
    await recent.latest(SUBJECT)
    await recent.latest(SUBJECT)
    assert store.calls == 1  # kept
    events.publish(PrintRunEvent(output_id=SUBJECT, slug="s", run_id="r2"))
    deadline = time.monotonic() + timeout
    while store.calls == 1:
        assert time.monotonic() < deadline, "the print.run event never dropped the kept run"
        await asyncio.sleep(0.02)
        await recent.latest(SUBJECT)
