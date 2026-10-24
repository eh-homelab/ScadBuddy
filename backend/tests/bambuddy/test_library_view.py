"""#1753 — a library file's preview and mesh analysis, read from the 3MF the print
path fetches, as an output's are read from its own."""

from __future__ import annotations

import hashlib
import io
import os
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import httpx
import pytest
import respx
import trimesh

from scadbuddy.bambuddy import library_view
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.library_view import (
    NotViewableError,
    library_filaments,
    library_geometry,
    library_preview,
)
from scadbuddy.bambuddy.print_source import ReadOutOfMemoryError
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.geometry import NoSuchPlateError
from scadbuddy.render.glb import read_glb
from scadbuddy.render.objects3mf import PlateRead
from scadbuddy.render.split import ColourPart
from scadbuddy.store.download_cache import DownloadCache
from tests.bambuddy.conftest import BASE_URL

API = f"{BASE_URL}/api/v1"


def _box(x: float, y: float, z: float) -> trimesh.Trimesh:
    box: trimesh.Trimesh = trimesh.creation.box(extents=(x, y, z))
    box.apply_translation((x / 2, y / 2, z / 2))
    return box


def two_colour_3mf(tmp_path: Path) -> bytes:
    out = tmp_path / "model.3mf"
    write_bambu_3mf(
        [
            ColourPart(1, "Color 1", "#FF0000", _box(10, 10, 4)),
            ColourPart(2, "Color 2", "#0000FF", _box(6, 6, 9)),
        ],
        out,
        thumbnails=None,
    )
    return out.read_bytes()


def library_file(
    file_id: int, content: bytes, *, file_type: str = "3mf", hashed: bool = True
) -> respx.Route:
    respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": file_id,
                "filename": f"file-{file_id}.{file_type}",
                "file_type": file_type,
                "file_size": len(content),
                "file_hash": hashlib.sha256(content).hexdigest() if hashed else None,
            },
        )
    )
    return respx.get(f"{API}/library/files/{file_id}/download").mock(
        return_value=httpx.Response(200, content=content)
    )


