"""§6.2's byte store: dedupe, caps, integrity, compare-and-swap, sweep."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from psycopg.errors import DeadlockDetected

from scadbuddy.store.content import (
    BlobCorruptError,
    BlobKind,
    BlobMissingError,
    BlobRef,
    BlobScope,
    ContentStore,
    RefusedDeleteError,
    ReuseLostError,
    StoreFullError,
    sweep_content,
)
from scadbuddy.store.index import BlobIndex, Pool
from scadbuddy.store.local import LocalContentBackend
from scadbuddy.store.refs import BlobRefs
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres
SCOPE = BlobScope(slug="demo", title="Demo")


async def test_put_then_read_and_the_same_bytes_are_stored_once(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool)
    first = await store.put("asset", b"<svg/>", name="a.svg", scope=SCOPE)
    stored = tmp_path / "remote" / first.backend_id
    os.utime(stored, ns=(1_000_000_000, 1_000_000_000))
    again = await store.put("asset", b"<svg/>", name="b.svg", scope=SCOPE)
    assert first == again
    assert stored.stat().st_mtime_ns == 1_000_000_000  # not uploaded a second time
    assert await store.read(first) == b"<svg/>"
    usage = store.usage()
    assert (usage.count, usage.bytes, usage.by_kind) == (1, 6, {"asset": 6})
    assert (await store.stat(f"asset-{first.sha256}")) is not None


class _ClosingBackend(LocalContentBackend):
    """Records each download whose generator was closed."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.closed: list[str] = []

    async def download(self, backend_id: str) -> AsyncGenerator[bytes]:
        try:
            async for chunk in super().download(backend_id):
                yield chunk
        finally:
            self.closed.append(backend_id)


async def test_a_read_stopped_early_closes_the_backends_download(
    tmp_path: Path, pool: Pool
) -> None:
    """A consumer that stops mid-stream and closes `get` closes the backend's download
    with it (#680), rather than leaving its HTTP stream to the garbage collector."""
    backend = _ClosingBackend(tmp_path / "remote")
    store = ContentStore(backend, BlobIndex(pool))
    ref = await store.put("piece", os.urandom(3 << 20), name="p", scope=SCOPE)
    async with contextlib.aclosing(store.get(ref)) as chunks:
        async for _ in chunks:
            break
    assert backend.closed == [ref.backend_id]


async def test_put_refuses_past_the_cap_except_a_re_put(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool, max_count=1)
    await store.put("asset", b"one", name="1", scope=SCOPE)
    with pytest.raises(StoreFullError, match="SCADBUDDY_STORE_MAX_COUNT"):
        await store.put("asset", b"two", name="2", scope=SCOPE)
    await store.put("asset", b"one", name="1 again", scope=SCOPE)  # already stored: never refused


async def test_put_refuses_past_the_byte_cap_except_a_re_put(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool, max_total_bytes=5)
    await store.put("asset", b"abc", name="1", scope=SCOPE)
    with pytest.raises(StoreFullError, match="SCADBUDDY_STORE_MAX_TOTAL_BYTES"):
        await store.put("asset", b"def", name="2", scope=SCOPE)
    await store.put("asset", b"abc", name="1 again", scope=SCOPE)


async def test_altered_or_missing_bytes_are_reported_not_returned(
    tmp_path: Path, pool: Pool
) -> None:
    store = local_content(tmp_path / "remote", pool)
    ref = await store.put("piece", b"real", name="p.zip", scope=SCOPE, key="k")
    (tmp_path / "remote" / ref.backend_id).write_bytes(b"fake")
    with pytest.raises(BlobCorruptError):
        await store.read(ref)
    (tmp_path / "remote" / ref.backend_id).unlink()
    with pytest.raises(BlobMissingError):
        await store.read(ref)
    assert await store.stat("k") is None  # a vanished object drops its index row
    assert store.index.get("k") is None


