"""`main._sweep_assets_logged`: which of the store's steps each sweep runs."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy import main
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.store.bambuddy import BambuddyContentBackend
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.s3 import S3ContentBackend


class _Remote:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def clock(self) -> datetime:
        return datetime(2026, 9, 28, tzinfo=UTC)

    async def mark_swept(self, ids: list[str], *, cutoff: datetime) -> None:
        self.calls.append(f"mark {','.join(ids)}")

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
    monkeypatch.setattr(main, "sweep_assets", lambda state, stop=None: ["a1"])
    state = SimpleNamespace(store=SimpleNamespace(remote_assets=remote), assets=object())
    await main._sweep_assets_logged(state)  # type: ignore[arg-type]
    assert remote.calls == ["drop", "reconcile", "backfill"]


async def test_a_stopped_sweep_marks_what_it_removed_and_converges_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2140: a sweep the shutdown stopped still hands what it removed to the store,
    marked for the next sweep's `reconcile` (the only thing that retries a drop), and
    skips the round trips of the drop, reconcile and backfill."""
    remote = _Remote()
    stop = threading.Event()

    def sweeping(state: Any, stop_: threading.Event | None = None) -> list[str]:
        assert stop_ is stop
        stop.set()
        return ["a1"]

    monkeypatch.setattr(main, "sweep_assets", sweeping)
    state = SimpleNamespace(store=SimpleNamespace(remote_assets=remote), assets=object())
    await main._sweep_assets_logged(state, stop)  # type: ignore[arg-type]
    assert remote.calls == ["mark a1"]


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


_forgotten: list[list[str]] = []


class _Geometry:
    """A geometry index that only records the pieces it is told are gone."""

    def __init__(self, pool: Any) -> None:
        pass

    def forget_pieces(self, piece_keys: list[str]) -> None:
        _forgotten.append(piece_keys)


async def test_the_blob_sweep_forgets_the_geometry_of_the_pieces_it_removes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swept piece is no geometry's source: its `piece_geometry` row goes with it."""
    monkeypatch.setattr(main, "sweep_blobs", lambda blobs, refs, *, grace: ["p1", "p2"])
    monkeypatch.setattr(main, "GeometryIndex", _Geometry)
    _forgotten.clear()
    state = SimpleNamespace(
        store=SimpleNamespace(content=None),
        blobs=object(),
        refs=SimpleNamespace(pool=object()),
        config=SimpleNamespace(job_ttl=86400),
    )
    await main._sweep_blobs_logged(state)  # type: ignore[arg-type]
    assert _forgotten == [["p1", "p2"]]


class _Index:
    """A blob index that only answers which backend ids a row names."""

    def __init__(self, recorded: frozenset[tuple[str, str]] = frozenset()) -> None:
        self.recorded = recorded

    def shares_backend_id(self, backend: str, backend_id: str) -> bool:
        return (backend, backend_id) in self.recorded


class _Bambuddy(BambuddyContentBackend):
    """A Bambuddy backend that only records the reconcile it is asked for."""

    def __init__(self) -> None:
        self.cutoffs: list[datetime] = []

    async def sweep_unrecorded(
        self, *, cutoff: datetime, recorded: Callable[[str], bool] | None = None
    ) -> list[str]:
        self.cutoffs.append(cutoff)
        return ["600"]


