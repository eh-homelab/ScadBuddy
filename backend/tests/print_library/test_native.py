"""`NativeLibrary` over the rows and the content store, and `PRINT_LIBRARY` (plan
2026-10-17-native-print-library, Task 3)."""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import psycopg
import pytest
import trimesh

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.core.components import Components, Core
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.print_library.component import COMPONENT, PrintLibrary
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.reads import LibraryListingSource, LibraryReads
from scadbuddy.print_library.store import NativeFile, NativeLibraryStore
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import render_plate_thumbnails
from scadbuddy.store.content import ContentStore
from scadbuddy.store.index import Pool
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS

pytestmark = pytest.mark.requires_postgres

# mypy checks these: Bambuddy's client is both read surfaces, as the native library is.
_reads: type[LibraryReads] = BambuddyClient
_listing: type[LibraryListingSource] = BambuddyClient
_native_reads: type[LibraryReads] = NativeLibrary
_native_listing: type[LibraryListingSource] = NativeLibrary


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
def native(pool: Pool, content: ContentStore, tmp_path: Path) -> NativeLibrary:
    spool = tmp_path / "spool"
    spool.mkdir()
    return NativeLibrary(NativeLibraryStore(pool), content, spool)


class UploadSpy:
    def __init__(self) -> None:
        self.uploads = 0


@pytest.fixture
def spy_backend(content: ContentStore, monkeypatch: pytest.MonkeyPatch) -> UploadSpy:
    spy = UploadSpy()
    upload = content.backend.upload

    async def counted(*args: Any, **kwargs: Any) -> str:
        spy.uploads += 1
        return await upload(*args, **kwargs)

    monkeypatch.setattr(content.backend, "upload", counted)
    return spy


async def upload(native: NativeLibrary, path: Path, folder_id: int | None = None) -> NativeFile:
    with path.open("rb") as data:
        return await native.upload(path.name, data, folder_id)


async def downloaded(native: NativeLibrary, file_id: int) -> bytes:
    return b"".join([c async for c in native.download_library_file(file_id)])


def utcnow() -> datetime:
    return datetime.now(UTC)


async def test_upload_lists_downloads_and_reads_plates(
    native: NativeLibrary, three_mf: Path
) -> None:
    with three_mf.open("rb") as data:
        f = await native.upload("bracket.3mf", data, None)
    rows = await native.library_listing(folder_id=None)
    assert [(r.id, r.filename, r.file_type) for r in rows] == [(f.id, "bracket.3mf", "3mf")]
    assert await downloaded(native, f.id) == three_mf.read_bytes()
    plates = await native.library_plates(f.id)
    assert plates.file_id == f.id and plates.plates[0].index == 1
    assert plates.plates[0].has_thumbnail and f.has_thumbnail


async def test_a_file_that_is_not_a_3mf_has_no_plates(
    native: NativeLibrary, tmp_path: Path
) -> None:
    stl = tmp_path / "part.stl"
    stl.write_bytes(b"solid x\nendsolid x\n")
    f = await upload(native, stl)
    assert f.plates == [] and not f.has_thumbnail and f.file_type == "stl"
    assert await downloaded(native, f.id) == stl.read_bytes()


async def test_a_broken_3mf_uploads_with_no_plates(native: NativeLibrary, tmp_path: Path) -> None:
    bad = tmp_path / "bad.3mf"
    bad.write_bytes(b"not a zip")
    f = await upload(native, bad)
    assert f.plates == [] and await downloaded(native, f.id) == b"not a zip"


async def test_an_upload_into_a_missing_folder_is_404(
    native: NativeLibrary, three_mf: Path, spy_backend: UploadSpy
) -> None:
    with pytest.raises(ApiError) as e:
        await upload(native, three_mf, folder_id=1_999_999_999)
    assert e.value.status == 404 and spy_backend.uploads == 0


async def test_purging_one_of_two_identical_files_keeps_the_other(
    native: NativeLibrary, three_mf: Path
) -> None:
    a = await upload(native, three_mf)
    b = await upload(native, three_mf)
    native.rows.trash([a.id])
    assert await native.purge(a.id, utcnow() + timedelta(seconds=1))
    assert await downloaded(native, b.id) == three_mf.read_bytes()
    assert await native.content.stat(a.blob_key) is None