async def test_replace_is_a_compare_and_swap(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool)
    v1 = await store.replace("k", "piece", b"v1", name="p", scope=SCOPE, expected=None)
    assert v1 is not None
    lost = await store.replace("k", "piece", b"other", name="p", scope=SCOPE, expected=None)
    assert lost is None
    assert not (tmp_path / "remote" / "piece" / hashlib.sha256(b"other").hexdigest()).exists()
    v2 = await store.replace("k", "piece", b"v2", name="p", scope=SCOPE, expected=v1.sha256)
    assert v2 is not None
    assert not (tmp_path / "remote" / v1.backend_id).exists()  # the superseded object is gone
    stale = await store.replace("k", "piece", b"v3", name="p", scope=SCOPE, expected=v1.sha256)
    assert stale is None
    named = store.index.get("k")
    assert named is not None and named.ref.sha256 == v2.sha256
    assert not (tmp_path / "remote" / "piece" / hashlib.sha256(b"v3").hexdigest()).exists()


async def test_delete_keeps_an_object_another_key_still_names(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool)
    ref = await store.put("snapshot", b"same", name="a", scope=SCOPE, key="a")
    await store.put("snapshot", b"same", name="b", scope=SCOPE, key="b")
    await store.delete("a")
    assert await store.read(ref) == b"same"
    await store.delete("b")
    assert not (tmp_path / "remote" / ref.backend_id).exists()


async def test_sweep_takes_only_unreferenced_stale_pieces_and_snapshots(
    tmp_path: Path, pool: Pool
) -> None:
    store = local_content(tmp_path / "remote", pool)
    refs = BlobRefs(pool)
    for key in ("kept", "fresh", "stale"):
        await store.put("piece", key.encode(), name=key, scope=SCOPE, key=key)
    await store.put("asset", b"asset", name="a.svg", scope=SCOPE, key="asset-x")
    refs.add("kept", "job", "j1")
    old = datetime.now(UTC) - timedelta(hours=2)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE store_blobs SET touched_at = %s WHERE key IN ('kept', 'stale', 'asset-x')",
            (old,),
        )
    assert await sweep_content(store, refs, grace=3600, now=time.time()) == ["stale"]
    assert {s.key for s in store.index.stats(None)} == {"kept", "fresh", "asset-x"}
    assert not (tmp_path / "remote" / "piece" / hashlib.sha256(b"stale").hexdigest()).exists()


def _age(pool: Pool, *keys: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE store_blobs SET touched_at = %s WHERE key = ANY(%s)",
            (datetime.now(UTC) - timedelta(hours=2), list(keys)),
        )


class _ClaimedAfterSnapshot(BlobRefs):
    """A claim of ``key`` that lands right after the sweep's `referenced()` snapshot:
    the claimant's `touch` (when ``touch``), then its `add`."""

    def __init__(self, pool: Pool, store: ContentStore, key: str, *, touch: bool) -> None:
        super().__init__(pool)
        self.store, self.key, self.touch = store, key, touch

    def referenced(self) -> set[str]:
        snapshot = super().referenced()
        if self.touch:
            self.store.index.touch(self.key)
        self.add(self.key, "job", "late")
        return snapshot


@pytest.mark.parametrize("touch", [True, False], ids=["touched-first", "added-only"])
async def test_a_claim_after_the_sweeps_snapshot_survives_only_when_touched_first(
    tmp_path: Path, pool: Pool, touch: bool
) -> None:
    """The claim protocol end to end (#637): `ContentStore.touch` before `refs.add`
    keeps a blob a sweep already decided was unreferenced; an `add` alone does not."""
    store = local_content(tmp_path / "remote", pool)
    await store.put("piece", b"claimed", name="c", scope=SCOPE, key="claimed")
    _age(pool, "claimed")
    refs = _ClaimedAfterSnapshot(pool, store, "claimed", touch=touch)
    removed = await sweep_content(store, refs, grace=3600, now=time.time())
    assert removed == ([] if touch else ["claimed"])
    assert (store.index.get("claimed") is not None) is touch


