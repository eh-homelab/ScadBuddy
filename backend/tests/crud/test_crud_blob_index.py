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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg.errors import DeadlockDetected
from temporalio import activity

from scadbuddy.api import crud_render
from scadbuddy.crud.client import CrudRefusedError, CrudSyncClient, activity_key
from scadbuddy.crud.render import BASE, RemoteBlobIndex, WriteKeys
from scadbuddy.store.archive import pack_dir
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import BlobRef, BlobScope, ReuseLostError
from scadbuddy.store.index import BlobIndex, Pool
from scadbuddy.store.local import LocalBlobStore
from tests.crud.crud_support import served_render_crud, threaded_render_crud
from tests.support.store import local_content, overwrite_object, store_pool

pytestmark = pytest.mark.requires_postgres

#: Render keys (`api/crud_render.py` `RENDER_KEY`): the only keys a render credential
#: writes.
K, K1, K2 = "e" * 64, "1" * 64, "2" * 64


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
    sql.put(K, _ref("a"), slug="demo", meta={})
    key = uuid.uuid4().hex

    def send() -> Any:
        return client.request(
            "PUT", f"{BASE}/blob-index/{K}", json=_wire(_ref("b")), idempotency_key=key
        ).json()

    first = await asyncio.to_thread(send)
    again = await asyncio.to_thread(send)
    assert first == {"previous": _ref("a").model_dump(mode="json")}
    assert again == first
    named = sql.get(K)
    assert named is not None and named.ref == _ref("b")
    assert _ledger(pool) == 1


async def test_a_put_without_a_key_is_a_428_and_writes_nothing(
    client: CrudSyncClient, pool: Pool
) -> None:
    for method, path, body in (
        ("PUT", f"/blob-index/{K}", _wire(_ref("b"))),
        ("POST", f"/blob-index/{K}/swap", {**_wire(_ref("b")), "expected": None}),
        ("DELETE", f"/blob-index/{K}", None),
        ("DELETE", f"/blob-index/{K}/stale?before=2026-01-01T00:00:00Z", None),
        (
            "POST",
            "/blob-index/mark",
            {"keys": [K], "backend": "bambuddy", "cutoff": "2026-01-01T00:00:00Z", "meta": {}},
        ),
    ):
        with pytest.raises(CrudRefusedError) as refused:
            await asyncio.to_thread(client.request, method, f"{BASE}{path}", json=body)
        assert refused.value.status == 428, (method, path)
    assert BlobIndex(pool).get(K) is None
    assert _ledger(pool) == 0


async def test_the_worker_put_answers_the_replaced_object(
    index: RemoteBlobIndex, pool: Pool
) -> None:
    assert await asyncio.to_thread(index.put, K, _ref("a"), slug="demo", meta={"x": 1}) is None
    previous = await asyncio.to_thread(index.put, K, _ref("b"), slug="demo", meta={})
    assert previous == _ref("a")
    named = BlobIndex(pool).get(K)
    assert named is not None and named.ref == _ref("b")


async def test_a_reuse_of_a_freed_object_raises_reuse_lost_on_the_worker(
    index: RemoteBlobIndex, pool: Pool
) -> None:
    """`ReuseLostError` is a 409 ``reuse-lost`` and the same error again on the worker,
    so `ContentStore` uploads its own copy; nothing is written or recorded."""
    with pytest.raises(ReuseLostError):
        await asyncio.to_thread(index.put, K, _ref("gone"), slug="demo", meta={}, reuse=True)
    with pytest.raises(ReuseLostError):
        await asyncio.to_thread(
            index.swap, K, _ref("gone"), expected=None, slug="demo", meta={}, reuse=True
        )
    assert BlobIndex(pool).get(K) is None
    assert _ledger(pool) == 0


