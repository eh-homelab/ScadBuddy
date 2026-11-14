"""The render credential's allowlists and the withheld fallback key, without a database
(#5298 final review C1, M1, I3). The routes' own tests are `test_crud_blob_index.py`,
`test_crud_render_refs.py` and `test_crud_render_settings.py`."""

from __future__ import annotations

import pytest

from scadbuddy.api import crud_render
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import RenderStoreSettings
from scadbuddy.store.bambuddy import render_target
from scadbuddy.store.content_models import BlobRef

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


def _ref(kind: str) -> BlobRef:
    return BlobRef(sha256="a" * 64, kind=kind, backend="s3", backend_id="x", size=1)


@pytest.mark.parametrize("key", FOREIGN + [LIBRARY_PIECE])
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
def test_a_ref_of_another_kind_is_refused(kind: str) -> None:
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