async def test_a_purge_of_a_restored_file_does_nothing(
    native: NativeLibrary, three_mf: Path
) -> None:
    a = await upload(native, three_mf)
    native.rows.trash([a.id])
    native.rows.restore([a.id])
    assert not await native.purge(a.id, utcnow() + timedelta(seconds=1))
    assert await downloaded(native, a.id) == three_mf.read_bytes()


async def test_copy_moves_no_bytes(
    native: NativeLibrary, three_mf: Path, spy_backend: UploadSpy
) -> None:
    a = await upload(native, three_mf)
    folder = native.rows.create_folder("Copies", None)
    uploads = spy_backend.uploads
    c = await native.copy(a.id, folder.id)
    assert c.id != a.id and spy_backend.uploads == uploads
    assert (c.name, c.folder_id, c.sha256, c.filed) == (a.name, folder.id, a.sha256, False)
    native.rows.trash([a.id])
    await native.purge(a.id, utcnow() + timedelta(seconds=1))
    assert await downloaded(native, c.id) == three_mf.read_bytes()


async def test_a_bambuddy_range_id_is_404(native: NativeLibrary) -> None:
    with pytest.raises(ApiError) as e:
        await native.library_file(42)
    assert e.value.status == 404


async def test_a_trashed_file_is_404(native: NativeLibrary, three_mf: Path) -> None:
    f = await upload(native, three_mf)
    native.rows.trash([f.id])
    with pytest.raises(ApiError) as e:
        await native.library_file(f.id)
    assert e.value.status == 404
    with pytest.raises(ApiError):
        await downloaded(native, f.id)


async def test_folders_nest_like_bambuddys(native: NativeLibrary, three_mf: Path) -> None:
    top = native.rows.create_folder("A", None)
    child = native.rows.create_folder("B", top.id)
    await upload(native, three_mf, folder_id=child.id)
    [a] = await native.folders()
    assert a.name == "A" and [c.name for c in a.children] == ["B"] and a.project_id is None
    assert a.file_count == 0 and a.children[0].file_count == 1
    assert a.children[0].parent_id == top.id


async def test_listing_one_folder_and_every_folder(native: NativeLibrary, three_mf: Path) -> None:
    folder = native.rows.create_folder("A", None)
    top = await upload(native, three_mf)
    inner = await upload(native, three_mf, folder_id=folder.id)
    assert [r.id for r in await native.library_listing(folder_id=folder.id)] == [inner.id]
    every = await native.library_listing(folder_id=None, every_folder=True)
    assert [r.id for r in every] == [top.id, inner.id]
    assert all(r.print_count == 0 and not r.is_external for r in every)


async def test_a_file_reads_as_bambuddys_model(native: NativeLibrary, three_mf: Path) -> None:
    f = await upload(native, three_mf)
    got = await native.library_file(f.id)
    assert (got.file_hash, got.file_size, got.file_type, got.folder_id) == (
        f.sha256,
        f.size,
        "3mf",
        None,
    )
    assert not got.is_external and got.thumbnail_path is not None
    [row] = await native.library_listing(folder_id=None)
    assert row.thumbnail_path is not None


async def test_filament_requirements_are_empty(native: NativeLibrary, three_mf: Path) -> None:
    f = await upload(native, three_mf)
    got = await native.filament_requirements(f.id, plate_id=1)
    assert got.filaments == []


async def test_thumbnails_come_from_the_file(native: NativeLibrary, three_mf: Path) -> None:
    f = await upload(native, three_mf)
    with zipfile.ZipFile(three_mf) as archive:
        plate_1 = archive.read("Metadata/plate_1.png")
    assert await native.thumbnail(f.id, 1) == plate_1
    assert await native.thumbnail(f.id, None) is not None
    assert await native.thumbnail(f.id, 7) is None


async def test_names_answer_none_for_unknown_ids(native: NativeLibrary, three_mf: Path) -> None:
    f = await upload(native, three_mf)
    got = await native.names([f.id, 42, f.id + 1000])
    assert [(i, n.name if n else None) for i, n in got] == [
        (f.id, "bracket.3mf"),
        (42, None),
        (f.id + 1000, None),
    ]


class FakeSettingsStore:
    def __init__(self, backend: str) -> None:
        self.backend = backend
        self.loads = 0

    def load(self) -> Any:
        self.loads += 1
        return SimpleNamespace(library_backend=self.backend)


def _core(settings_store: FakeSettingsStore, database_url: str, **more: Any) -> Core:
    settings = Settings(database_url=database_url, temporal_address=UNUSED_TEMPORAL_ADDRESS)
    return cast(Core, SimpleNamespace(settings=settings, settings_store=settings_store, **more))


