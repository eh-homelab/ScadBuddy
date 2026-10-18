from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import trimesh

from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.store import NativeFile, NativeLibraryStore
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import render_plate_thumbnails
from scadbuddy.store.content import ContentStore
from scadbuddy.store.index import Pool
from tests.support.store import content, pool, s3  # noqa: F401


def _moved(x: float, y: float, z: float) -> np.ndarray:
    matrix = np.eye(4)
    matrix[:3, 3] = (x, y, z)
    return matrix


@pytest.fixture
def three_mf(tmp_path: Path) -> Path:
    parts = [
        ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box(extents=(10, 10, 4))),
        ColourPart(
            2,
            "Color 2",
            "#1F6FEB",
            trimesh.creation.box(extents=(2, 2, 2), transform=_moved(0, 0, 3)),
        ),
    ]
    out = tmp_path / "bracket.3mf"
    write_bambu_3mf(parts, out, thumbnails=render_plate_thumbnails(parts), model_name="bracket")
    return out


@pytest.fixture
def native(pool: Pool, content: ContentStore, tmp_path: Path) -> NativeLibrary:  # noqa: F811
    spool = tmp_path / "spool"
    spool.mkdir()
    return NativeLibrary(NativeLibraryStore(pool), content, spool)


async def upload(native: NativeLibrary, path: Path, folder_id: int | None = None) -> NativeFile:
    with path.open("rb") as data:
        return await native.upload(path.name, data, folder_id)
