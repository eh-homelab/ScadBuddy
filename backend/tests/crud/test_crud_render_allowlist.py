"""The render credential's allowlists and the withheld fallback key, without a database
(#5298 final review C1, M1, I3). The routes' own tests are `test_crud_blob_index.py`,
`test_crud_render_refs.py` and `test_crud_render_settings.py`."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from scadbuddy.api import crud_render
from scadbuddy.api.deps import AppState
from scadbuddy.auth.service_credentials import ServiceAuth
from scadbuddy.core.problems import ApiError
from scadbuddy.crud import idempotency
from scadbuddy.crud import render as crud_worker
from scadbuddy.crud.client import CrudSyncClient
from scadbuddy.library.settings_store import RenderStoreSettings
from scadbuddy.slicing.transfer import SLICED_OUTPUT_META
from scadbuddy.store.bambuddy import render_target
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import (
    BlobCorruptError,
    BlobKind,
    BlobRef,
    BlobScope,
    BlobStat,
)
from scadbuddy.store.local import LocalContentBackend
from scadbuddy.store.s3 import S3ContentBackend
from scadbuddy.workflows.pipelines import TRANSFER

_NOW = datetime(2026, 11, 14, tzinfo=UTC)

FOREIGN = [
    "library-file-1",
    "workspace-" + "a" * 64,
    "customer-file-" + "a" * 64,
    "request-file-" + "a" * 64,
    "customer-preview-" + "a" * 64,
    "listing-media-" + "a" * 64,
    "slice-" + "a" * 64,
    "asset-" + "a" * 64,
    "font-dejavu",
    "src-demo-abc",
    "quote-" + "a" * 64,
    "A" * 64,
    "a" * 63,
    "a" * 64 + "\n",
    "act-" + "a" * 39,
    "output-x-0",
]
RENDER = ["0123456789abcdef" * 4, "act-" + "a" * 40, "output-" + "a" * 32 + "-12"]
LIBRARY_PIECE = "lib3-" + "a" * 48 + "-0"


def _ref(kind: BlobKind) -> BlobRef:
    return BlobRef(sha256="a" * 64, kind=kind, backend="s3", backend_id="x", size=1)


@pytest.mark.parametrize("key", [*FOREIGN, LIBRARY_PIECE])
def test_a_write_of_any_key_but_a_render_jobs_is_refused(key: str) -> None:
    with pytest.raises(ApiError) as refused:
        crud_render._worker_writable(key)
    assert refused.value.status == 403
    assert refused.value.extensions["reason"] == crud_render.API_OWNED_KEY
    with pytest.raises(ApiError):
        crud_render._worker_writable(RENDER[0], key)


@pytest.mark.parametrize("key", RENDER)
def test_a_render_jobs_piece_is_written(key: str) -> None:
    crud_render._worker_writable(key, ref=_ref("piece"))


@pytest.mark.parametrize("kind", ["slice", "asset", "font", "snapshot", "library", "workspace"])
def test_a_ref_of_another_kind_is_refused(kind: BlobKind) -> None:
    with pytest.raises(ApiError) as refused:
        crud_render._worker_writable(RENDER[0], ref=_ref(kind))
    assert refused.value.status == 403


@pytest.mark.parametrize("value", ["sliced", "other", None])
def test_meta_that_names_the_slicers_output_mark_is_refused(value: str | None) -> None:
    """The print worker's read URL needs `SLICED_OUTPUT_META`, which only the API's
    `Transfers.finish_put` writes (#6179): a render credential writes no key of it, with
    any value, beside any other meta."""
    for key in SLICED_OUTPUT_META:
        with pytest.raises(ApiError) as refused:
            crud_render._worker_writable(RENDER[0], ref=_ref("piece"), meta={key: value})
        assert refused.value.status == 403
        assert refused.value.extensions["reason"] == crud_render.API_OWNED_KEY
        with pytest.raises(ApiError):
            crud_render._worker_writable(RENDER[0], meta={"dir": "x", key: value})
    crud_render._worker_writable(RENDER[0], ref=_ref("piece"), meta={"dir": "x"})
    crud_render._worker_writable(RENDER[0], ref=_ref("piece"), meta={})


def _no_database(*_: object, **__: object) -> None:
    raise AssertionError("the write reached the database")


def _forged(route: str, kind: BlobKind, meta: dict[str, Any]) -> Awaitable[Any]:
    """The route's write of ``RENDER[0]`` with ``kind`` and ``meta``, as a render
    credential sends it."""
    state = cast(AppState, SimpleNamespace())
    ref = _ref(kind)
    if route == "put":
        body = crud_render.IndexPut(ref=ref, slug=None, meta=meta)
        return crud_render.index_put(RENDER[0], body, state, "k")
    if route == "swap":
        swap = crud_render.IndexSwap(ref=ref, slug=None, meta=meta, expected=None)
        return crud_render.index_swap(RENDER[0], swap, state, "k")
    mark = crud_render.IndexMark(keys=[RENDER[0]], backend="s3", cutoff=_NOW, meta=meta)
    return crud_render.index_mark(mark, state, "k")


# The mark itself, and each of its keys with another value beside other meta (#6513).
_FORGED_META = [
    dict(SLICED_OUTPUT_META),
    *({"dir": "x", key: "other"} for key in SLICED_OUTPUT_META),
]


@pytest.mark.parametrize("meta", _FORGED_META)
@pytest.mark.parametrize("route", ["put", "swap", "mark"])
async def test_no_render_write_forges_the_slicers_output_mark(
    route: str, meta: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each index write the render credential reaches refuses the mark before it opens a
    transaction (#6179), whatever the value and whatever meta is beside it."""
    monkeypatch.setattr(crud_render, "_indexed", _no_database)
    with pytest.raises(ApiError) as refused:
        await _forged(route, "piece", meta)
    assert refused.value.status == 403
    assert refused.value.extensions["reason"] == crud_render.API_OWNED_KEY


@pytest.mark.parametrize("route", ["put", "swap"])
async def test_no_render_write_stores_a_slice(route: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A put or swap of kind ``slice`` is refused before it opens a transaction, so a
    render credential cannot write the kind a transfer URL needs."""
    monkeypatch.setattr(crud_render, "_indexed", _no_database)
    with pytest.raises(ApiError) as refused:
        await _forged(route, "slice", {})
    assert refused.value.extensions["reason"] == crud_render.API_OWNED_KEY


def test_a_row_read_under_its_lock_of_another_kind_is_refused() -> None:
    crud_render._kept_piece(None)
    crud_render._kept_piece(_ref("piece"))
    with pytest.raises(ApiError):
        crud_render._kept_piece(_ref("library"))


@pytest.mark.parametrize("key", [*FOREIGN[:7], "quote-" + "a" * 64, "k"])
def test_a_hold_or_read_of_another_services_key_is_refused(key: str) -> None:
    assert crud_render.HELD_KEY.fullmatch(key) is None
    with pytest.raises(ApiError) as refused:
        crud_render._worker_readable(key)
    assert refused.value.status == 403


@pytest.mark.parametrize(
    "key", [*RENDER, LIBRARY_PIECE, "src-demo-abc", "font-dejavu", "asset-" + "a" * 64]
)
def test_the_worker_reads_what_it_renders_from(key: str) -> None:
    crud_render._worker_readable(key)


def test_the_worker_holds_its_pieces_and_the_library_objects_it_lays_out() -> None:
    for key in [*RENDER, LIBRARY_PIECE]:
        assert crud_render.HELD_KEY.fullmatch(key) is not None


def test_only_the_kinds_the_worker_reads_are_listed() -> None:
    crud_render._worker_kinds(["font"])
    crud_render._worker_kinds(["piece", "snapshot", "asset"])
    for kinds in (None, ["slice"], ["piece", "library"], ["workspace"]):
        with pytest.raises(ApiError):
            crud_render._worker_kinds(kinds)


@pytest.mark.parametrize(
    "kind", ["snapshot", "asset", "font", "slice", "library", "workspace", "media"]
)
def test_only_a_piece_is_found_by_sha(kind: BlobKind) -> None:
    crud_render._worker_finds("piece")
    with pytest.raises(ApiError) as refused:
        crud_render._worker_finds(kind)
    assert refused.value.extensions["reason"] == crud_render.API_OWNED_KEY


@pytest.mark.parametrize("key", [*RENDER, LIBRARY_PIECE, "src-demo-abc"])
def test_the_worker_touches_what_it_holds_and_the_snapshot_it_renders_from(key: str) -> None:
    crud_render._worker_touchable(key)


@pytest.mark.parametrize("key", [*FOREIGN[:9], "quote-" + "a" * 64, "k"])
def test_a_touch_of_another_services_key_is_refused(key: str) -> None:
    """A touch keeps a row from its sweep: not for a font's or upload's copy either."""
    with pytest.raises(ApiError) as refused:
        crud_render._worker_touchable(key)
    assert refused.value.status == 403
    assert refused.value.extensions["reason"] == crud_render.API_OWNED_KEY


def test_only_a_library_object_is_forgotten_after_a_read() -> None:
    assert crud_render.LIBRARY_PIECE_KEY.fullmatch(LIBRARY_PIECE)
    for key in [*RENDER, *FOREIGN]:
        assert crud_render.LIBRARY_PIECE_KEY.fullmatch(key) is None


def _settings(*, render_key: bool) -> RenderStoreSettings:
    return RenderStoreSettings(
        store_backend="bambuddy",
        bambuddy_url="http://bambuddy.test",
        library_folder_id=1,
        api_key="render" if render_key else "full",
        key_is_fallback=not render_key,
    )


def test_the_full_key_is_withheld_and_a_render_key_released() -> None:
    fallback = crud_render.withheld(_settings(render_key=False))
    assert (fallback.api_key, fallback.key_is_fallback) == (None, True)
    assert crud_render.released(fallback) == []
    narrow = _settings(render_key=True)
    assert crud_render.withheld(narrow) is narrow
    assert crud_render.released(narrow) == ["bambuddy_render_api_key"]


def test_a_worker_without_the_full_key_names_the_render_key_setting() -> None:
    with pytest.raises(ApiError) as refused:
        render_target(crud_render.withheld(_settings(render_key=False)))
    assert "bambuddy_render_api_key" in refused.value.detail
    # The API's own read keeps the full key, and its own target works.
    assert render_target(_settings(render_key=False)).config.api_key == "full"
    assert render_target(_settings(render_key=True)).config.api_key == "render"


# -- forgetting a library file's object (#5298 final review N-1, N-2) ----------------

_LIB_REF = BlobRef(sha256="a" * 64, kind="piece", backend="s3", backend_id="x", size=1)
_LIB_STAT = BlobStat(key=LIBRARY_PIECE, ref=_LIB_REF, slug=None, meta={}, touched_at=_NOW)


class _Index:
    """The rows: ``LIBRARY_PIECE`` names `_LIB_REF`; ``restored`` makes the
    compare-and-delete find another object (the row stored again meanwhile)."""

    def __init__(self, *, restored: bool = False) -> None:
        self.restored = restored
        self.taken: list[BlobRef] = []

    def get(self, key: str) -> BlobStat | None:
        return _LIB_STAT if key == LIBRARY_PIECE else None

    def delete_if_names(self, key: str, ref: BlobRef) -> BlobStat | None:
        if self.restored:
            return None
        self.taken.append(ref)
        return _LIB_STAT


class _Backend:
    def __init__(self, *, exists: bool) -> None:
        self._exists = exists

    async def exists(self, backend_id: str) -> bool:
        return self._exists


class _Content:
    """The API's blob store: ``damage`` is how its read of the object goes."""

    name = "s3"

    def __init__(self, damage: str) -> None:
        self.damage = damage
        self.backend = _Backend(exists=damage != "missing")
        self.reads = 0

    async def get(self, ref: BlobRef) -> AsyncGenerator[bytes]:
        self.reads += 1
        if self.damage == "slow":
            await asyncio.sleep(10)
        yield b"bytes"
        if self.damage == "corrupt":
            raise BlobCorruptError(ref.sha256)


@pytest.fixture
def forget(monkeypatch: pytest.MonkeyPatch) -> Callable[[_Content, _Index], Awaitable[Any]]:
    monkeypatch.setattr(crud_render, "_LAST_CHECKED", {})

    def run(content: _Content, index: _Index) -> Awaitable[Any]:
        monkeypatch.setattr(crud_render, "_index", lambda state: index)
        state = cast(AppState, SimpleNamespace(store=SimpleNamespace(content=content)))
        return crud_render._forget_unreadable(LIBRARY_PIECE, state)

    return run


def _intact(raised: pytest.ExceptionInfo[ApiError]) -> None:
    assert raised.value.status == 409
    assert raised.value.extensions["reason"] == crud_render.COPY_INTACT


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
async def test_an_unreadable_library_object_is_dropped_while_its_row_names_it(
    forget: Callable[[_Content, _Index], Awaitable[Any]], damage: str
) -> None:
    index = _Index()
    assert await forget(_Content(damage), index) == _LIB_STAT
    assert index.taken == [_LIB_REF]


async def test_a_row_stored_again_since_the_read_is_kept(
    forget: Callable[[_Content, _Index], Awaitable[Any]],
) -> None:
    """The drop is one compare-and-delete: a row that names another object by then stays,
    and the worker hears ``copy-intact``."""
    with pytest.raises(ApiError) as raised:
        await forget(_Content("corrupt"), _Index(restored=True))
    _intact(raised)


async def test_a_read_past_its_time_limit_keeps_the_row(
    forget: Callable[[_Content, _Index], Awaitable[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crud_render, "FORGET_READ_TIMEOUT", 0.05)
    index = _Index()
    with pytest.raises(ApiError) as raised:
        await forget(_Content("slow"), index)
    _intact(raised)
    assert index.taken == []


async def test_a_key_read_whole_is_not_read_again_within_the_cooldown(
    forget: Callable[[_Content, _Index], Awaitable[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render credential cannot make the API read one object over and over."""
    content = _Content("none")
    for _ in range(3):
        with pytest.raises(ApiError) as raised:
            await forget(content, _Index())
        _intact(raised)
    assert content.reads == 1
    # Past the cooldown, the next forget reads it again.
    monkeypatch.setattr(crud_render, "FORGET_CHECK_COOLDOWN", 0.0)
    with pytest.raises(ApiError):
        await forget(content, _Index())
    assert content.reads == 2


def test_the_cooldowns_record_stays_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crud_render, "_LAST_CHECKED", {})
    monkeypatch.setattr(crud_render, "FORGET_CHECKS_KEPT", 4)
    for n in range(10):
        assert not crud_render._recently_checked(f"lib3-{n}", float(n))
    assert len(crud_render._LAST_CHECKED) <= 4
    assert crud_render._recently_checked("lib3-9", 9.5)


def test_a_forget_copy_waits_longer_than_the_apis_whole_read() -> None:
    """The worker's forget-copy outlasts the API's read of a library object, so a slow
    read answers the worker instead of timing it out (#6228 review)."""
    from scadbuddy.crud import render as crud_worker

    calls: list[dict[str, Any]] = []

    class _Response:
        def json(self) -> None:
            return None

    class _Client:
        def request(self, method: str, path: str, **kwargs: Any) -> _Response:
            calls.append({"method": method, "path": path, **kwargs})
            return _Response()

    index = crud_worker.RemoteBlobIndex(cast(Any, _Client()))
    assert index.delete("lib3-" + "a" * 48 + "-0") is None
    assert calls[0]["path"].endswith("/forget-copy")
    assert calls[0]["timeout"] == crud_worker.FORGET_TIMEOUT
    assert crud_worker.FORGET_TIMEOUT > crud_render.FORGET_READ_TIMEOUT


# -- a render write is bound to its job and to its object's bytes (#6233) -------------

_JOB = "0123456789abcdef" * 2
_OUTPUT = f"output-{_JOB}-0"


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _Conn:
    """The write's connection: ``open_jobs`` are the render jobs that run, ``replies``
    the ledger's rows by Idempotency-Key; every other query finds no row."""

    def __init__(
        self,
        open_jobs: set[str],
        queries: list[str],
        replies: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.open_jobs = open_jobs
        self.queries = queries
        self.replies = replies or {}

    def execute(self, query: str, params: Any = None) -> _Rows:
        self.queries.append(query)
        if "FROM render_jobs" in query:
            return _Rows([{"id": job} for job in params[0] if job in self.open_jobs])
        if "FROM crud_requests" in query and params[1] in self.replies:
            return _Rows([self.replies[params[1]]])
        return _Rows([])

    def transaction(self) -> contextlib.nullcontext[None]:
        return contextlib.nullcontext()


class _WriteIndex:
    def put_in(self, *_: object, **__: object) -> None:
        return None

    def swap_in(self, *_: object, **__: object) -> bool:
        return True


def _written(
    monkeypatch: pytest.MonkeyPatch, open_jobs: set[str], queries: list[str] | None = None
) -> list[str]:
    """Run each index write on a `_Conn` with ``open_jobs``, its SQL kept in ``queries``;
    the routes whose write ran to its end."""
    reached: list[str] = []
    kept = [] if queries is None else queries

    def run(state: object, lock: str, key: str | None, route: str, body: object, write: Any) -> Any:
        answer = write(_WriteIndex(), _Conn(open_jobs, kept))
        reached.append(route)
        return answer

    monkeypatch.setattr(crud_render, "_indexed", run)
    return reached


def _write(
    route: str, key: str, ref: BlobRef, state: AppState, idempotency_key: str = "k"
) -> Awaitable[Any]:
    """The route's write of ``key`` (beside a piece's key, for a mark) as a render
    credential sends it."""
    if route == "put":
        body = crud_render.IndexPut(ref=ref, slug="demo", meta={})
        return crud_render.index_put(key, body, state, idempotency_key)
    if route == "swap":
        swap = crud_render.IndexSwap(ref=ref, slug="demo", meta={}, expected=None)
        return crud_render.index_swap(key, swap, state, idempotency_key)
    mark = crud_render.IndexMark(keys=[RENDER[0], key], backend="s3", cutoff=_NOW, meta={})
    return crud_render.index_mark(mark, state, idempotency_key)


class _Pool:
    """The API's pool for the reads before the write (`_replayed_or_open`): the ledger,
    then the job check."""

    def __init__(
        self, open_jobs: set[str], replies: dict[str, dict[str, Any]] | None = None
    ) -> None:
        self.open_jobs = open_jobs
        self.replies = replies
        self.queries: list[str] = []

    @contextlib.contextmanager
    def connection(self) -> Iterator[_Conn]:
        yield _Conn(self.open_jobs, self.queries, self.replies)


def _jobs_state(
    open_jobs: set[str],
    content: object = None,
    replies: dict[str, dict[str, Any]] | None = None,
) -> AppState:
    """A state whose pool knows ``open_jobs`` and the ledger's ``replies``, and whose
    store is ``content``."""
    return cast(
        AppState,
        SimpleNamespace(
            store=SimpleNamespace(content=content),
            settings_store=SimpleNamespace(pool=_Pool(open_jobs, replies)),
        ),
    )


_NO_STORE = _jobs_state(set())


@pytest.mark.parametrize("route", ["put", "swap", "mark"])
async def test_an_output_of_a_job_that_does_not_run_is_not_written(
    route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render credential cannot rewrite another job's outputs once that job settled,
    nor those of a job that never was: only a running job's outputs are written (#6233)."""
    reached = _written(monkeypatch, open_jobs={"f" * 32})
    with pytest.raises(ApiError) as refused:
        await _write(route, _OUTPUT, _ref("piece"), _jobs_state({"f" * 32}))
    assert refused.value.status == 403
    assert refused.value.extensions["reason"] == crud_render.JOB_CLOSED
    assert reached == []


@pytest.mark.parametrize("route", ["put", "swap"])
async def test_a_settled_jobs_output_is_refused_before_its_object_is_read(
    route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job check runs before `_verified` reads the object, so a write to a settled
    job's output costs no GET and hash before its 403 (#6534). The store here must not be
    read, and no row vouches for the ref, so a read would fail the test."""
    content = ContentStore(cast(Any, _Unread()), cast(Any, None))
    monkeypatch.setattr(crud_render, "_index", lambda _: _Vouched())
    monkeypatch.setattr(crud_render, "_indexed", _no_database)
    with pytest.raises(ApiError) as refused:
        await _write(route, _OUTPUT, _local_ref(b"output"), _jobs_state(set(), content))
    assert refused.value.status == 403
    assert refused.value.extensions["reason"] == crud_render.JOB_CLOSED


def _reply(route: str, key: str, ref: BlobRef, answer: dict[str, Any]) -> dict[str, Any]:
    """The ledger's row of a committed put or swap of ``key`` (`_write`'s body)."""
    if route == "put":
        name = "PUT /blob-index/{key}"
        body = crud_render.IndexPut(ref=ref, slug="demo", meta={}).model_dump(mode="json")
    else:
        name = "POST /blob-index/{key}/swap"
        swap = crud_render.IndexSwap(ref=ref, slug="demo", meta={}, expected=None)
        body = swap.model_dump(mode="json")
    digest = idempotency._digest({"key": key, **body})
    return {"route": name, "body_sha256": digest, "status": 200, "body": answer}


@pytest.mark.parametrize("route", ["put", "swap"])
async def test_a_committed_write_is_replayed_after_its_job_settled(
    route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A put or swap committed, its answer was lost, and the job settled before the
    worker sent it again with the same Idempotency-Key: the resend gets the first answer,
    with no job check and no read (#6538). A new key for the same write is still a 403
    ``job-closed``, unread. The store must not be read, and no write may run."""
    content = ContentStore(cast(Any, _Unread()), cast(Any, None))
    ref = _local_ref(b"output")
    answer: dict[str, Any] = {"previous": None} if route == "put" else {"swapped": True}
    state = _jobs_state(set(), content, {"k": _reply(route, _OUTPUT, ref, answer)})
    monkeypatch.setattr(crud_render, "_index", _no_database)
    monkeypatch.setattr(crud_render, "_indexed", _no_database)
    replayed = await _write(route, _OUTPUT, ref, state)
    assert replayed.model_dump(mode="json") == answer
    with pytest.raises(ApiError) as refused:
        await _write(route, _OUTPUT, ref, state, idempotency_key="new")
    assert refused.value.status == 403
    assert refused.value.extensions["reason"] == crud_render.JOB_CLOSED


@pytest.mark.parametrize("route", ["put", "swap"])
async def test_a_job_that_settles_after_the_first_check_is_refused_in_the_write(
    route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write's transaction checks the job again: it can settle between the checks."""
    reached = _written(monkeypatch, open_jobs=set())
    with pytest.raises(ApiError) as refused:
        await _write(route, _OUTPUT, _ref("piece"), _jobs_state({_JOB}))
    assert refused.value.extensions["reason"] == crud_render.JOB_CLOSED
    assert reached == []


@pytest.mark.parametrize("route", ["put", "swap", "mark"])
async def test_a_running_jobs_output_and_any_piece_are_written(
    route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reached = _written(monkeypatch, open_jobs={_JOB})
    await _write(route, _OUTPUT, _ref("piece"), _jobs_state({_JOB}))
    assert len(reached) == 1
    reached = _written(monkeypatch, open_jobs=set())
    for key in RENDER[:2]:
        await _write(route, key, _ref("piece"), _NO_STORE)
    assert len(reached) == 2


class _Vouched:
    """The rows `_verified` asks about (`BlobIndex.vouched`): whether one the API verified
    names the ref, and whether one of another kind names its object."""

    def __init__(self, verified: bool = False, foreign: bool = False) -> None:
        self.answer = (verified, foreign)
        self.asked: list[BlobRef] = []

    def vouched(self, ref: BlobRef) -> tuple[bool, bool]:
        self.asked.append(ref)
        return self.answer


class _Unread(LocalContentBackend):
    """A local backend that must not be read."""

    def __init__(self) -> None:
        super().__init__(Path("/nonexistent/unread"))

    def download(self, backend_id: str) -> AsyncGenerator[bytes]:
        raise AssertionError(f"the API read {backend_id}")


def _local_ref(data: bytes) -> BlobRef:
    """The ref of ``data`` as the worker stores it on the local backend: its object id is
    ``kind/sha256``."""
    sha = hashlib.sha256(data).hexdigest()
    return BlobRef(
        sha256=sha, kind="piece", backend="local", backend_id=f"piece/{sha}", size=len(data)
    )


@pytest.fixture
async def stored(tmp_path: Path) -> tuple[AppState, BlobRef, LocalContentBackend]:
    """The API's store, holding the object of ``b"real piece"``, and its true ref."""
    backend = LocalContentBackend(tmp_path / "remote")
    data = b"real piece"
    ref = _local_ref(data)
    assert await backend.upload("piece", data, name="p", scope=BlobScope()) == ref.backend_id
    content = ContentStore(backend, cast(Any, None))
    return _jobs_state(set(), content), ref, backend


@pytest.mark.parametrize("how", ["bytes", "size"])
@pytest.mark.parametrize("verified", [False, True])
@pytest.mark.parametrize("route", ["put", "swap"])
async def test_a_piece_whose_bytes_are_not_its_sha256_is_not_indexed(
    route: str,
    how: str,
    verified: bool,
    stored: tuple[AppState, BlobRef, LocalContentBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API reads the object and hashes it: it never trusts the worker's SHA-256, so a
    render credential cannot index bytes of its own under another piece's SHA-256, which
    a later put of that piece would reuse (#6233). ``bytes``: the object at the ref's id
    holds other bytes (the S3 keys can write them there). A row that names the ref
    without the API's mark (written before #6233) does not spare the read; only a
    verified one does (``verified``, `test_a_verified_row_spares_the_read`)."""
    state, ref, backend = stored
    if how == "bytes":
        (backend.root / ref.backend_id).write_bytes(b"forged piece")
    else:
        ref = ref.model_copy(update={"size": ref.size - 1})
    rows = _Vouched(verified=False)
    monkeypatch.setattr(crud_render, "_index", lambda _: rows)
    monkeypatch.setattr(crud_render, "_indexed", _no_database)
    with pytest.raises(ApiError) as refused:
        await _write(route, RENDER[0], ref, state)
    assert refused.value.status == 403
    assert refused.value.extensions["reason"] == crud_render.REF_MISMATCH
    assert rows.asked == [ref]


@pytest.mark.parametrize(
    "backend_id",
    [
        "asset/" + hashlib.sha256(b"real piece").hexdigest(),
        "piece/" + "f" * 64,
        "workspace/" + hashlib.sha256(b"real piece").hexdigest(),
    ],
)
@pytest.mark.parametrize("route", ["put", "swap"])
async def test_a_ref_to_another_object_than_its_bytes_name_is_refused_unread(
    route: str, backend_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render credential cannot index a render key at another service's object whose
    SHA-256 it knows (an upload's copy, a workspace file) and then read it as a piece: the
    ref's object id must be the one its kind and SHA-256 give, checked before any read
    or query (#6233)."""
    ref = _local_ref(b"real piece").model_copy(update={"backend_id": backend_id})
    content = ContentStore(cast(Any, _Unread()), cast(Any, None))
    state = _jobs_state(set(), content)
    monkeypatch.setattr(crud_render, "_index", _no_database)
    monkeypatch.setattr(crud_render, "_indexed", _no_database)
    with pytest.raises(ApiError) as refused:
        await _write(route, RENDER[0], ref, state)
    assert refused.value.status == 403
    assert refused.value.extensions["reason"] == crud_render.REF_MISMATCH


def test_an_s3_refs_object_is_the_one_its_bytes_name() -> None:
    """On S3 every upload goes to `object_key` (`S3ContentBackend.upload`)."""
    settings = SimpleNamespace(prefix="scadbuddy")
    backend = S3ContentBackend.__new__(S3ContentBackend)
    backend.settings = cast(Any, settings)
    content = ContentStore(backend, cast(Any, None))
    ref = _ref("piece").model_copy(update={"backend": "s3"})
    assert crud_render._bound_object(content, ref) == f"scadbuddy/piece/{ref.sha256}"


async def test_a_bambuddy_file_another_kinds_row_names_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bambuddy's ids are not named by the bytes, so a file a row of another kind names
    (a print library file, a customer's file) is refused before it is read."""

    class _Bambuddy:
        backend = "bambuddy"

        def download(self, backend_id: str) -> AsyncGenerator[bytes]:
            raise AssertionError(f"the API read {backend_id}")

    content = ContentStore(cast(Any, _Bambuddy()), cast(Any, None))
    state = _jobs_state(set(), content)
    ref = BlobRef(sha256="a" * 64, kind="piece", backend="bambuddy", backend_id="17", size=1)
    monkeypatch.setattr(crud_render, "_index", lambda _: _Vouched(foreign=True))
    monkeypatch.setattr(crud_render, "_indexed", _no_database)
    with pytest.raises(ApiError) as refused:
        await _write("put", RENDER[0], ref, state)
    assert refused.value.extensions["reason"] == crud_render.REF_MISMATCH


@pytest.mark.parametrize("route", ["put", "swap"])
async def test_a_ref_to_an_object_that_is_gone_is_a_lost_reuse(
    route: str,
    stored: tuple[AppState, BlobRef, LocalContentBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker then stores its own copy again, as after any reuse of a freed object."""
    state, _, _ = stored
    monkeypatch.setattr(crud_render, "_index", lambda _: _Vouched())
    monkeypatch.setattr(crud_render, "_indexed", _no_database)
    with pytest.raises(ApiError) as refused:
        await _write(route, RENDER[0], _local_ref(b"never stored"), state)
    assert refused.value.status == 409
    assert refused.value.extensions["reason"] == crud_render.REUSE_LOST


@pytest.mark.parametrize("route", ["put", "swap"])
async def test_a_true_ref_is_indexed_and_marked_verified(
    route: str,
    stored: tuple[AppState, BlobRef, LocalContentBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A true ref is read, indexed, and its row marked (`verified_at`), so the next write
    that names it is not read again. Without a blob store nothing is read or marked."""
    state, ref, _ = stored
    queries: list[str] = []
    reached = _written(monkeypatch, open_jobs=set(), queries=queries)
    monkeypatch.setattr(crud_render, "_index", lambda _: _Vouched())
    await _write(route, RENDER[0], ref, state)
    assert len(reached) == 1
    assert any("SET verified_at" in query for query in queries)
    queries.clear()
    await _write(route, RENDER[0], ref, _NO_STORE)
    assert len(reached) == 2
    assert not any("verified_at" in query for query in queries)


@pytest.mark.parametrize("route", ["put", "swap"])
async def test_a_verified_row_spares_the_read(
    route: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ref a verified row names (a reuse, a resend) costs one indexed query: its object
    is not read, and can be gone (the index's own reuse check answers that)."""
    content = ContentStore(cast(Any, _Unread()), cast(Any, None))
    state = _jobs_state(set(), content)
    queries: list[str] = []
    reached = _written(monkeypatch, open_jobs=set(), queries=queries)
    monkeypatch.setattr(crud_render, "_index", lambda _: _Vouched(verified=True))
    await _write(route, RENDER[0], _local_ref(b"never stored"), state)
    assert len(reached) == 1
    assert any("SET verified_at" in query for query in queries)


async def test_a_ref_on_another_backend_is_not_indexed(
    stored: tuple[AppState, BlobRef, LocalContentBackend], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid `/store/migrate` the API cannot read another backend's object to check it."""
    state, ref, _ = stored
    monkeypatch.setattr(crud_render, "_index", lambda _: _Vouched())
    monkeypatch.setattr(crud_render, "_indexed", _no_database)
    with pytest.raises(ApiError) as refused:
        await _write("put", RENDER[0], ref.model_copy(update={"backend": "s3"}), state)
    assert refused.value.status == 409
    assert refused.value.extensions["reason"] == crud_render.OTHER_BACKEND


class _Failing(LocalContentBackend):
    """A backend whose reads fail as an S3 or Bambuddy 5xx does."""

    def __init__(self) -> None:
        super().__init__(Path("/nonexistent/failing"))

    async def download(self, backend_id: str) -> AsyncGenerator[bytes]:
        raise ConnectionError(f"the store dropped the read of {backend_id}")
        yield b""  # pragma: no cover


class _Dropped(LocalContentBackend):
    """A backend whose read yields some chunks, then fails: a dropped stream. It counts
    the chunks it sent."""

    def __init__(self) -> None:
        super().__init__(Path("/nonexistent/dropped"))
        self.sent = 0

    async def download(self, backend_id: str) -> AsyncGenerator[bytes]:
        for chunk in (b"real ", b"pie"):
            self.sent += 1
            yield chunk
        raise ConnectionError(f"the store dropped the read of {backend_id}")


@pytest.mark.parametrize("failing", [_Failing, _Dropped])
@pytest.mark.parametrize("route", ["put", "swap"])
async def test_a_read_the_store_fails_is_a_transient_503(
    route: str, failing: Callable[[], LocalContentBackend], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that fails the read answers a 503, which the worker retries, not a 500
    (#6534). That holds before the first chunk, and after some chunks arrived (a dropped
    stream, #6538)."""
    backend = failing()
    content = ContentStore(cast(Any, backend), cast(Any, None))
    state = _jobs_state(set(), content)
    monkeypatch.setattr(crud_render, "_index", lambda _: _Vouched())
    monkeypatch.setattr(crud_render, "_indexed", _no_database)
    with pytest.raises(ApiError) as refused:
        await _write(route, RENDER[0], _local_ref(b"real piece"), state)
    assert refused.value.status == 503
    assert refused.value.extensions["reason"] == crud_render.STORE_UNAVAILABLE
    if isinstance(backend, _Dropped):
        assert backend.sent == 2


# -- the worker's side of a verified write (#6534) ------------------------------------

_CRUD = {"X-ScadBuddy-Crud": "1"}


class _Token:
    def token(self) -> str | None:
        return "t"

    def invalidate(self, token: str) -> None:
        pass


def _problem(status: int, reason: str) -> httpx.Response:
    body = {"type": "about:blank", "title": "x", "status": status, "detail": "d", "reason": reason}
    return httpx.Response(status, json=body, headers=_CRUD)


def _remote_index(
    answers: list[httpx.Response], seen: list[httpx.Request]
) -> crud_worker.RemoteBlobIndex:
    """A `RemoteBlobIndex` on a real `CrudSyncClient` whose API answers ``answers`` in
    turn; each request it sends is kept in ``seen``."""

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers.pop(0)

    client = CrudSyncClient(
        "http://api.example",
        "render",
        ServiceAuth(_Token()),
        transport=httpx.MockTransport(answer),
        unavailable_budget=5.0,
        backoff=0.01,
    )
    return crud_worker.RemoteBlobIndex(client)


def _sized(size: int) -> BlobRef:
    return _ref("piece").model_copy(update={"size": size})


def _send(index: crud_worker.RemoteBlobIndex, route: str, ref: BlobRef) -> object:
    if route == "put":
        return index.put(RENDER[0], ref, slug="demo", meta={})
    return index.swap(RENDER[0], ref, expected=None, slug="demo", meta={})


def _ok(route: str) -> httpx.Response:
    body = {"previous": None} if route == "put" else {"swapped": True}
    return httpx.Response(200, json=body, headers=_CRUD)


@pytest.mark.parametrize("route", ["put", "swap"])
def test_a_large_refs_write_waits_for_the_apis_read(route: str) -> None:
    """The API reads and hashes a new object before it answers a put or swap, so the
    worker's wait grows with the ref's size. A small ref keeps the floor, the client's
    default 10 s (#6534)."""
    seen: list[httpx.Request] = []
    index = _remote_index([_ok(route), _ok(route)], seen)
    _send(index, route, _sized(64 << 20))
    _send(index, route, _sized(4096))
    large, small = (request.extensions["timeout"]["read"] for request in seen)
    assert large == crud_worker.VERIFY_TIMEOUT_FLOOR + 64 * crud_worker.VERIFY_SECONDS_PER_MIB
    assert large > 10.0
    assert small == crud_worker.VERIFY_TIMEOUT_FLOOR == 10.0


@pytest.mark.parametrize("route", ["put", "swap"])
def test_a_huge_refs_write_waits_no_longer_than_the_activity_allows(route: str) -> None:
    """The wait stops at `VERIFY_TIMEOUT_CEILING`, half of one `TRANSFER`, the budget a
    render activity has for each piece it publishes: the other half is for the upload
    (#6538). Without the ceiling, a 256 MiB ref waited 266 s, past the 180 s."""
    seen: list[httpx.Request] = []
    index = _remote_index([_ok(route), _ok(route)], seen)
    _send(index, route, _sized(256 << 20))
    _send(index, route, _sized(4 << 30))
    waits = [request.extensions["timeout"]["read"] for request in seen]
    assert waits == [crud_worker.VERIFY_TIMEOUT_CEILING] * 2
    assert TRANSFER.total_seconds() / 2 >= crud_worker.VERIFY_TIMEOUT_CEILING


@pytest.mark.parametrize("route", ["put", "swap"])
def test_a_ref_on_another_backend_names_the_old_backend(route: str) -> None:
    """A 409 ``other-backend`` (a worker still on the old backend after a
    `/store/migrate`) is a named error that says so, not a plain refusal (#6534)."""
    index = _remote_index([_problem(409, crud_render.OTHER_BACKEND)], [])
    with pytest.raises(crud_worker.OtherBackendError) as raised:
        _send(index, route, _sized(1))
    assert "another backend than the API's store" in str(raised.value)


@pytest.mark.parametrize("route", ["put", "swap"])
def test_a_store_that_fails_the_apis_read_is_tried_again(route: str) -> None:
    """The client retries a 503 ``store-unavailable`` as a transient failure, with the
    same Idempotency-Key, inside its budget (#6534)."""
    seen: list[httpx.Request] = []
    index = _remote_index([_problem(503, crud_render.STORE_UNAVAILABLE), _ok(route)], seen)
    _send(index, route, _sized(1))
    assert len(seen) == 2
    assert seen[0].headers["Idempotency-Key"] == seen[1].headers["Idempotency-Key"]
