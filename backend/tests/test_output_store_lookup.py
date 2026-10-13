"""#2755: a listing's details find each output's directory without searching for it."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.api.outputs import detail
from scadbuddy.library.outputs import OutputNotFoundError, OutputStore
from tests.support.arrange import copied_output, saved_output


def _count_globs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    patterns: list[str] = []
    real = Path.glob

    def counted(self: Path, pattern: str, *args: Any, **kwargs: Any) -> Any:
        patterns.append(pattern)
        return real(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", counted)
    return patterns


async def test_a_listing_and_its_details_glob_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every read of an output by id used to glob every model's directory (~90 ms each
    on the production NAS), about ten per output for a listing's details."""
    paths, meta, _ = await saved_output(tmp_path)
    for n in range(4):
        copied_output(paths, meta, slug="demo", output_id=f"{n:032x}")
    store = OutputStore(paths)
    patterns = _count_globs(monkeypatch)

    metas = store.list_for("demo")
    details = [detail(store, m, []) for m in metas]

    assert len(details) == 5
    assert patterns == ["*/meta.json"]


async def test_a_moved_or_deleted_output_is_looked_for_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, meta, _ = await saved_output(tmp_path)
    store = OutputStore(paths)
    before = store.directory(meta.id)
    moved = paths.outputs / "elsewhere" / meta.id
    moved.parent.mkdir(parents=True)
    shutil.move(before, moved)
    assert store.directory(meta.id) == moved
    shutil.rmtree(moved)
    with pytest.raises(OutputNotFoundError):
        store.directory(meta.id)


async def test_a_cached_directory_left_without_its_record_is_not_found(tmp_path: Path) -> None:
    """#2909: a half-finished delete, or a directory made again, keeps the directory but
    not its meta.json; the cached path is not taken for the output then."""
    paths, meta, _ = await saved_output(tmp_path)
    store = OutputStore(paths)
    directory = store.directory(meta.id)
    (directory / "meta.json").unlink()

    assert directory.is_dir()
    with pytest.raises(OutputNotFoundError):
        store.get(meta.id)


async def test_a_deleted_output_leaves_no_cached_directory(tmp_path: Path) -> None:
    """#2909: delete forgets the output's directory, so the map does not only grow."""
    paths, meta, _ = await saved_output(tmp_path)
    store = OutputStore(paths)
    store.directory(meta.id)

    store.delete(meta.id)

    assert meta.id not in store._dirs
