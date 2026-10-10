"""`main._sweep_assets_logged`: which of the store's steps each sweep runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy import main
from scadbuddy.store.bambuddy import BambuddyContentBackend


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


async def test_the_sweep_drops_then_reconciles_and_backfills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Schedule's sweep is the only one (review #1095 1): it drops what it removed,
    then converges with the store."""
    remote = _Remote()
    monkeypatch.setattr(main, "sweep_assets", lambda state: ["a1"])
    state = SimpleNamespace(store=SimpleNamespace(remote_assets=remote), assets=object())
    await main._sweep_assets_logged(state)  # type: ignore[arg-type]
    assert remote.calls == ["drop", "reconcile", "backfill"]


@pytest.mark.parametrize("content", [False, True])
async def test_both_blob_sweeps_keep_an_unreferenced_piece_for_the_job_ttl(
    monkeypatch: pytest.MonkeyPatch, content: bool
) -> None:
    """Pieces and snapshots are a job's data on either backend, so both sweeps take
    `job_ttl`; `asset_sweep_grace` is the uploads' knob."""
    graces: list[int] = []

    def sweep_blobs(blobs: Any, refs: Any, *, grace: int) -> list[str]:
        graces.append(grace)
        return []

    async def sweep_content(store: Any, refs: Any, *, grace: int) -> list[str]:
        graces.append(grace)
        return []

    monkeypatch.setattr(main, "sweep_blobs", sweep_blobs)
    monkeypatch.setattr(main, "sweep_content", sweep_content)
    state = SimpleNamespace(
        store=SimpleNamespace(
            content=SimpleNamespace(backend=object()) if content else None, blobs=object()
        ),
        blobs=object(),
        refs=object(),
        config=SimpleNamespace(job_ttl=86400, asset_sweep_grace=7 * 86400),
    )
    await main._sweep_blobs_logged(state)  # type: ignore[arg-type]
    assert graces == [86400]


class _Bambuddy(BambuddyContentBackend):
    """A Bambuddy backend that only records the reconcile it is asked for."""

    def __init__(self) -> None:
        self.cutoffs: list[datetime] = []

    async def sweep_unrecorded(self, *, cutoff: datetime) -> list[str]:
        self.cutoffs.append(cutoff)
        return ["600"]


@pytest.mark.parametrize("on_bambuddy", [False, True])
async def test_the_blob_sweep_reconciles_unrecorded_work_files_on_bambuddy_only(
    monkeypatch: pytest.MonkeyPatch, on_bambuddy: bool
) -> None:
    """#701: on Bambuddy the sweep also deletes the Work/ files no row names that are
    older than `job_ttl`; another backend has no such files to find."""

    async def sweep_content(store: Any, refs: Any, *, grace: int) -> list[str]:
        return []

    monkeypatch.setattr(main, "sweep_content", sweep_content)
    backend: Any = _Bambuddy() if on_bambuddy else object()
    state = SimpleNamespace(
        store=SimpleNamespace(content=SimpleNamespace(backend=backend), blobs=object()),
        refs=object(),
        config=SimpleNamespace(job_ttl=86400),
    )
    before = datetime.now(UTC)
    await main._sweep_blobs_logged(state)  # type: ignore[arg-type]
    if on_bambuddy:
        [cutoff] = backend.cutoffs
        assert (
            before - timedelta(seconds=86400)
            <= cutoff
            <= datetime.now(UTC) - timedelta(seconds=86400)
        )
