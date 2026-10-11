"""#1973 — a library file's objects for Arrange are read from its 3MF once per file
hash: the object list (or why it cannot be arranged) is kept under the hash Bambuddy
states, and the pieces are published as they are read, so a second dialog or the
arrange that follows downloads and parses nothing."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import trimesh

from scadbuddy.bambuddy import library_objects
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.library_objects import NotArrangeableError, read_library_objects
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.jobs import LAYOUT_NAME
from scadbuddy.render.objects3mf import read_objects
from scadbuddy.render.read_budget import ReadBudget
from scadbuddy.render.split import ColourPart
from scadbuddy.store.local import LocalBlobStore
from tests.bambuddy.conftest import BASE_URL

API = f"{BASE_URL}/api/v1"


def _project(tmp_path: Path, height: float = 9) -> bytes:
    out = tmp_path / f"plain-{height}.3mf"
    write_bambu_3mf(
        [
            ColourPart(1, "Red", "#FF0000", trimesh.creation.box(extents=(10, 10, 4))),
            ColourPart(2, "Blue", "#0000FF", trimesh.creation.box(extents=(6, 6, height))),
        ],
        out,
        thumbnails=None,
        model_name="plain",
    )
    return out.read_bytes()


def library_file(file_id: int, content: bytes, *, hashed: bool = True) -> respx.Route:
    respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": file_id,
                "filename": f"file-{file_id}.3mf",
                "file_type": "3mf",
                "file_size": len(content),
                "file_hash": hashlib.sha256(content).hexdigest() if hashed else None,
            },
        )
    )
    return respx.get(f"{API}/library/files/{file_id}/download").mock(
        return_value=httpx.Response(200, content=content)
    )


@pytest.fixture
def parses(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """One entry per 3MF parse, so a test can say none happened."""
    calls: list[int] = []

    def counted(payload: bytes, budget: ReadBudget | None = None) -> Any:
        calls.append(len(payload))
        return read_objects(payload, budget)

    monkeypatch.setattr(library_objects, "read_objects", counted)
    return calls


@respx.mock
async def test_a_file_read_once_is_not_downloaded_or_parsed_again(
    bambuddy: BambuddyClient, tmp_path: Path, parses: list[int]
) -> None:
    download = library_file(88, _project(tmp_path))
    blobs, cache = LocalBlobStore(tmp_path / "blobs"), tmp_path / "cache"

    first = await read_library_objects(bambuddy, 88, blobs=blobs, cache=cache)
    second = await read_library_objects(bambuddy, 88, blobs=blobs, cache=cache)

    assert download.call_count == 1 and len(parses) == 1
    assert second.objects == first.objects
    [obj] = second.objects
    assert obj.part.startswith("lib2-") and obj.library_file_id == 88
    assert obj.colours == ["#FF0000", "#0000FF"]
    # The pieces are stored as the file is read: nothing is left for the arrange to write.
    assert (blobs.dir_for(obj.part) / LAYOUT_NAME).is_file()


@respx.mock
async def test_a_changed_file_is_read_again(
    bambuddy: BambuddyClient, tmp_path: Path, parses: list[int]
) -> None:
    blobs, cache = LocalBlobStore(tmp_path / "blobs"), tmp_path / "cache"
    library_file(88, _project(tmp_path, 9))
    before = await read_library_objects(bambuddy, 88, blobs=blobs, cache=cache)
    respx.clear()
    library_file(88, _project(tmp_path, 12))
    after = await read_library_objects(bambuddy, 88, blobs=blobs, cache=cache)
    assert len(parses) == 2
    assert [round(o.bbox.size[2]) for o in after.objects] == [12]
    assert after.objects[0].part != before.objects[0].part


@respx.mock
async def test_a_file_bambuddy_names_no_hash_for_is_read_each_time(
    bambuddy: BambuddyClient, tmp_path: Path, parses: list[int]
) -> None:
    download = library_file(88, _project(tmp_path), hashed=False)
    blobs, cache = LocalBlobStore(tmp_path / "blobs"), tmp_path / "cache"
    await read_library_objects(bambuddy, 88, blobs=blobs, cache=cache)
    await read_library_objects(bambuddy, 88, blobs=blobs, cache=cache)
    assert download.call_count == 2 and len(parses) == 2
    assert not cache.exists() or not any(cache.rglob("*.json"))


@respx.mock
async def test_a_refusal_is_kept_with_the_file(
    bambuddy: BambuddyClient, tmp_path: Path, parses: list[int]
) -> None:
    download = library_file(89, b"not a zip")
    blobs, cache = LocalBlobStore(tmp_path / "blobs"), tmp_path / "cache"
    for _ in range(2):
        with pytest.raises(NotArrangeableError) as refused:
            await read_library_objects(bambuddy, 89, blobs=blobs, cache=cache)
        assert refused.value.reason == "the file is not a 3MF archive"
        assert str(refused.value) == "file-89.3mf: the file is not a 3MF archive"
    assert download.call_count == 1


@respx.mock
async def test_a_refusal_under_a_smaller_budget_is_read_again_under_a_larger_one(
    bambuddy: BambuddyClient, tmp_path: Path, parses: list[int]
) -> None:
    # #2087: the project is 24 triangles. Refused within 10, kept; a request allowed
    # more reads it again rather than taking the smaller budget's word for it.
    download = library_file(88, _project(tmp_path))
    blobs, cache = LocalBlobStore(tmp_path / "blobs"), tmp_path / "cache"
    small = ReadBudget(max_triangles=10)
    with pytest.raises(NotArrangeableError) as refused:
        await read_library_objects(bambuddy, 88, blobs=blobs, cache=cache, budget=small)
    assert "max_triangles read budget" in refused.value.reason
    found = await read_library_objects(bambuddy, 88, blobs=blobs, cache=cache)
    assert [obj.library_file_id for obj in found.objects] == [88]
    assert download.call_count == 2 and len(parses) == 2


@respx.mock
async def test_a_refusal_answers_a_budget_it_covers_without_a_read(
    bambuddy: BambuddyClient, tmp_path: Path, parses: list[int]
) -> None:
    download = library_file(88, _project(tmp_path))
    blobs, cache = LocalBlobStore(tmp_path / "blobs"), tmp_path / "cache"
    for budget in (ReadBudget(max_triangles=20), ReadBudget(max_triangles=10)):
        with pytest.raises(NotArrangeableError, match="max_triangles read budget"):
            await read_library_objects(bambuddy, 88, blobs=blobs, cache=cache, budget=budget)
    assert download.call_count == 1 and len(parses) == 1


@respx.mock
async def test_a_kept_list_answers_any_budget(
    bambuddy: BambuddyClient, tmp_path: Path, parses: list[int]
) -> None:
    # Keeping it cost the read already: a smaller budget is about work not yet done.
    download = library_file(88, _project(tmp_path))
    blobs, cache = LocalBlobStore(tmp_path / "blobs"), tmp_path / "cache"
    first = await read_library_objects(bambuddy, 88, blobs=blobs, cache=cache)
    again = await read_library_objects(
        bambuddy, 88, blobs=blobs, cache=cache, budget=ReadBudget(max_triangles=1)
    )
    assert again.objects == first.objects
    assert download.call_count == 1 and len(parses) == 1


@respx.mock
async def test_a_file_whose_pieces_were_swept_is_read_again(
    bambuddy: BambuddyClient, tmp_path: Path, parses: list[int]
) -> None:
    library_file(88, _project(tmp_path))
    blobs, cache = LocalBlobStore(tmp_path / "blobs"), tmp_path / "cache"
    [obj] = (await read_library_objects(bambuddy, 88, blobs=blobs, cache=cache)).objects
    blobs.remove(obj.part)
    [again] = (await read_library_objects(bambuddy, 88, blobs=blobs, cache=cache)).objects
    assert len(parses) == 2 and again.part == obj.part
    assert (blobs.dir_for(obj.part) / LAYOUT_NAME).is_file()


@respx.mock
async def test_the_same_bytes_under_another_file_are_that_files_objects(
    bambuddy: BambuddyClient, tmp_path: Path, parses: list[int]
) -> None:
    content = _project(tmp_path)
    library_file(88, content)
    library_file(90, content)
    blobs, cache = LocalBlobStore(tmp_path / "blobs"), tmp_path / "cache"
    await read_library_objects(bambuddy, 88, blobs=blobs, cache=cache)
    copy = await read_library_objects(bambuddy, 90, blobs=blobs, cache=cache)
    assert len(parses) == 1
    assert copy.filename == "file-90.3mf"
    assert [o.library_file_id for o in copy.objects] == [90]