async def test_a_deadlock_retried_in_the_api_records_only_the_write_that_landed(
    index: RemoteBlobIndex, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API's deadlock retry wraps the ledger row too: a victim's half (its row
    written, then broken) rolls back whole, and the retry answers the true previous."""
    sql = BlobIndex(pool)
    sql.put(K, _ref("a"), slug="demo", meta={})
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
    previous = await asyncio.to_thread(index.put, K, _ref("b"), slug="demo", meta={})
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
        await asyncio.to_thread(index.put, K, _ref("b"), slug="demo", meta={})
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
        sql.put(K1, p, slug="demo", meta={})
        sql.put(K2, o, slug="demo", meta={})
        start = threading.Barrier(2)
        results: dict[str, str] = {}
        await asyncio.gather(
            asyncio.to_thread(_racing_put, index, start, results, K1, o),
            asyncio.to_thread(_racing_put, index, start, results, K2, p),
        )
        assert set(results.values()) <= {"ok", "lost"}, results
        assert "ok" in results.values()


# -- swap, mark, touch and the deletes (R14, R15) ------------------------------


async def test_swap_is_a_compare_and_swap(index: RemoteBlobIndex, pool: Pool) -> None:
    assert await asyncio.to_thread(index.swap, K, _ref("a"), expected=None, slug="demo", meta={})
    assert not await asyncio.to_thread(
        index.swap, K, _ref("b"), expected=None, slug="demo", meta={}
    )
    assert await asyncio.to_thread(
        index.swap, K, _ref("b"), expected=_ref("a").sha256, slug="demo", meta={}
    )
    named = BlobIndex(pool).get(K)
    assert named is not None and named.ref == _ref("b")


async def test_a_delete_answers_the_row_and_resent_answers_it_again(
    client: CrudSyncClient, index: RemoteBlobIndex, pool: Pool
) -> None:
    sql = BlobIndex(pool)
    sql.put(K, _ref("a"), slug="demo", meta={"m": 1})
    stat = sql.get(K)
    key = uuid.uuid4().hex

    def send() -> Any:
        return client.request(
            "DELETE", f"{BASE}/blob-index/{K}?backend=bambuddy", idempotency_key=key
        ).json()

    first = await asyncio.to_thread(send)
    assert first is not None and first["ref"] == _ref("a").model_dump(mode="json")
    assert await asyncio.to_thread(send) == first
    sql.put(K, _ref("b", backend="s3"), slug="demo", meta={})
    # Another backend's row stays.
    assert await asyncio.to_thread(index.delete, K, backend="bambuddy") is None
    gone = await asyncio.to_thread(index.delete, K)
    assert gone is not None and gone.ref == _ref("b", backend="s3")
    assert stat is not None and stat.meta == {"m": 1}


async def test_delete_if_stale_takes_only_a_row_untouched_since_the_cutoff(
    index: RemoteBlobIndex, pool: Pool
) -> None:
    sql = BlobIndex(pool)
    sql.put(K, _ref("a"), slug="demo", meta={})
    before = sql.now() - timedelta(seconds=60)
    assert await asyncio.to_thread(index.delete_if_stale, K, before) is None
    cutoff = await asyncio.to_thread(index.now)
    stat = await asyncio.to_thread(index.delete_if_stale, K, cutoff, backend="bambuddy")
    assert stat is not None and stat.key == K and stat.ref == _ref("a")
    assert sql.get(K) is None


async def test_mark_merges_meta_into_the_stale_rows_only_and_touch_unmarks_none(
    index: RemoteBlobIndex, pool: Pool
) -> None:
    sql = BlobIndex(pool)
    a, b, c = ("a" * 64, "b" * 64, "c" * 64)
    for key in (a, b):
        sql.put(key, _ref(key[0]), slug="demo", meta={"x": 1})
    cutoff = await asyncio.to_thread(index.now)
    sql.put(c, _ref("c"), slug="demo", meta={"x": 1})
    await asyncio.to_thread(index.mark, [a, b, c], backend="bambuddy", cutoff=cutoff, swept=True)
    await asyncio.to_thread(index.touch, a)
    marked = {key: (stat.meta if (stat := sql.get(key)) else None) for key in (a, b, c)}
    assert marked == {a: {"x": 1, "swept": True}, b: {"x": 1, "swept": True}, c: {"x": 1}}
    touched = sql.get(a)
    assert touched is not None and touched.touched_at > cutoff


# -- the reads (R13, R16) ------------------------------------------------------


async def test_the_reads_answer_as_the_sql_index(index: RemoteBlobIndex, pool: Pool) -> None:
    sql = BlobIndex(pool)
    assert await asyncio.to_thread(index.get, K) is None
    sql.put(K, _ref("a"), slug="demo", meta={"n": [1, 2]})
    sql.put(K2, _ref("b", backend="s3"), slug="other", meta={})
    assert await asyncio.to_thread(index.get, K) == sql.get(K)
    assert await asyncio.to_thread(index.by_sha, "a" * 64, "piece", "bambuddy") == _ref("a")
    assert await asyncio.to_thread(index.by_sha, "a" * 64, "piece", "s3") is None
    assert await asyncio.to_thread(index.shares_backend_id, "bambuddy", "id-a")
    assert not await asyncio.to_thread(index.shares_backend_id, "bambuddy", "id-b")
    assert await asyncio.to_thread(index.recorded, "s3", "id-b")
    assert not await asyncio.to_thread(index.recorded, "s3", "id-a")
    assert await asyncio.to_thread(index.usage, "bambuddy") == sql.usage("bambuddy")
    for kinds, slug, backend in (
        (["piece"], None, None),
        (["piece"], None, "s3"),
        (["asset"], None, None),
        (["piece", "font"], "demo", None),
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
    BlobIndex(pool).put(K, ref, slug=None, meta={})
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
    assert await asyncio.to_thread(index.put, K, _ref("a"), slug="demo", meta={}) is None
    info.attempt = 2
    previous = await asyncio.to_thread(index.put, K, _ref("b"), slug="demo", meta={})
    assert previous == _ref("a")
    named = BlobIndex(pool).get(K)
    assert named is not None and named.ref == _ref("b")


# -- keys only the API writes (#5298 Task 18, final review C1, M1) -----------------

#: A key of every other service's rows in `store_blobs`, and of the API's own: the render
#: credential writes none of them.
FOREIGN_KEYS = [
    "library-file-1",
    "workspace-" + "a" * 64,
    "customer-file-" + "a" * 64,
    "request-file-" + "a" * 64,
    "customer-preview-" + "a" * 64,
    "listing-media-" + "a" * 64,
    "slice-" + "c" * 64,
    "asset-" + "a" * 64,
    "font-dejavu",
    "src-demo-abc",
    "quote-" + "a" * 64,
    "lib3-" + "a" * 48 + "-0",
    "k",
]


@pytest.mark.parametrize("key", FOREIGN_KEYS)
async def test_the_worker_cannot_store_a_key_that_is_not_a_render_jobs(
    index: RemoteBlobIndex, pool: Pool, key: str
) -> None:
    """An allowlist, not a denylist: template Python in the render pod can reach the
    render credential, and with a put of ``library-file-<id>`` naming its own 3MF it
    would get its file sliced and printed; with a delete of ``workspace-<sha>`` it would
    drop an agent's file. An upload's copy is what an adopt writes the upload's row from,
    and a marked ``slice-`` key gets the print worker a read URL. Nor may it delete one:
    a delete whose object release then failed would leave the object untracked. A touch
    keeps a row from its sweep: only a library file's object and a snapshot are touched.
    A font's, snapshot's or library object's forget goes to the API's ``forget-copy``."""
    sql = BlobIndex(pool)
    sql.put(key, _ref("b"), slug=None, meta={"name": "real"})
    for write in (
        lambda: index.put(key, _ref("a"), slug=None, meta={"name": "forged"}),
        lambda: index.swap(key, _ref("a"), expected=None, slug=None, meta={}),
        lambda: index.swap(key, _ref("a"), expected=_ref("b").sha256, slug=None, meta={}),
        lambda: index.mark([key], backend="bambuddy", cutoff=sql.now(), name="forged"),
        lambda: index.mark([K, key], backend="bambuddy", cutoff=sql.now(), name="forged"),
        lambda: index.delete(key, backend="bambuddy"),
        lambda: index.delete_if_stale(key, sql.now(), backend="bambuddy"),
    ):
        with pytest.raises(CrudRefusedError) as refused:
            await asyncio.to_thread(write)
        assert refused.value.status == 403
        assert refused.value.problem["reason"] == "api-owned-key"
    held = sql.get(key)
    assert held is not None and held.meta == {"name": "real"} and held.ref == _ref("b")
    if key.startswith(("lib3-", "src-")):
        await asyncio.to_thread(index.touch, key)
    else:
        with pytest.raises(CrudRefusedError) as refused:
            await asyncio.to_thread(index.touch, key)
        assert refused.value.status == 403
        assert refused.value.problem["reason"] == "api-owned-key"
    if key.startswith(("font-", "src-")):
        forgotten = await asyncio.to_thread(index.delete, key)  # through ``forget-copy``
        assert forgotten is not None and forgotten.ref == _ref("b")
        assert sql.get(key) is None
        assert await asyncio.to_thread(index.delete, key) is None  # natural
    elif key.startswith("lib3-"):
        # This API has no blob store to read it from (`state.store.content` is None, a
        # local store): nothing is forgotten. `test_a_library_object_*` read one.
        assert await asyncio.to_thread(index.delete, key) is None
        assert sql.get(key) is not None
    else:
        # An upload's copy is dropped through ``…/assets/{id}/drop-copy`` only; every
        # other key is its service's, and the API's sweep's.
        with pytest.raises(CrudRefusedError) as refused:
            await asyncio.to_thread(index.delete, key)
        assert refused.value.status == 403
        assert sql.get(key) is not None


@pytest.mark.parametrize("key", [K, "act-" + "a" * 40, "output-" + "0123456789abcdef" * 2 + "-3"])
async def test_the_worker_stores_and_drops_a_render_jobs_pieces(
    index: RemoteBlobIndex, pool: Pool, key: str
) -> None:
    """A piece's key, a template call's emitted files and an output's files."""
    sql = BlobIndex(pool)
    assert await asyncio.to_thread(index.put, key, _ref("a"), slug="demo", meta={}) is None
    assert await asyncio.to_thread(
        index.swap, key, _ref("b"), expected=_ref("a").sha256, slug="demo", meta={}
    )
    await asyncio.to_thread(index.mark, [key], backend="bambuddy", cutoff=sql.now(), swept=True)
    held = await asyncio.to_thread(index.get, key)
    assert held is not None and held.ref == _ref("b") and held.meta == {"swept": True}
    gone = await asyncio.to_thread(index.delete, key)
    assert gone is not None and gone.ref == _ref("b")


async def test_a_render_key_naming_another_kind_is_refused(
    index: RemoteBlobIndex, pool: Pool
) -> None:
    """No render key names another kind than a piece today; if one ever did, the render
    credential must not repoint, mark or drop it: refused, and nothing written."""
    sql = BlobIndex(pool)
    other = BlobRef(sha256="d" * 64, kind="slice", backend="bambuddy", backend_id="id-d", size=4)
    sql.put(K, other, slug=None, meta={"name": "real"})
    for write in (
        lambda: index.put(K, _ref("a"), slug=None, meta={}),
        lambda: index.swap(K, _ref("a"), expected=other.sha256, slug=None, meta={}),
        lambda: index.swap(K, _ref("a"), expected=None, slug=None, meta={}),
        lambda: index.mark([K], backend="bambuddy", cutoff=sql.now(), name="forged"),
        lambda: index.delete(K, backend="bambuddy"),
        lambda: index.delete(K),
        lambda: index.delete_if_stale(K, sql.now(), backend="bambuddy"),
    ):
        with pytest.raises(CrudRefusedError) as refused:
            await asyncio.to_thread(write)
        assert refused.value.status == 403
        assert refused.value.problem["reason"] == "api-owned-key"
    held = sql.get(K)
    assert held is not None and held.ref == other and held.meta == {"name": "real"}
    assert _ledger(pool) == 0


async def test_a_ref_of_another_kind_is_refused(index: RemoteBlobIndex, pool: Pool) -> None:
    """The render worker stores pieces only."""
    other = BlobRef(sha256="d" * 64, kind="slice", backend="bambuddy", backend_id="id-d", size=4)
    for write in (
        lambda: index.put(K, other, slug=None, meta={}),
        lambda: index.swap(K, other, expected=None, slug=None, meta={}),
    ):
        with pytest.raises(CrudRefusedError) as refused:
            await asyncio.to_thread(write)
        assert refused.value.status == 403
    assert BlobIndex(pool).get(K) is None


@pytest.mark.parametrize(
    "key",
    [
        "library-file-1",
        "workspace-" + "a" * 64,
        "customer-file-" + "a" * 64,
        "request-file-" + "a" * 64,
        "customer-preview-" + "a" * 64,
        "listing-media-" + "a" * 64,
        "slice-" + "c" * 64,
    ],
)
async def test_the_worker_cannot_read_another_services_row(
    index: RemoteBlobIndex, pool: Pool, key: str
) -> None:
    """A row's meta and slug tell a customer file's name and an attachment's scope."""
    BlobIndex(pool).put(key, _ref("b"), slug="secret", meta={"name": "customer.3mf"})
    with pytest.raises(CrudRefusedError) as refused:
        await asyncio.to_thread(index.get, key)
    assert refused.value.status == 403
    assert refused.value.problem["reason"] == "api-owned-key"
    assert "customer.3mf" not in str(refused.value.problem)


async def test_the_worker_reads_what_it_renders_from(index: RemoteBlobIndex, pool: Pool) -> None:
    """Its pieces, a library file's object Arrange lays out, a snapshot, a font and an
    upload's copy."""
    sql = BlobIndex(pool)
    for key in (
        K,
        "lib3-" + "a" * 48 + "-0",
        "src-demo-abc",
        "font-dejavu",
        "asset-" + "a" * 64,
    ):
        sql.put(key, _ref("b"), slug=None, meta={})
        assert await asyncio.to_thread(index.get, key) == sql.get(key)


async def test_the_worker_lists_and_finds_only_the_kinds_it_reads(
    index: RemoteBlobIndex,
) -> None:
    """It lists the kinds it reads (`FontMirror.sync`), and finds by SHA-256 only a piece,
    the one kind it stores (`ContentStore.put`)."""
    for read in (
        lambda: index.stats(None),
        lambda: index.stats(["piece", "slice"]),
        lambda: index.stats(["workspace"]),
        lambda: index.by_sha("a" * 64, "library", "bambuddy"),
        lambda: index.by_sha("a" * 64, "media", "bambuddy"),
        lambda: index.by_sha("a" * 64, "workspace", "bambuddy"),
        lambda: index.by_sha("a" * 64, "font", "bambuddy"),
        lambda: index.by_sha("a" * 64, "asset", "bambuddy"),
        lambda: index.by_sha("a" * 64, "snapshot", "bambuddy"),
    ):
        with pytest.raises(CrudRefusedError) as refused:
            await asyncio.to_thread(read)
        assert refused.value.status == 403
        assert refused.value.problem["reason"] == "api-owned-key"
    assert await asyncio.to_thread(index.stats, ["font"]) == []
    assert await asyncio.to_thread(index.by_sha, "a" * 64, "piece", "bambuddy") is None


async def test_forget_copy_takes_only_a_fonts_or_snapshots_key(client: CrudSyncClient) -> None:
    for key in ("asset-" + "a" * 64, "piece-x", K):
        with pytest.raises(CrudRefusedError) as refused:
            await asyncio.to_thread(client.request, "POST", f"{BASE}/blob-index/{key}/forget-copy")
        assert refused.value.status == 403


# -- a library file's object the worker cannot read (#5298 final review I-3) ----------

LIB3 = "lib3-" + "a" * 48 + "-0"


@pytest.fixture(autouse=True)
def no_forget_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with no key read for a forget (`crud_render._recently_checked`)."""
    monkeypatch.setattr(crud_render, "_LAST_CHECKED", {})


def test_the_compare_and_delete_keeps_a_row_stored_again(pool: Pool) -> None:
    """`delete_if_names`: a row that names another object than the one read stays."""
    sql = BlobIndex(pool)
    sql.put(LIB3, _ref("b"), slug=None, meta={})
    assert sql.delete_if_names(LIB3, _ref("a")) is None
    held = sql.get(LIB3)
    assert held is not None and held.ref == _ref("b")
    taken = sql.delete_if_names(LIB3, _ref("b"))
    assert taken is not None and taken.ref == _ref("b")
    assert sql.get(LIB3) is None


def test_a_swap_kept_to_pieces_leaves_a_row_of_another_kind(pool: Pool) -> None:
    """`swap_in`'s ``only_kind``, checked under the row's lock: a row that holds another
    kind is not rewritten, even when it names the expected bytes."""
    sql = BlobIndex(pool)
    other = BlobRef(sha256="d" * 64, kind="slice", backend="bambuddy", backend_id="id-d", size=4)
    sql.put(K, other, slug=None, meta={"name": "real"})
    with pool.connection() as conn:
        swapped = sql.swap_in(
            conn,
            K,
            _ref("a"),
            expected=other.sha256,
            slug="demo",
            meta={},
            only_kind="piece",
        )
    assert swapped is False
    held = sql.get(K)
    assert held is not None and held.ref == other and held.meta == {"name": "real"}
    # The same swap without the bound rewrites it: the bound is what kept it.
    with pool.connection() as conn:
        assert sql.swap_in(conn, K, _ref("a"), expected=other.sha256, slug="demo", meta={})


@pytest.fixture
async def api_content(tmp_path: Path, pool: Pool) -> AsyncIterator[ContentStore]:
    """The API's blob store, which reads the object before it forgets a library row."""
    store = local_content(tmp_path / "remote", pool)
    try:
        yield store
    finally:
        await store.aclose()


@pytest.mark.parametrize("damage", ["corrupt", "missing", "none"])
async def test_a_library_object_is_forgotten_only_when_the_api_cannot_read_it(
    api_content: ContentStore, pool: Pool, damage: str
) -> None:
    """The render credential may not drop a library row the API reads whole: a 409
    ``copy-intact``, and the row stays. One whose object is gone or altered is dropped,
    so the next Arrange of the file stores it again."""
    ref = await api_content.put("piece", b"objects", name="o", scope=BlobScope(), key=LIB3)
    if damage == "corrupt":
        await overwrite_object(api_content, ref.backend_id, b"altered")
    elif damage == "missing":
        await api_content.backend.remove(ref.backend_id)
    with threaded_render_crud(pool, content=api_content) as client:
        if damage == "none":
            with pytest.raises(CrudRefusedError) as refused:
                await asyncio.to_thread(
                    client.request, "POST", f"{BASE}/blob-index/{LIB3}/forget-copy"
                )
            assert refused.value.status == 409
            assert refused.value.problem["reason"] == "copy-intact"
        forgotten = await asyncio.to_thread(RemoteBlobIndex(client).delete, LIB3)
    held = api_content.index.get(LIB3)
    if damage == "none":
        assert forgotten is None  # the worker reads the 409 as nothing taken
        assert held is not None and held.ref == ref
    else:
        assert forgotten is not None and forgotten.ref == ref
        assert held is None


async def test_a_worker_fetching_an_altered_library_object_has_the_api_forget_it(
    api_content: ContentStore, pool: Pool, tmp_path: Path
) -> None:
    """`CachedBlobStore._fetch` forgets a piece it cannot read; for a library file's
    object that goes through ``forget-copy``, not the refused delete."""
    source = tmp_path / "objects"
    source.mkdir()
    (source / "layout.json").write_text("{}")
    ref = await api_content.put("piece", pack_dir(source), name="o", scope=BlobScope(), key=LIB3)
    await overwrite_object(api_content, ref.backend_id, b"altered")
    with threaded_render_crud(pool, content=api_content) as client:
        worker = CachedBlobStore(
            LocalBlobStore(tmp_path / "worker"),
            ContentStore(api_content.backend, RemoteBlobIndex(client)),
            max_bytes=1 << 30,
            min_age=0,
        )
        assert await worker.fetch(LIB3) is False
    assert api_content.index.get(LIB3) is None