async def test_the_sweep_leaves_another_backends_rows_alone(
    tmp_path: Path, pool: Pool, caplog: pytest.LogCaptureFixture
) -> None:
    store = local_content(tmp_path / "remote", pool)
    foreign = BlobRef(sha256="0" * 64, kind="piece", backend="bambuddy", backend_id="42", size=1)
    store.index.put("switched", foreign, slug="demo", meta={})
    await store.put("piece", b"stale", name="p", scope=SCOPE, key="stale")
    _age(pool, "switched", "stale")
    assert await sweep_content(store, BlobRefs(pool), grace=3600, now=time.time()) == ["stale"]
    assert store.index.get("switched") is not None
    assert [s.key async for s in store.list(SCOPE)] == []
    assert await store.stat("switched") is None  # not this backend's to check
    await store.delete("switched")  # nothing of this backend's to delete
    assert store.index.get("switched") is not None
    # A put over the switched key lands here and leaves the old object where it is.
    with caplog.at_level(logging.WARNING, logger="scadbuddy.store.content"):
        ref = await store.put("piece", b"new", name="p", scope=SCOPE, key="switched")
    named = store.index.get("switched")
    assert named is not None and named.ref == ref and ref.backend == "local"
    assert [r.message for r in caplog.records if getattr(r, "key", None) == "switched"] == [
        "left a replaced blob on another backend"
    ]


class _RefusingBackend(LocalContentBackend):
    def __init__(self, root: Path, refused: str) -> None:
        super().__init__(root)
        self.refused = refused

    async def remove(self, backend_id: str) -> None:
        if backend_id == self.refused:
            raise RefusedDeleteError(backend_id)
        await super().remove(backend_id)


async def test_a_refused_delete_is_logged_and_the_sweep_goes_on(
    tmp_path: Path, pool: Pool, caplog: pytest.LogCaptureFixture
) -> None:
    store = local_content(tmp_path / "remote", pool)
    refused = f"piece/{hashlib.sha256(b'a').hexdigest()}"
    store.backend = _RefusingBackend(tmp_path / "remote", refused)
    await store.put("piece", b"a", name="a", scope=SCOPE, key="a-refused")
    await store.put("piece", b"b", name="b", scope=SCOPE, key="b-stale")
    _age(pool, "a-refused", "b-stale")
    with caplog.at_level(logging.ERROR, logger="scadbuddy.store.content"):
        removed = await sweep_content(store, BlobRefs(pool), grace=3600, now=time.time())
    assert removed == ["b-stale"]
    refusals = [r for r in caplog.records if getattr(r, "key", None) == "a-refused"]
    assert len(refusals) == 1


