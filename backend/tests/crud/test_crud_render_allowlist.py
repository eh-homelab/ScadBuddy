"""The render credential's allowlists and the withheld fallback key, without a database
(#5298 final review C1, M1, I3). The routes' own tests are `test_crud_blob_index.py`,
`test_crud_render_refs.py` and `test_crud_render_settings.py`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.api import crud_render
from scadbuddy.api.deps import AppState
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import RenderStoreSettings
from scadbuddy.store.bambuddy import render_target
from scadbuddy.store.content_models import BlobCorruptError, BlobKind, BlobRef, BlobStat

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
