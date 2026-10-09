"""#1753 — a library file's preview and mesh analysis, read from the 3MF the print
path fetches, as an output's are read from its own."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import httpx
import pytest
import respx
import trimesh

from scadbuddy.bambuddy import library_view
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.library_view import (
    NotViewableError,
    library_geometry,
    library_preview,
)
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.geometry import NoSuchPlateError
from scadbuddy.render.glb import read_glb
from scadbuddy.render.split import ColourPart
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
    assert not cache.exists() or not any(cache.iterdir())


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
