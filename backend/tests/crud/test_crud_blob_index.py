"""The render worker's blob index through the CRUD routes (#5298 Task 16, inventory
R13-R16): `RemoteBlobIndex` over `api/crud_render.py`, on the API's own `BlobIndex`.

`put`, `swap`, `mark` and both deletes take an ``Idempotency-Key`` (plan D5): a `put`
answers the object it replaced, and a resend after a lost answer must answer that object
again, not the new one, or the worker would release what it just stored. The ledger row
commits with the index write, inside the API's deadlock retry."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg.errors import DeadlockDetected
from temporalio import activity

from scadbuddy.crud.client import CrudRefusedError, CrudSyncClient, activity_key
from scadbuddy.crud.render import BASE, RemoteBlobIndex, WriteKeys
from scadbuddy.store.content_models import BlobRef, ReuseLostError
from scadbuddy.store.index import BlobIndex, Pool
from tests.crud.crud_support import served_render_crud
from tests.support.store import store_pool

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened


@pytest.fixture
async def client(pool: Pool) -> AsyncIterator[CrudSyncClient]:
    async with served_render_crud(pool, unavailable_budget=0.5) as crud:
        blocking = crud.blocking()
        try:
            yield blocking
        finally:
            blocking.close()


@pytest.fixture
def index(client: CrudSyncClient) -> RemoteBlobIndex:
    return RemoteBlobIndex(client)


def _ref(name: str, backend: str = "bambuddy") -> BlobRef:
    return BlobRef(sha256=name * 64, kind="piece", backend=backend, backend_id=f"id-{name}", size=4)


def _wire(ref: BlobRef, *, reuse: bool = False) -> dict[str, Any]:
    return {"ref": ref.model_dump(mode="json"), "slug": "demo", "meta": {}, "reuse": reuse}


def _ledger(pool: Pool) -> int:
    with pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM crud_requests").fetchone()
    assert row is not None
    return int(row["n"])


# -- put: the replaced object, once per key (R14) ------------------------------


async def test_a_put_resent_with_its_key_answers_the_first_previous(
    client: CrudSyncClient, pool: Pool
) -> None:
    """The second send is a retry after a lost answer: it answers the object the first
    send replaced, not the one it stored, and writes nothing more."""
    sql = BlobIndex(pool)
    sql.put("k", _ref("a"), slug="demo", meta={})
    key = uuid.uuid4().hex

    def send() -> Any:
        return client.request(
            "PUT", f"{BASE}/blob-index/k", json=_wire(_ref("b")), idempotency_key=key
        ).json()

    first = await asyncio.to_thread(send)
    again = await asyncio.to_thread(send)
    assert first == {"previous": _ref("a").model_dump(mode="json")}
    assert again == first
    named = sql.get("k")
    assert named is not None and named.ref == _ref("b")
    assert _ledger(pool) == 1


async def test_a_put_without_a_key_is_a_428_and_writes_nothing(
    client: CrudSyncClient, pool: Pool
) -> None:
    for method, path, body in (
        ("PUT", "/blob-index/k", _wire(_ref("b"))),
        ("POST", "/blob-index/k/swap", {**_wire(_ref("b")), "expected": None}),
        ("DELETE", "/blob-index/k", None),
        ("DELETE", "/blob-index/k/stale?before=2026-01-01T00:00:00Z", None),
        (
            "POST",
            "/blob-index/mark",
            {"keys": ["k"], "backend": "bambuddy", "cutoff": "2026-01-01T00:00:00Z", "meta": {}},
        ),
    ):
        with pytest.raises(CrudRefusedError) as refused:
            await asyncio.to_thread(client.request, method, f"{BASE}{path}", json=body)
        assert refused.value.status == 428, (method, path)
    assert BlobIndex(pool).get("k") is None
    assert _ledger(pool) == 0


async def test_the_worker_put_answers_the_replaced_object(
    index: RemoteBlobIndex, pool: Pool
) -> None:
    assert await asyncio.to_thread(index.put, "k", _ref("a"), slug="demo", meta={"x": 1}) is None
    previous = await asyncio.to_thread(index.put, "k", _ref("b"), slug="demo", meta={})
    assert previous == _ref("a")
    named = BlobIndex(pool).get("k")
    assert named is not None and named.ref == _ref("b")


async def test_a_reuse_of_a_freed_object_raises_reuse_lost_on_the_worker(
    index: RemoteBlobIndex, pool: Pool
) -> None:
    """`ReuseLostError` is a 409 ``reuse-lost`` and the same error again on the worker,
    so `ContentStore` uploads its own copy; nothing is written or recorded."""
    with pytest.raises(ReuseLostError):
        await asyncio.to_thread(index.put, "k", _ref("gone"), slug="demo", meta={}, reuse=True)
    with pytest.raises(ReuseLostError):
        await asyncio.to_thread(
            index.swap, "k", _ref("gone"), expected=None, slug="demo", meta={}, reuse=True
        )
    assert BlobIndex(pool).get("k") is None
    assert _ledger(pool) == 0


async def test_a_deadlock_retried_in_the_api_records_only_the_write_that_landed(
    index: RemoteBlobIndex, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API's deadlock retry wraps the ledger row too: a victim's half (its row
    written, then broken) rolls back whole, and the retry answers the true previous."""
    sql = BlobIndex(pool)
    sql.put("k", _ref("a"), slug="demo", meta={})
    put_in = BlobIndex.put_in
    calls = 0

    def once_a_victim(self: BlobIndex, conn: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        answer = put_in(self, conn, *args, **kwargs)
        if calls == 1:
            raise DeadlockDetected("deadlock detected")
        return answer

    monkeypatch.setattr(BlobIndex, "put_in", once_a_victim)
    previous = await asyncio.to_thread(index.put, "k", _ref("b"), slug="demo", meta={})
    assert calls == 2
    assert previous == _ref("a")
    assert _ledger(pool) == 1


async def test_a_deadlock_the_api_gave_up_on_is_a_deadlock_on_the_worker(
    index: RemoteBlobIndex, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As over SQL, `ContentStore` then removes its own upload: no row names it."""

    def victim(*_: object, **__: object) -> Any:
        raise DeadlockDetected("deadlock detected")

    monkeypatch.setattr(BlobIndex, "put_in", victim)
    with pytest.raises(DeadlockDetected):
        await asyncio.to_thread(index.put, "k", _ref("b"), slug="demo", meta={})
    assert _ledger(pool) == 0


def _racing_put(
    index: RemoteBlobIndex,
    start: threading.Barrier,
    results: dict[str, str],
    key: str,
    ref: BlobRef,
) -> None:
    start.wait(10)
    try:
        index.put(key, ref, slug="demo", meta={}, reuse=True)
        results[key] = "ok"
    except ReuseLostError:
        results[key] = "lost"
    except Exception as error:  # reported by the caller's assertion
        results[key] = type(error).__name__


async def test_puts_swapping_objects_between_two_keys_both_finish(
    index: RemoteBlobIndex, pool: Pool
) -> None:
    """Each reuses the object the other key names, again and again, at once. The row
    locks are taken in key order in the API (`BlobIndex._locked`), and a deadlock is
    retried there: each put lands or finds its object freed, never a deadlock."""
    sql = BlobIndex(pool)
    p, o = _ref("1"), _ref("2")
    for _ in range(10):
        sql.put("k1", p, slug="demo", meta={})
        sql.put("k2", o, slug="demo", meta={})
        start = threading.Barrier(2)
        results: dict[str, str] = {}
        await asyncio.gather(
            asyncio.to_thread(_racing_put, index, start, results, "k1", o),
            asyncio.to_thread(_racing_put, index, start, results, "k2", p),
        )
        assert set(results.values()) <= {"ok", "lost"}, results
        assert "ok" in results.values()


# -- swap, mark, touch and the deletes (R14, R15) ------------------------------


async def test_swap_is_a_compare_and_swap(index: RemoteBlobIndex, pool: Pool) -> None:
    assert await asyncio.to_thread(index.swap, "k", _ref("a"), expected=None, slug="demo", meta={})
    assert not await asyncio.to_thread(
        index.swap, "k", _ref("b"), expected=None, slug="demo", meta={}
    )
    assert await asyncio.to_thread(
        index.swap, "k", _ref("b"), expected=_ref("a").sha256, slug="demo", meta={}
    )
    named = BlobIndex(pool).get("k")
    assert named is not None and named.ref == _ref("b")


async def test_a_delete_answers_the_row_and_resent_answers_it_again(
    client: CrudSyncClient, index: RemoteBlobIndex, pool: Pool
) -> None:
    sql = BlobIndex(pool)
    sql.put("k", _ref("a"), slug="demo", meta={"m": 1})
    stat = sql.get("k")
    key = uuid.uuid4().hex

    def send() -> Any:
        return client.request(
            "DELETE", f"{BASE}/blob-index/k?backend=bambuddy", idempotency_key=key
        ).json()

    first = await asyncio.to_thread(send)
    assert first is not None and first["ref"] == _ref("a").model_dump(mode="json")
    assert await asyncio.to_thread(send) == first
    sql.put("k", _ref("b", backend="s3"), slug="demo", meta={})
    # Another backend's row stays.
    assert await asyncio.to_thread(index.delete, "k", backend="bambuddy") is None
    gone = await asyncio.to_thread(index.delete, "k")
    assert gone is not None and gone.ref == _ref("b", backend="s3")
    assert stat is not None and stat.meta == {"m": 1}


async def test_delete_if_stale_takes_only_a_row_untouched_since_the_cutoff(
    index: RemoteBlobIndex, pool: Pool
) -> None:
    sql = BlobIndex(pool)
    sql.put("k", _ref("a"), slug="demo", meta={})
    before = sql.now() - timedelta(seconds=60)
    assert await asyncio.to_thread(index.delete_if_stale, "k", before) is None
    cutoff = await asyncio.to_thread(index.now)
    stat = await asyncio.to_thread(index.delete_if_stale, "k", cutoff, backend="bambuddy")
    assert stat is not None and stat.key == "k" and stat.ref == _ref("a")
    assert sql.get("k") is None


async def test_mark_merges_meta_into_the_stale_rows_only_and_touch_unmarks_none(
    index: RemoteBlobIndex, pool: Pool
) -> None:
    sql = BlobIndex(pool)
    for key in ("a", "b"):
        sql.put(key, _ref(key), slug="demo", meta={"x": 1})
    cutoff = await asyncio.to_thread(index.now)
    sql.put("c", _ref("c"), slug="demo", meta={"x": 1})
    await asyncio.to_thread(
        index.mark, ["a", "b", "c"], backend="bambuddy", cutoff=cutoff, swept=True
    )
    await asyncio.to_thread(index.touch, "a")
    marked = {key: (stat.meta if (stat := sql.get(key)) else None) for key in "abc"}
    assert marked == {"a": {"x": 1, "swept": True}, "b": {"x": 1, "swept": True}, "c": {"x": 1}}
    a = sql.get("a")
    assert a is not None and a.touched_at > cutoff


# -- the reads (R13, R16) ------------------------------------------------------


async def test_the_reads_answer_as_the_sql_index(index: RemoteBlobIndex, pool: Pool) -> None:
    sql = BlobIndex(pool)
    assert await asyncio.to_thread(index.get, "piece/one") is None
    sql.put("piece/one", _ref("a"), slug="demo", meta={"n": [1, 2]})
    sql.put("two", _ref("b", backend="s3"), slug="other", meta={})
    assert await asyncio.to_thread(index.get, "piece/one") == sql.get("piece/one")
    assert await asyncio.to_thread(index.by_sha, "a" * 64, "piece", "bambuddy") == _ref("a")
    assert await asyncio.to_thread(index.by_sha, "a" * 64, "piece", "s3") is None
    assert await asyncio.to_thread(index.shares_backend_id, "bambuddy", "id-a")
    assert not await asyncio.to_thread(index.shares_backend_id, "bambuddy", "id-b")
    assert await asyncio.to_thread(index.recorded, "s3", "id-b")
    assert not await asyncio.to_thread(index.recorded, "s3", "id-a")
    assert await asyncio.to_thread(index.usage, "bambuddy") == sql.usage("bambuddy")
    for kinds, slug, backend in (
        (None, None, None),
        (["piece"], None, "s3"),
        (["asset"], None, None),
        (None, "demo", None),
    ):
        assert await asyncio.to_thread(index.stats, kinds, slug, backend=backend) == sql.stats(
            kinds, slug, backend=backend
        )
    assert abs((await asyncio.to_thread(index.now) - sql.now()).total_seconds()) < 5


async def test_a_backend_id_with_slashes_is_one_path_part(
    index: RemoteBlobIndex, pool: Pool
) -> None:
    """Local and S3 object ids hold slashes (`kind/sha`)."""
    ref = BlobRef(sha256="c" * 64, kind="piece", backend="s3", backend_id="t/piece/cc", size=1)
    BlobIndex(pool).put("k", ref, slug=None, meta={})
    assert await asyncio.to_thread(index.shares_backend_id, "s3", "t/piece/cc")
    assert await asyncio.to_thread(index.recorded, "s3", "t/piece/cc")


# -- the key a write sends ------------------------------------------------------


def test_outside_an_activity_each_write_takes_a_key_of_its_own() -> None:
    keys = WriteKeys("blob-index")
    assert len({keys.next() for _ in range(3)}) == 3


def test_inside_an_activity_the_keys_are_its_ordinals(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same on every attempt of one activity, so a write resent by a retried attempt
    is answered once; counted per attempt."""
    info = SimpleNamespace(workflow_id="wf", workflow_run_id="run", activity_id="7", attempt=1)
    monkeypatch.setattr(activity, "in_activity", lambda: True)
    monkeypatch.setattr(activity, "info", lambda: info)
    keys = WriteKeys("blob-index")
    first = [keys.next() for _ in range(2)]
    info.attempt = 2
    second = [keys.next() for _ in range(2)]
    assert first == second == [activity_key(0, "blob-index"), activity_key(1, "blob-index")]
    # Another run of the workflow (a render key's next run) is not a retry.
    info.workflow_run_id = "run-2"
    assert keys.next() != first[0]


def test_two_stores_in_one_attempt_never_share_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each store counts its own writes: at the same ordinal, in the same attempt, the
    namespace keeps their keys apart, so one store's request is never answered with
    another's reply."""
    info = SimpleNamespace(workflow_id="wf", workflow_run_id="run", activity_id="7", attempt=1)
    monkeypatch.setattr(activity, "in_activity", lambda: True)
    monkeypatch.setattr(activity, "info", lambda: info)
    index, previews = WriteKeys("blob-index"), WriteKeys("previews")
    assert index.next() != previews.next()
    assert index.next() != previews.next()
    assert WriteKeys("previews").next() == activity_key(0, "previews")


async def test_a_retried_attempt_writing_another_body_under_its_key_writes_anew(
    index: RemoteBlobIndex, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier attempt's put under this ordinal stored another object (a new upload):
    the ledger refuses the key for this body (422), and the worker sends the write again
    under a key of its own, never failing the render on it."""
    info = SimpleNamespace(workflow_id="wf", workflow_run_id="run", activity_id="7", attempt=1)
    monkeypatch.setattr(activity, "in_activity", lambda: True)
    monkeypatch.setattr(activity, "info", lambda: info)
    assert await asyncio.to_thread(index.put, "k", _ref("a"), slug="demo", meta={}) is None
    info.attempt = 2
    previous = await asyncio.to_thread(index.put, "k", _ref("b"), slug="demo", meta={})
    assert previous == _ref("a")
    named = BlobIndex(pool).get("k")
    assert named is not None and named.ref == _ref("b")


# -- keys only the API writes (#5298 Task 18) ---------------------------------------


@pytest.mark.parametrize("key", ["asset-" + "a" * 64, "font-dejavu", "src-demo-abc"])
async def test_the_worker_cannot_store_a_key_only_the_api_writes(
    index: RemoteBlobIndex, pool: Pool, key: str
) -> None:
    """An upload's copy is what an adopt writes the upload's row from: a render
    credential that could store one could forge the row. Fonts and snapshots are the
    API's too. Nor may it delete one: a delete whose object release then failed would
    leave the object untracked, the row's restore being a write too. A touch is allowed;
    a font's or snapshot's forget goes to the API's ``forget-copy``."""
    sql = BlobIndex(pool)
    sql.put(key, _ref("b"), slug=None, meta={"name": "real"})
    for write in (
        lambda: index.put(key, _ref("a"), slug=None, meta={"name": "forged"}),
        lambda: index.swap(key, _ref("a"), expected=None, slug=None, meta={}),
        lambda: index.mark([key], backend="bambuddy", cutoff=sql.now(), name="forged"),
        lambda: index.delete(key, backend="bambuddy"),
        lambda: index.delete_if_stale(key, sql.now(), backend="bambuddy"),
    ):
        with pytest.raises(CrudRefusedError) as refused:
            await asyncio.to_thread(write)
        assert refused.value.status == 403
        assert refused.value.problem["reason"] == "api-owned-key"
    held = sql.get(key)
    assert held is not None and held.meta == {"name": "real"} and held.ref == _ref("b")
    await asyncio.to_thread(index.touch, key)
    if key.startswith("asset-"):
        # An upload's copy is dropped through ``…/assets/{id}/drop-copy`` only.
        with pytest.raises(CrudRefusedError) as refused:
            await asyncio.to_thread(index.delete, key)
        assert refused.value.status == 403
        assert sql.get(key) is not None
    else:
        forgotten = await asyncio.to_thread(index.delete, key)  # through ``forget-copy``
        assert forgotten is not None and forgotten.ref == _ref("b")
        assert sql.get(key) is None
        assert await asyncio.to_thread(index.delete, key) is None  # natural


async def test_forget_copy_takes_only_a_fonts_or_snapshots_key(client: CrudSyncClient) -> None:
    for key in ("asset-" + "a" * 64, "piece-x"):
        with pytest.raises(CrudRefusedError) as refused:
            await asyncio.to_thread(client.request, "POST", f"{BASE}/blob-index/{key}/forget-copy")
        assert refused.value.status == 403