@pytest.mark.parametrize("on_bambuddy", [False, True])
async def test_the_blob_sweep_reconciles_unrecorded_uploads_on_a_sweeper_only(
    monkeypatch: pytest.MonkeyPatch, on_bambuddy: bool
) -> None:
    """#701: on Bambuddy the sweep also deletes the Work/ files no row names that are
    older than `job_ttl`; a backend that is no `UnrecordedSweeper` is not asked."""

    async def sweep_content(store: Any, refs: Any, *, grace: int) -> list[str]:
        return []

    monkeypatch.setattr(main, "sweep_content", sweep_content)
    monkeypatch.setattr(main, "GeometryIndex", _Geometry)
    backend: Any = _Bambuddy() if on_bambuddy else object()
    state = SimpleNamespace(
        store=SimpleNamespace(
            content=SimpleNamespace(backend=backend, index=_Index()), blobs=object()
        ),
        refs=SimpleNamespace(pool=object()),
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


async def test_the_blob_sweep_reconciles_unrecorded_s3_objects_by_the_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On S3 too, the sweep deletes the objects no row names that are older than
    `job_ttl`; whether a row names one is the content store's index, asked for `s3`."""
    calls: list[tuple[datetime, Callable[[str], bool]]] = []

    async def sweep_unrecorded(
        self: S3ContentBackend, *, cutoff: datetime, recorded: Callable[[str], bool]
    ) -> int:
        calls.append((cutoff, recorded))
        return 2

    async def sweep_content(store: Any, refs: Any, *, grace: int) -> list[str]:
        return []

    monkeypatch.setattr(S3ContentBackend, "sweep_unrecorded", sweep_unrecorded)
    monkeypatch.setattr(main, "sweep_content", sweep_content)
    monkeypatch.setattr(main, "GeometryIndex", _Geometry)
    _forgotten.clear()
    backend = S3ContentBackend(
        S3Settings(
            endpoint="http://127.0.0.1:1",
            bucket="b",
            prefix="p",
            access_key_id="a",
            secret_access_key="s",
        )
    )
    index = _Index(frozenset({("s3", "p/piece/kept")}))
    state = SimpleNamespace(
        store=SimpleNamespace(
            content=SimpleNamespace(backend=backend, index=index), blobs=object()
        ),
        refs=SimpleNamespace(pool=object()),
        config=SimpleNamespace(job_ttl=86400),
    )
    before = datetime.now(UTC)
    await main._sweep_blobs_logged(state)  # type: ignore[arg-type]
    [(cutoff, recorded)] = calls
    ttl = timedelta(seconds=86400)
    assert before - ttl <= cutoff <= datetime.now(UTC) - ttl
    assert recorded("p/piece/kept") is True and recorded("p/piece/gone") is False
    assert _forgotten == []  # objects are no piece keys


class _Cache(CachedBlobStore):
    """A worker cache that only records that it was trimmed."""

    def __init__(self) -> None:
        self.evicted = 0

    def evict(self, *, now: float | None = None) -> list[str]:
        self.evicted += 1
        return []


class _Unreachable(BambuddyContentBackend):
    def __init__(self) -> None:
        pass

    async def sweep_unrecorded(
        self, *, cutoff: datetime, recorded: Callable[[str], bool] | None = None
    ) -> list[str]:
        raise OSError("Bambuddy is down")


class _Reachable(BambuddyContentBackend):
    """A Bambuddy whose unrecorded-upload sweep works, and records that it ran."""

    def __init__(self) -> None:
        self.swept = 0

    async def sweep_unrecorded(
        self, *, cutoff: datetime, recorded: Callable[[str], bool] | None = None
    ) -> list[str]:
        self.swept += 1
        return []


@pytest.mark.parametrize("failing", ["sweep_content", "sweep_unrecorded"])
async def test_a_failing_bambuddy_step_still_trims_the_worker_cache(
    monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """#2115: a Bambuddy that is down fails the sweep, but the local cache is still
    evicted, or it grows for as long as Bambuddy stays down. Both steps need it:
    `sweep_content` releases each stale blob through Bambuddy too."""

    async def sweep_content(store: Any, refs: Any, *, grace: int) -> list[str]:
        if failing == "sweep_content":
            raise OSError("Bambuddy is down")
        return []

    monkeypatch.setattr(main, "sweep_content", sweep_content)
    cache = _Cache()
    state = SimpleNamespace(
        store=SimpleNamespace(
            content=SimpleNamespace(backend=_Unreachable(), index=_Index()), blobs=cache
        ),
        refs=object(),
        config=SimpleNamespace(job_ttl=86400),
    )
    with pytest.raises(OSError, match="down"):
        await main._sweep_blobs_logged(state)  # type: ignore[arg-type]
    assert cache.evicted == 1


class _FailingCache(CachedBlobStore):
    """A worker cache whose trim fails."""

    def __init__(self) -> None:
        pass

    def evict(self, *, now: float | None = None) -> list[str]:
        raise RuntimeError("the cache disk is full")


def _sweep_state(cache: CachedBlobStore, backend: Any) -> SimpleNamespace:
    return SimpleNamespace(
        store=SimpleNamespace(
            content=SimpleNamespace(backend=backend, index=_Index()), blobs=cache
        ),
        refs=object(),
        config=SimpleNamespace(job_ttl=86400),
    )


async def test_a_failing_trim_does_not_hide_the_bambuddy_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#2560: each failure is logged on its own, and the Bambuddy one is what fails
    the Schedule's activity, not the trim raised over it."""

    async def sweep_content(store: Any, refs: Any, *, grace: int) -> list[str]:
        raise OSError("Bambuddy is down")

    monkeypatch.setattr(main, "sweep_content", sweep_content)
    with pytest.raises(OSError, match="down"):
        await main._sweep_blobs_logged(_sweep_state(_FailingCache(), _Unreachable()))  # type: ignore[arg-type]
    logged = {r.getMessage(): r.exc_info[1] for r in caplog.records if r.exc_info}
    assert isinstance(logged["could not trim the worker cache"], RuntimeError)
    assert isinstance(logged["could not sweep unreferenced blobs"], OSError)


async def test_a_failing_trim_alone_fails_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both Bambuddy steps succeed, then the trim fails: that is the sweep's failure
    (review #2984: a real backend, so `sweep_unrecorded` runs too)."""

    async def sweep_content(store: Any, refs: Any, *, grace: int) -> list[str]:
        return []

    monkeypatch.setattr(main, "sweep_content", sweep_content)
    backend = _Reachable()
    with pytest.raises(RuntimeError, match="disk is full"):
        await main._sweep_blobs_logged(_sweep_state(_FailingCache(), backend))  # type: ignore[arg-type]
    assert backend.swept == 1


async def test_a_cancelled_sweep_stays_cancelled_when_the_trim_also_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Review #2984: the `BaseException` branch. A cancellation propagates as itself,
    and the trim's own failure under it is only logged."""

    async def sweep_content(store: Any, refs: Any, *, grace: int) -> list[str]:
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "sweep_content", sweep_content)
    with pytest.raises(asyncio.CancelledError):
        await main._sweep_blobs_logged(_sweep_state(_FailingCache(), _Reachable()))  # type: ignore[arg-type]
    logged = {r.getMessage(): r.exc_info[1] for r in caplog.records if r.exc_info}
    assert isinstance(logged["could not trim the worker cache"], RuntimeError)