class _GatedBackend(LocalContentBackend):
    """Holds each upload until both racing puts have uploaded."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.gate = asyncio.Barrier(2)

    async def upload(self, kind: BlobKind, data: bytes, *, name: str, scope: BlobScope) -> str:
        backend_id = await super().upload(kind, data, name=name, scope=scope)
        await self.gate.wait()
        return backend_id


@pytest.mark.parametrize("existing", [True, False], ids=["over-a-row", "new-key"])
async def test_racing_puts_to_one_key_leave_no_orphan(
    tmp_path: Path, pool: Pool, existing: bool
) -> None:
    root = tmp_path / "remote"
    store = local_content(root, pool)
    if existing:
        await store.put("piece", b"previous", name="p", scope=SCOPE, key="k")
    store.backend = _GatedBackend(root)
    await asyncio.gather(
        store.put("piece", b"A", name="p", scope=SCOPE, key="k"),
        store.put("piece", b"B", name="p", scope=SCOPE, key="k"),
    )
    named = store.index.get("k")
    assert named is not None
    assert [s.key for s in store.index.stats(None)] == ["k"]
    assert sorted(p.name for p in (root / "piece").iterdir()) == [
        named.ref.backend_id.removeprefix("piece/")
    ]


class _ReleasedWhileReused(LocalContentBackend):
    """Finds the object a put means to reuse, then holds that put until a delete has
    freed and removed the object, as a release racing the reuse would."""

    def __init__(self, root: Path, release: asyncio.Event) -> None:
        super().__init__(root)
        self.gate = asyncio.Barrier(2)
        self.release = release

    async def exists(self, backend_id: str) -> bool:
        found = await super().exists(backend_id)
        await self.gate.wait()
        await self.release.wait()
        return found


class _DistinctIdsReleasedWhileReused(_ReleasedWhileReused):
    """As Bambuddy does: every upload is a new object with its own id."""

    async def upload(self, kind: BlobKind, data: bytes, *, name: str, scope: BlobScope) -> str:
        backend_id = f"{kind}/{uuid.uuid4().hex * 2}"
        path = self._path(backend_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return backend_id


@pytest.mark.parametrize(
    "backend_class",
    [_ReleasedWhileReused, _DistinctIdsReleasedWhileReused],
    ids=["content-addressed", "distinct-ids"],
)
async def test_a_reuse_that_loses_to_a_release_uploads_its_own_copy(
    tmp_path: Path, pool: Pool, backend_class: type[_ReleasedWhileReused]
) -> None:
    root = tmp_path / "remote"
    store = local_content(root, pool)
    old = await store.put("snapshot", b"same", name="a", scope=SCOPE, key="a")
    released = asyncio.Event()
    backend = backend_class(root, released)
    store.backend = backend

    async def release_a() -> None:
        await backend.gate.wait()  # the put found `old` and means to reuse it
        await store.delete("a")
        released.set()

    ref, _ = await asyncio.gather(
        store.put("snapshot", b"same", name="b", scope=SCOPE, key="b"), release_a()
    )
    assert (root / ref.backend_id).is_file()  # its own copy, not the freed object
    assert await store.read(ref) == b"same"
    if backend_class is _DistinctIdsReleasedWhileReused:
        assert ref.backend_id != old.backend_id
    gone = old.model_copy(update={"backend_id": f"snapshot/{'0' * 64}"})
    with pytest.raises(ReuseLostError):
        store.index.put("c", gone, slug="demo", meta={}, reuse=True)


async def test_a_sweep_racing_a_put_of_the_same_bytes_under_another_key_loses_nothing(
    tmp_path: Path, pool: Pool
) -> None:
    """The sweep takes a stale key while a put of another key reuses its object: the
    put stores its own copy, so the new key stays readable."""
    root = tmp_path / "remote"
    store = local_content(root, pool)
    await store.put("piece", b"same", name="a", scope=SCOPE, key="a")
    _age(pool, "a")
    released = asyncio.Event()
    backend = _ReleasedWhileReused(root, released)
    store.backend = backend

    async def sweep() -> list[str]:
        await backend.gate.wait()  # the put found `a`'s object and means to reuse it
        removed = await sweep_content(store, BlobRefs(pool), grace=3600, now=time.time())
        released.set()
        return removed

    ref, removed = await asyncio.gather(
        store.put("piece", b"same", name="b", scope=SCOPE, key="b"), sweep()
    )
    assert removed == ["a"]
    assert await store.read(ref) == b"same"
    named = store.index.get("b")
    assert named is not None and named.ref == ref


async def test_a_lost_reuse_at_the_cap_is_still_stored(
    tmp_path: Path, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback copy stands in for a re-put, which is never refused."""
    root = tmp_path / "remote"
    store = local_content(root, pool)
    await store.put("snapshot", b"same", name="a", scope=SCOPE, key="a")
    released = asyncio.Event()
    backend = _ReleasedWhileReused(root, released)
    store.backend = backend

    def full(size: int) -> None:
        raise StoreFullError("at the cap")

    monkeypatch.setattr(store, "_require_room", full)

    async def release_a() -> None:
        await backend.gate.wait()
        await store.delete("a")
        released.set()

    ref, _ = await asyncio.gather(
        store.put("snapshot", b"same", name="b", scope=SCOPE, key="b"), release_a()
    )
    assert await store.read(ref) == b"same"