@respx.mock
async def test_a_library_files_preview_is_its_3mfs_parts_in_their_colours(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    download = library_file(7, two_colour_3mf(tmp_path))
    cache = tmp_path / "cache"

    glb = await library_preview(bambuddy, cache, 7, 1)
    (tmp_path / "out.glb").write_bytes(glb)
    assert sorted(part.colour for part in read_glb(tmp_path / "out.glb")) == ["#0000FF", "#FF0000"]
    # The same file again is the cached preview: nothing downloaded twice.
    assert await library_preview(bambuddy, cache, 7, 1) == glb
    assert download.call_count == 1


@respx.mock
async def test_a_file_bambuddy_names_no_hash_for_is_read_each_time(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    download = library_file(7, two_colour_3mf(tmp_path), hashed=False)
    cache = tmp_path / "cache"
    await library_preview(bambuddy, cache, 7, 1)
    await library_preview(bambuddy, cache, 7, 1)
    assert download.call_count == 2
    # Nothing is kept: the only thing under the cache is the spool, empty again.
    assert not cache.exists() or not [path for path in cache.rglob("*") if path.is_file()]


@respx.mock
async def test_a_library_files_geometry_is_measured_per_plate(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    download = library_file(7, two_colour_3mf(tmp_path))
    cache = tmp_path / "cache"
    analysis = await library_geometry(bambuddy, cache, 7, 1)
    assert (analysis.plate, analysis.plates) == (1, 1)
    assert [part.colour for part in analysis.parts] == ["#FF0000", "#0000FF"]
    assert (await library_geometry(bambuddy, cache, 7, 1)) == analysis
    assert download.call_count == 1
    with pytest.raises(NoSuchPlateError):
        await library_geometry(bambuddy, cache, 7, 2)


@respx.mock
async def test_an_stl_is_viewed_as_the_print_path_wraps_it(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    buffer = io.BytesIO()
    _box(30, 20, 10).export(buffer, file_type="stl")
    library_file(8, buffer.getvalue(), file_type="stl")
    analysis = await library_geometry(bambuddy, tmp_path, 8, 1)
    assert [part.colour for part in analysis.parts] == ["#FFFFFF"]


@respx.mock
@pytest.mark.parametrize(
    ("file_type", "content", "reason"),
    [
        ("gcode.3mf", b"sliced", "sliced"),
        ("step", b"step", "3MF and STL"),
        ("3mf", b"not a zip", "not a 3MF"),
        ("stl", b"solid nothing\nendsolid\n", "no mesh"),
    ],
)
async def test_a_file_that_cannot_be_read_is_not_viewable_saying_why(
    bambuddy: BambuddyClient, tmp_path: Path, file_type: str, content: bytes, reason: str
) -> None:
    library_file(9, content, file_type=file_type)
    with pytest.raises(NotViewableError, match=reason):
        await library_preview(bambuddy, tmp_path, 9, 1)


@respx.mock
async def test_the_cache_keeps_only_the_newest_views(
    bambuddy: BambuddyClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(library_view, "MAX_CACHED", 2)
    cache = tmp_path / "cache"
    for file_id, size in ((1, 10), (2, 11), (3, 12)):
        out = tmp_path / f"{file_id}.3mf"
        write_bambu_3mf(
            [ColourPart(1, "Color 1", "#FF0000", _box(size, 10, 4))], out, thumbnails=None
        )
        library_file(file_id, out.read_bytes())
        await library_preview(bambuddy, cache, file_id, 1)
    assert len(list((cache / library_view.CACHE_DIRNAME).iterdir())) == 2


def _view(tmp_path: Path, file_id: int, size: float) -> bytes:
    out = tmp_path / f"{file_id}.3mf"
    write_bambu_3mf([ColourPart(1, "Color 1", "#FF0000", _box(size, 10, 4))], out, thumbnails=None)
    return out.read_bytes()


@respx.mock
async def test_the_cache_keeps_the_most_recently_used_views(
    bambuddy: BambuddyClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2160: a hit counts as use, so the view used most recently outlives a newer one."""
    monkeypatch.setattr(library_view, "MAX_CACHED", 2)
    cache = tmp_path / "cache"
    views = cache / library_view.CACHE_DIRNAME
    downloads = {
        file_id: library_file(file_id, _view(tmp_path, file_id, 9 + file_id))
        for file_id in (1, 2, 3)
    }
    await library_preview(bambuddy, cache, 1, 1)
    await library_geometry(bambuddy, cache, 2, 1)
    # Both written long ago, file 1's first.
    for entry in views.iterdir():
        age = 1000 if "geometry" in entry.name else 2000
        os.utime(entry, ns=(time.time_ns() - age * 10**9,) * 2)

    await library_preview(bambuddy, cache, 1, 1)  # a hit on the older view
    await library_preview(bambuddy, cache, 3, 1)  # a third view prunes one

    assert downloads[1].call_count == 1
    names = sorted(entry.name for entry in views.iterdir())
    assert len(names) == 2
    assert not any("geometry" in name for name in names)  # file 2's, unused, went


def test_a_prune_tolerates_files_another_prune_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2160: two prunes at once each list a file the other removes, before its stat or
    before its unlink; neither fails."""
    views = tmp_path / library_view.CACHE_DIRNAME
    views.mkdir()
    for index, name in enumerate(("old", "older", "oldest")):
        (views / name).write_bytes(b"x")
        os.utime(views / name, ns=(time.time_ns() - (index + 1) * 10**9,) * 2)
    real_iterdir, real_stat = Path.iterdir, Path.stat

    def iterdir(self: Path) -> Iterator[Path]:
        yield from real_iterdir(self)
        yield self / "gone-before-stat"

    def stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = real_stat(self, follow_symlinks=follow_symlinks)
        if self.name == "oldest":
            os.unlink(self)  # gone after its stat, before its unlink
        return result

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "stat", stat)
    library_view._store(views / "new", b"y", 2)
    monkeypatch.undo()
    assert sorted(entry.name for entry in views.iterdir()) == ["new", "old"]


def _out_of_memory(*_: object, **__: object) -> object:
    raise MemoryError


@pytest.mark.parametrize("step", ["read_plate_parts", "write_glb"])
def test_a_plate_glb_out_of_memory_is_a_read_out_of_memory_error(
    monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """#2767: plate_glb maps a MemoryError itself, so no route has to (an output's plate,
    a job's): reading the plate, or writing its GLB."""
    monkeypatch.setattr(library_view, "read_plate_parts", lambda *_: PlateRead([], 1))
    monkeypatch.setattr(library_view, step, _out_of_memory)
    with pytest.raises(ReadOutOfMemoryError):
        library_view.plate_glb(b"", 1)


@respx.mock
@pytest.mark.parametrize(
    ("step", "read"),
    [
        # The 3MF's read, then the two steps that run on what it read (#2501).
        ("read_plate_parts", library_preview),
        ("write_glb", library_preview),
        ("analyze_geometry", library_geometry),
    ],
)
async def test_running_out_of_memory_is_refused_as_such_and_never_cached(
    bambuddy: BambuddyClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    read: Callable[..., Awaitable[object]],
) -> None:
    """Wherever the view runs out of memory it is the same refusal, saying how to bound
    it, and nothing is cached: the next read, with memory to spare, answers."""
    download = library_file(7, two_colour_3mf(tmp_path))
    cache = tmp_path / "cache"
    monkeypatch.setattr(library_view, step, _out_of_memory)
    with pytest.raises(NotViewableError, match="ran out of memory") as refused:
        await read(bambuddy, cache, 7, 1)
    assert "SCADBUDDY_READ_MAX_TRIANGLES" in str(refused.value)
    assert not [
        path
        for path in cache.rglob("*")
        if path.is_file() and library_view.FILAMENTS_DIRNAME not in path.parts
    ]
    monkeypatch.undo()
    assert await read(bambuddy, cache, 7, 1)
    assert download.call_count == 2


# --- #2788: the download itself, shared by every read and every process ---------------


@respx.mock
async def test_every_read_of_one_files_bytes_downloads_it_once(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """Its preview, its analysis, a second plate and its filament slots, each in a
    process of its own (another ``cache``), share one download of the same bytes."""
    download = library_file(7, two_colour_3mf(tmp_path))
    downloads = DownloadCache(tmp_path / "shared", max_bytes=1 << 30)
    await library_preview(bambuddy, tmp_path / "api", 7, 1, downloads=downloads)
    await library_geometry(bambuddy, tmp_path / "other", 7, 1, downloads=downloads)
    with pytest.raises(NoSuchPlateError):
        await library_preview(bambuddy, tmp_path / "third", 7, 2, downloads=downloads)
    file = await bambuddy.library_file(7)
    assert await library_filaments(bambuddy, tmp_path / "fourth", file, downloads=downloads)
    assert download.call_count == 1


@respx.mock
async def test_slots_are_read_from_a_callers_fetch_and_cached(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#4495, #4553 review: on a miss the slots are read from the bytes the caller
    fetched, with no download of their own, and cached for the next read."""
    content = two_colour_3mf(tmp_path)
    download = library_file(7, content)
    file = await bambuddy.library_file(7)

    async def fetched() -> bytes:
        return content

    cache = tmp_path / "cache"
    assert await library_filaments(bambuddy, cache, file, fetched=fetched)
    assert not download.called

    async def unread() -> bytes:
        raise AssertionError("the cached slots are read before any fetch")

    assert await library_filaments(bambuddy, cache, file, fetched=unread)
    assert not download.called


@respx.mock
async def test_a_callers_fetch_is_kept_for_the_next_read_of_its_bytes(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#4562 review: the bytes a caller fetched are kept in the shared download cache, as
    the spool keeps its own download (#2788), so the file's preview downloads nothing."""
    content = two_colour_3mf(tmp_path)
    download = library_file(7, content)
    downloads = DownloadCache(tmp_path / "shared", max_bytes=1 << 30)
    file = await bambuddy.library_file(7)

    async def fetched() -> bytes:
        return content

    assert await library_filaments(
        bambuddy, tmp_path / "api", file, downloads=downloads, fetched=fetched
    )
    await library_preview(bambuddy, tmp_path / "other", 7, 1, downloads=downloads)
    assert not download.called


@respx.mock
async def test_a_fetch_that_holds_nothing_reads_the_slots_from_disk(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#4562 review: a fetch that holds no bytes (one past MAX_DOWNLOAD_BYTES) leaves
    the slots to the spooled download."""
    download = library_file(7, two_colour_3mf(tmp_path))
    file = await bambuddy.library_file(7)

    async def nothing() -> None:
        return None

    assert await library_filaments(bambuddy, tmp_path / "cache", file, fetched=nothing)
    assert download.call_count == 1


@respx.mock
async def test_a_fetch_out_of_memory_reads_the_slots_from_disk(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """#4553 review: as the spool's own, a fetch that runs out of memory is not the
    read's failure; the file is downloaded to disk and read there."""
    download = library_file(7, two_colour_3mf(tmp_path))
    file = await bambuddy.library_file(7)

    async def out_of_memory() -> bytes:
        raise MemoryError

    assert await library_filaments(bambuddy, tmp_path / "cache", file, fetched=out_of_memory)
    assert download.call_count == 1


@respx.mock
async def test_bytes_that_are_not_the_stated_hash_are_read_but_never_kept(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """A file changed after Bambuddy stated its hash: this read is of what came, and
    nothing is kept under a hash it does not have."""
    content = two_colour_3mf(tmp_path)
    respx.get(f"{API}/library/files/7").mock(
        return_value=httpx.Response(
            200,
            json={"id": 7, "filename": "a.3mf", "file_type": "3mf", "file_hash": "0" * 64},
        )
    )
    download = respx.get(f"{API}/library/files/7/download").mock(
        return_value=httpx.Response(200, content=content)
    )
    downloads = DownloadCache(tmp_path / "shared", max_bytes=1 << 30)
    await library_geometry(bambuddy, tmp_path / "a", 7, 1, downloads=downloads)
    await library_geometry(bambuddy, tmp_path / "b", 7, 1, downloads=downloads)
    assert download.call_count == 2
    assert list((tmp_path / "shared").iterdir()) == []


@respx.mock
async def test_an_stl_is_not_kept_as_a_download(bambuddy: BambuddyClient, tmp_path: Path) -> None:
    """What is read of an STL is the 3MF it is wrapped in, not the file's own bytes."""
    buffer = io.BytesIO()
    _box(30, 20, 10).export(buffer, file_type="stl")
    library_file(8, buffer.getvalue(), file_type="stl")
    downloads = DownloadCache(tmp_path / "shared", max_bytes=1 << 30)
    await library_geometry(bambuddy, tmp_path / "a", 8, 1, downloads=downloads)
    assert not (tmp_path / "shared").exists()


@respx.mock
async def test_bytes_planted_under_a_files_hash_are_never_read(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """Whatever can write the shared directory, a read uses only bytes that are the
    stated hash: a planted entry is dropped and the file downloaded (#2788)."""
    content = two_colour_3mf(tmp_path)
    download = library_file(7, content)
    downloads = DownloadCache(tmp_path / "shared", max_bytes=1 << 30)
    downloads.root.mkdir()
    sha = hashlib.sha256(content).hexdigest()
    downloads.path(sha).write_bytes(content[:-10])
    analysis = await library_geometry(bambuddy, tmp_path / "a", 7, 1, downloads=downloads)
    assert [part.colour for part in analysis.parts] == ["#FF0000", "#0000FF"]
    assert download.call_count == 1
    assert downloads.path(sha).read_bytes() == content


@respx.mock
async def test_a_download_cache_that_cannot_be_used_never_fails_the_read(
    bambuddy: BambuddyClient, tmp_path: Path
) -> None:
    """Keeping a copy is best effort: a cache directory that cannot be written (a full
    or unwritable volume) is a read without one, never a failed read (#2788 review)."""
    content = two_colour_3mf(tmp_path)
    download = library_file(7, content)
    (tmp_path / "shared").write_bytes(b"not a directory")
    downloads = DownloadCache(tmp_path / "shared", max_bytes=1 << 30)
    analysis = await library_geometry(bambuddy, tmp_path / "a", 7, 1, downloads=downloads)
    assert [part.colour for part in analysis.parts] == ["#FF0000", "#0000FF"]
    assert download.call_count == 1
