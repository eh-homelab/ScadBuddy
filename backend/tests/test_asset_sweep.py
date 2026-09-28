"""`main._sweep_assets_logged`: which of the store's steps each sweep runs."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy import main


class _Remote:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def clock(self) -> datetime:
        return datetime(2026, 9, 28, tzinfo=UTC)

    async def drop(self, ids: list[str], *, cutoff: datetime) -> list[str]:
        self.calls.append("drop")
        return ids

    async def reconcile(self, store: Any, *, cutoff: datetime) -> list[str]:
        self.calls.append("reconcile")
        return []

    async def backfill(self, store: Any) -> int:
        self.calls.append("backfill")
        return 0


@pytest.mark.parametrize(
    ("converge", "calls"),
    [(False, ["drop"]), (True, ["drop", "reconcile", "backfill"])],
)
async def test_only_the_periodic_sweep_reconciles_and_backfills(
    monkeypatch: pytest.MonkeyPatch, converge: bool, calls: list[str]
) -> None:
    """The boot's sweep drops what it removed but leaves the long Bambuddy passes to
    the periodic sweep, so an unreachable Bambuddy never holds up the start."""
    remote = _Remote()
    monkeypatch.setattr(main, "sweep_assets", lambda state: ["a1"])
    state = SimpleNamespace(store=SimpleNamespace(remote_assets=remote), assets=object())
    await main._sweep_assets_logged(state, converge=converge)  # type: ignore[arg-type]
    assert remote.calls == calls