def _parked_hold(monkeypatch: pytest.MonkeyPatch, after: Callable[[], None]) -> None:
    """Run ``after`` in `_hold_shared`, once the hold is taken."""
    hold = BlobIndex._hold_shared

    def parked(*args: Any, **kwargs: Any) -> None:
        hold(*args, **kwargs)
        after()

    monkeypatch.setattr(BlobIndex, "_hold_shared", staticmethod(parked))


def _both(*calls: Callable[[], object]) -> list[str]:
    results: list[str] = []

    def run(call: Callable[[], object]) -> None:
        try:
            call()
            results.append("ok")
        except Exception as error:
            results.append(type(error).__name__)

    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return results


def _ref(backend_id: str, sha: str) -> BlobRef:
    return BlobRef(
        sha256=sha * 64, kind="snapshot", backend="bambuddy", backend_id=backend_id, size=4
    )


def _meet(barrier: threading.Barrier) -> Callable[[], None]:
    def meet() -> None:
        # Both transactions hold what they hold here, if both got this far; a retry
        # (or one that waited on the other) passes alone once the wait times out.
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=2)

    return meet


async def test_concurrent_re_puts_of_one_key_reusing_other_objects_hold_each(
    tmp_path: Path, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two re-puts of ``k``, each reusing an object another key names (A under ``a``,
    B under ``b``): the second waits on ``k``'s row lock, and each holds its own
    object's row FOR SHARE. A delete of ``a`` while the first holds A waits for it, and
    its release then sees ``k`` naming A and keeps the object. Neither re-put
    deadlocks, and ``k`` ends up naming B."""
    root = tmp_path / "remote"
    store = local_content(root, pool)
    a = await store.put("snapshot", b"alpha", name="a", scope=SCOPE, key="a")
    b = await store.put("snapshot", b"beta", name="b", scope=SCOPE, key="b")
    c = await store.put("snapshot", b"gamma", name="k", scope=SCOPE, key="k")
    held = [threading.Event(), threading.Event()]
    go = [threading.Event(), threading.Event()]
    holds: list[int] = []

    def park() -> None:
        turn = len(holds)
        holds.append(turn)
        held[turn].set()
        assert go[turn].wait(10)

    _parked_hold(monkeypatch, park)
    returned: dict[str, BlobRef | None] = {}

    def re_put(name: str, ref: BlobRef) -> Callable[[], None]:
        def run() -> None:
            returned[name] = store.index.put("k", ref, slug="demo", meta={}, reuse=True)

        return run

    first = threading.Thread(target=re_put("first", a))
    second = threading.Thread(target=re_put("second", b))
    release = threading.Thread(target=lambda: asyncio.run(store.delete("a")))
    try:
        first.start()
        assert held[0].wait(10)  # the first holds k's row and A's row
        second.start()
        second.join(0.3)
        assert not held[1].is_set()  # the second waits on k's row lock
        release.start()
        release.join(0.5)
        assert release.is_alive()  # the delete of `a` waits on the first's hold of A
        go[0].set()
        first.join(10)
        assert held[1].wait(10)  # the second has k's row and holds B's
        release.join(10)
        assert not release.is_alive()
        # The release ran after the first committed k -> A, so it kept A.
        assert await store.read(a) == b"alpha"
        assert store.index.get("a") is None
        go[1].set()
        second.join(10)
    finally:
        for event in go:
            event.set()
        for thread in (first, second, release):
            if thread.is_alive():
                thread.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert returned == {"first": c, "second": a}
    named = store.index.get("k")
    assert named is not None and named.ref == b
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT backend_id, count(*) AS n FROM store_blobs GROUP BY backend_id"
        ).fetchall()
    assert {row["backend_id"]: row["n"] for row in rows} == {b.backend_id: 2}


def test_puts_swapping_objects_between_two_keys_do_not_deadlock(
    pool: Pool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Each reuses the object the other key names. Locking key-then-object leaves this
    one deadlock (locking in key text order would avoid it too; retrying is simpler),
    so the deadlock Postgres breaks is retried, and logged. The first to commit frees
    the other's object, so the retry finds it gone (`ContentStore` then uploads its own
    copy); neither is a deadlock."""
    index = BlobIndex(pool)
    p, o = _ref("21", "b"), _ref("22", "c")
    index.put("k1", p, slug="demo", meta={})
    index.put("k2", o, slug="demo", meta={})
    _parked_hold(monkeypatch, _meet(threading.Barrier(2)))
    with caplog.at_level(logging.WARNING, logger="scadbuddy.store.index"):
        results = _both(
            lambda: index.put("k1", o, slug="demo", meta={}, reuse=True),
            lambda: index.put("k2", p, slug="demo", meta={}, reuse=True),
        )
    assert sorted(results) == ["ReuseLostError", "ok"]
    [retried] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert retried.__dict__["key"] in ("k1", "k2")
    assert retried.__dict__["attempt"] == 1
    k1, k2 = index.get("k1"), index.get("k2")
    assert k1 is not None and k2 is not None
    assert (k1.ref, k2.ref) in [(o, o), (p, p)]  # one moved; the other kept its object


async def test_a_release_waits_for_a_reuse_that_holds_the_object(
    tmp_path: Path, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving A: the reuse holds the object's row first, so the delete of that
    row waits, and its release then sees the new row and keeps the object."""
    root = tmp_path / "remote"
    store = local_content(root, pool)
    old = await store.put("snapshot", b"same", name="a", scope=SCOPE, key="a")
    held, go = threading.Event(), threading.Event()

    def park() -> None:
        held.set()
        go.wait(10)

    _parked_hold(monkeypatch, park)
    reuse = threading.Thread(
        target=lambda: store.index.put("b", old, slug="demo", meta={}, reuse=True)
    )
    reuse.start()
    assert held.wait(10)
    release = threading.Thread(target=lambda: asyncio.run(store.delete("a")))
    release.start()
    release.join(0.5)
    assert release.is_alive()  # waits on the reuse's hold
    go.set()
    reuse.join(10)
    release.join(10)
    assert (root / old.backend_id).is_file()
    named = store.index.get("b")
    assert named is not None and named.ref == old
    assert store.index.get("a") is None


def _deadlocked(*_: object, **__: object) -> Any:
    raise DeadlockDetected("deadlock detected")


async def test_a_put_the_index_gave_up_on_removes_its_own_upload(
    tmp_path: Path, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "remote"
    store = local_content(root, pool)
    monkeypatch.setattr(store.index, "put", _deadlocked)
    with pytest.raises(DeadlockDetected):
        await store.put("snapshot", b"fresh", name="a", scope=SCOPE, key="a")
    assert not list((root / "snapshot").iterdir())  # no row will ever name it


async def test_a_put_the_index_gave_up_on_keeps_a_reused_object(
    tmp_path: Path, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "remote"
    store = local_content(root, pool)
    kept = await store.put("snapshot", b"same", name="a", scope=SCOPE, key="a")
    monkeypatch.setattr(store.index, "swap", _deadlocked)
    with pytest.raises(DeadlockDetected):
        await store.replace("b", "snapshot", b"same", name="b", scope=SCOPE, expected=None)
    assert await store.read(kept) == b"same"


def test_mark_merges_meta_into_the_stale_rows_only(pool: Pool) -> None:
    index = BlobIndex(pool)
    for key in ("b", "a"):
        index.put(key, _ref(key, "d"), slug="demo", meta={"x": 1})
    cutoff = index.now()
    index.put("c", _ref("c", "d"), slug="demo", meta={"x": 1})
    index.mark(["a", "b", "c"], backend="bambuddy", cutoff=cutoff, stale=True)
    marked = {key: (stat.meta if (stat := index.get(key)) else None) for key in "abc"}
    assert marked == {
        "a": {"x": 1, "stale": True},
        "b": {"x": 1, "stale": True},
        "c": {"x": 1},
    }


def test_a_local_object_id_with_a_trailing_newline_is_refused(tmp_path: Path) -> None:
    backend = LocalContentBackend(tmp_path)
    with pytest.raises(ValueError, match="not a local object id"):
        backend._path(f"piece/{'0' * 64}\n")


class _CancelledRemove(LocalContentBackend):
    async def remove(self, backend_id: str) -> None:
        raise asyncio.CancelledError


async def test_a_release_cancelled_midway_keeps_the_row(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool)
    await store.put("piece", b"p", name="p", scope=SCOPE, key="k")
    store.backend = _CancelledRemove(tmp_path / "remote")
    with pytest.raises(asyncio.CancelledError):
        await store.delete("k")
    assert store.index.get("k") is not None


class _SlowRemove(LocalContentBackend):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.removing = asyncio.Event()

    async def remove(self, backend_id: str) -> None:
        self.removing.set()
        await asyncio.Event().wait()


async def test_a_release_cancelled_twice_still_puts_the_row_back(
    tmp_path: Path, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the shield is for: a second cancellation lands while the row is going
    back. The re-insert finishes on its own, and `aclose` waits for it."""
    store = local_content(tmp_path / "remote", pool)
    await store.put("piece", b"p", name="p", scope=SCOPE, key="k")
    backend = _SlowRemove(tmp_path / "remote")
    store.backend = backend
    swapping, go = threading.Event(), threading.Event()
    swap = store.index.swap

    def slow_swap(*args: Any, **kwargs: Any) -> Any:
        swapping.set()
        go.wait(10)
        return swap(*args, **kwargs)

    monkeypatch.setattr(store.index, "swap", slow_swap)
    delete = asyncio.create_task(store.delete("k"))
    await backend.removing.wait()
    delete.cancel()  # mid-remove: the row starts going back
    assert await asyncio.to_thread(swapping.wait, 10)
    delete.cancel()  # mid-re-insert
    with pytest.raises(asyncio.CancelledError):
        await delete
    go.set()
    await store.aclose()
    assert store.index.get("k") is not None


async def test_a_failed_release_that_loses_to_a_put_releases_the_old_object(
    tmp_path: Path, pool: Pool
) -> None:
    store = local_content(tmp_path / "remote", pool)
    old = await store.put("piece", b"old", name="p", scope=SCOPE, key="k")

    class PutMeanwhile(LocalContentBackend):
        first = True

        async def remove(self, backend_id: str) -> None:
            if self.first:
                self.first = False
                await store.put("piece", b"new", name="p", scope=SCOPE, key="k")
                raise RuntimeError("Bambuddy is restarting")
            await super().remove(backend_id)

    store.backend = PutMeanwhile(tmp_path / "remote")
    with pytest.raises(RuntimeError, match="restarting"):
        await store.delete("k")
    row = store.index.get("k")
    assert row is not None and row.ref.sha256 == hashlib.sha256(b"new").hexdigest()
    assert not (tmp_path / "remote" / old.backend_id).exists()  # not left untracked


async def test_a_refused_delete_of_the_replaced_object_does_not_fail_the_put(
    tmp_path: Path, pool: Pool, caplog: pytest.LogCaptureFixture
) -> None:
    """Final review m1: the old object was moved out of `Work/` in Bambuddy's UI. The
    new one is stored and indexed; the old one is left untracked, as the sweep does."""
    store = local_content(tmp_path / "remote", pool)
    store.backend = _RefusingBackend(
        tmp_path / "remote", f"piece/{hashlib.sha256(b'old').hexdigest()}"
    )
    await store.put("piece", b"old", name="p", scope=SCOPE, key="k")
    with caplog.at_level(logging.ERROR, logger="scadbuddy.store.content"):
        new = await store.put("piece", b"new", name="p", scope=SCOPE, key="k")
    stat = store.index.get("k")
    assert stat is not None and stat.ref == new
    assert [r for r in caplog.records if getattr(r, "key", None) == "k"]