@pytest.fixture
def core_without_pools() -> Iterator[tuple[Core, FakeSettingsStore]]:
    """No settings pool open and no `store` attribute, as at `build_all`."""
    store = FakeSettingsStore("bambuddy")
    yield _core(store, UNUSED_DATABASE_URL), store


def test_building_the_component_reads_nothing(
    core_without_pools: tuple[Core, FakeSettingsStore],
) -> None:
    core, settings_store = core_without_pools
    lib = COMPONENT.build(core, Components(core, []))
    assert isinstance(lib, PrintLibrary) and settings_store.loads == 0
    assert lib.backend == "bambuddy" and lib.backend == "bambuddy"
    assert settings_store.loads == 1
    with pytest.raises(RuntimeError):
        _ = lib.native


async def test_the_component_builds_the_native_library_once(
    pg_conninfo: str, content: ContentStore, three_mf: Path
) -> None:
    core = _core(FakeSettingsStore("native"), pg_conninfo, store=SimpleNamespace(content=content))
    lib = COMPONENT.build(core, Components(core, []))
    assert COMPONENT.run is not None
    async with COMPONENT.run(lib):
        assert lib.native is lib.native and lib.native.content is content
        f = await upload(lib.native, three_mf)
        assert (await lib.native.library_file(f.id)).id == f.id


def _rewritten(src: Path, out: Path, name: str, data: bytes) -> Path:
    """``src`` with entry ``name`` replaced by ``data``, deflated (a small bomb)."""
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as b:
        for info in a.infolist():
            if info.filename != name:
                b.writestr(info, a.read(info.filename))
        b.writestr(name, data)
    return out


async def test_a_settings_bomb_is_not_inflated(
    native: NativeLibrary, three_mf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bomb = _rewritten(
        three_mf, tmp_path / "bomb.3mf", "Metadata/model_settings.config", b" " * (32 << 20)
    )
    read = zipfile.ZipFile.read
    monkeypatch.setattr(
        zipfile.ZipFile, "read", lambda *a, **k: pytest.fail("inflated") or read(*a, **k)
    )
    f = await upload(native, bomb)
    assert f.plates == [] and not f.has_thumbnail


async def test_an_oversized_cover_is_no_thumbnail(
    native: NativeLibrary, three_mf: Path, tmp_path: Path
) -> None:
    big = _rewritten(three_mf, tmp_path / "big.3mf", "Metadata/plate_1.png", b"\0" * (32 << 20))
    f = await upload(native, big)
    assert f.has_thumbnail and await native.thumbnail(f.id, 1) is None


async def test_a_3mf_with_only_a_cover_lists_with_a_thumbnail(
    native: NativeLibrary, tmp_path: Path
) -> None:
    cover = b"\x89PNG cover"
    plain = tmp_path / "plain.3mf"
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        z.writestr("Metadata/thumbnail.png", cover)
    f = await upload(native, plain)
    assert f.plates == [] and f.has_thumbnail
    [row] = await native.library_listing(folder_id=None)
    assert row.thumbnail_path is not None
    assert await native.thumbnail(f.id, None) == cover


async def test_a_cover_past_the_cap_is_no_thumbnail(native: NativeLibrary, tmp_path: Path) -> None:
    plain = tmp_path / "big-cover.3mf"
    with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        z.writestr("Metadata/thumbnail.png", b"\0" * (9 << 20))
    f = await upload(native, plain)
    assert not f.has_thumbnail and await native.thumbnail(f.id, None) is None


@pytest.mark.parametrize("copying", [False, True])
async def test_a_failed_insert_leaves_no_library_blob(
    native: NativeLibrary, three_mf: Path, monkeypatch: pytest.MonkeyPatch, copying: bool
) -> None:
    # A `library` blob is never swept, so one the row never named would stay for good.
    source = await upload(native, three_mf) if copying else None
    tried: list[NativeFile] = []

    def broken(row: NativeFile) -> NativeFile:
        tried.append(row)
        raise psycopg.OperationalError("the connection dropped")

    monkeypatch.setattr(native.rows, "insert_file", broken)
    with pytest.raises(psycopg.OperationalError):
        if source is not None:
            await native.copy(source.id, None)
        else:
            await upload(native, three_mf)
    [row] = tried
    assert await native.content.stat(row.blob_key) is None
    if source is not None:
        assert await downloaded(native, source.id) == three_mf.read_bytes()
