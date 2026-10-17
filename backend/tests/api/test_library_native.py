"""The `/print/library` reads on `library_backend=native` (plan
2026-10-17-native-print-library, Task 4): the same shapes as on Bambuddy, answered from
ScadBuddy's own library over the S3 content store."""

from __future__ import annotations

from collections.abc import Iterator
from functools import partial
from pathlib import Path

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.print_library.component import PRINT_LIBRARY
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.store import NativeFile
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import render_plate_thumbnails

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def native_client(settings: Settings, s3: S3Settings) -> Iterator[TestClient]:
    native = settings.model_copy(
        update={
            "store_backend": "s3",
            "store_s3_endpoint": s3.endpoint,
            "store_s3_bucket": s3.bucket,
            "store_s3_prefix": s3.prefix,
            "store_s3_access_key_id": s3.access_key_id,
            "store_s3_secret_access_key": s3.secret_access_key,
            "library_backend": "native",
        }
    )
    with TestClient(create_app(native)) as client:
        yield client


def _native(client: TestClient) -> NativeLibrary:
    library = getattr(client.app.state, STATE_ATTR).components.get(PRINT_LIBRARY)  # type: ignore[attr-defined]
    assert library.backend == "native"
    native: NativeLibrary = library.native
    return native


def _three_mf(path: Path) -> Path:
    shifted = np.eye(4)
    shifted[:3, 3] = (0, 0, 3)
    parts = [
        ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box(extents=(10, 10, 4))),
        ColourPart(
            2, "Color 2", "#1F6FEB", trimesh.creation.box(extents=(2, 2, 2), transform=shifted)
        ),
    ]
    write_bambu_3mf(parts, path, thumbnails=render_plate_thumbnails(parts), model_name="bracket")
    return path


def _upload(
    client: TestClient,
    path: Path,
    folder_id: int | None = None,
    output_id: str | None = None,
) -> NativeFile:
    native = _native(client)
    assert client.portal is not None
    with path.open("rb") as data:
        uploaded: NativeFile = client.portal.call(
            partial(native.upload, path.name, data, folder_id, output_id=output_id)
        )
    return uploaded


@pytest.fixture
def uploaded(native_client: TestClient, tmp_path: Path) -> NativeFile:
    return _upload(native_client, _three_mf(tmp_path / "bracket.3mf"))


@pytest.fixture
def in_folder(native_client: TestClient, tmp_path: Path) -> NativeFile:
    folder = _native(native_client).rows.create_folder("Dinos", None)
    return _upload(native_client, _three_mf(tmp_path / "bracket.3mf"), folder.id)


@pytest.fixture
def uploaded_with_output(
    native_client: TestClient, model: str, paths: DataPaths, tmp_path: Path
) -> NativeFile:
    """A file whose row names a saved output. On the S3 store a render needs the
    template's git snapshot, so the output is its directory and `meta.json` alone,
    which is all `OutputStore.directory` finds it by."""
    output_id = "0123456789abcdef"
    directory = paths.outputs / model / output_id
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text("{}", encoding="utf-8")
    return _upload(native_client, _three_mf(tmp_path / "bracket.3mf"), output_id=output_id)


def test_listing_on_native(native_client: TestClient, uploaded: NativeFile) -> None:
    body = native_client.get("/api/v1/print/library", params={"all": True}).json()
    assert body["files"][0] | {"created_at": None} == {
        "id": uploaded.id,
        "filename": "bracket.3mf",
        "file_type": "3mf",
        "folder_id": None,
        "has_thumbnail": True,
        "print_count": 0,
        "printable": True,
        "sliced": False,
        "file_size": uploaded.size,
        "created_at": None,
        "output_id": None,
        "is_external": False,
    }


def test_listing_names_native_folders(native_client: TestClient, in_folder: NativeFile) -> None:
    body = native_client.get("/api/v1/print/library").json()
    assert body["files"] == []
    assert body["folders"] == [
        {
            "id": in_folder.folder_id,
            "name": "Dinos",
            "parent_id": None,
            "depth": 0,
            "file_count": 1,
            "project_id": None,
            "project_name": None,
        }
    ]


def test_file_id_lists_its_folder(native_client: TestClient, in_folder: NativeFile) -> None:
    assert (
        native_client.get("/api/v1/print/library", params={"file_id": in_folder.id}).json()[
            "folder_id"
        ]
        == in_folder.folder_id
    )


def test_a_bambuddy_range_id_is_404_on_native(native_client: TestClient) -> None:
    assert native_client.get("/api/v1/print/library/42/file").status_code == 404
    assert native_client.get("/api/v1/print/library/42/plates").status_code == 404
    assert native_client.get("/api/v1/print/library/42/thumbnail").status_code == 404
    assert native_client.get("/api/v1/print/library", params={"file_id": 42}).status_code == 404


def test_download_is_whole_with_its_hash(native_client: TestClient, uploaded: NativeFile) -> None:
    r = native_client.get(
        f"/api/v1/print/library/{uploaded.id}/file", headers={"range": "bytes=0-1"}
    )
    assert r.status_code == 200 and r.headers["etag"] == f'"{uploaded.sha256}"'
    assert r.headers["content-disposition"] == "attachment" and len(r.content) == uploaded.size
    assert r.headers["accept-ranges"] == "none"
    assert r.headers["content-length"] == str(uploaded.size)
    assert r.headers["content-type"] == "application/octet-stream"


def test_names_of_unknown_ids_are_null_not_502(native_client: TestClient) -> None:
    r = native_client.get("/api/v1/print/library/names", params=[("ids", 41), ("ids", 42)])
    assert r.status_code == 200 and [n["name"] for n in r.json()] == [None, None]


def test_names_name_a_native_file(native_client: TestClient, in_folder: NativeFile) -> None:
    r = native_client.get(
        "/api/v1/print/library/names", params=[("ids", in_folder.id), ("ids", 42)]
    )
    assert r.json() == [
        {"id": in_folder.id, "name": "bracket.3mf", "folder_id": in_folder.folder_id},
        {"id": 42, "name": None, "folder_id": None},
    ]


def test_search_finds_by_folder_path(native_client: TestClient, in_folder: NativeFile) -> None:
    hits = native_client.get("/api/v1/print/library/search", params={"q": "dino"}).json()
    assert hits["files"][0]["folder_path"] == ["Dinos"]
    assert hits["files"][0]["id"] == in_folder.id and hits["total"] == 1


def test_plates_on_native(native_client: TestClient, uploaded: NativeFile) -> None:
    plates = native_client.get(f"/api/v1/print/library/{uploaded.id}/plates").json()
    assert plates == [{"index": 1, "has_thumbnail": True, "name": "bracket"}]


def test_plate_thumbnail_is_the_3mf_cover(native_client: TestClient, uploaded: NativeFile) -> None:
    r = native_client.get(f"/api/v1/print/library/{uploaded.id}/plates/1/thumbnail")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")


def test_thumbnail_on_native(native_client: TestClient, uploaded: NativeFile) -> None:
    r = native_client.get(f"/api/v1/print/library/{uploaded.id}/thumbnail")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_a_missing_thumbnail_is_404(
    native_client: TestClient, uploaded: NativeFile, tmp_path: Path
) -> None:
    assert (
        native_client.get(f"/api/v1/print/library/{uploaded.id}/plates/9/thumbnail").status_code
        == 404
    )
    stl = tmp_path / "part.stl"
    stl.write_bytes(b"solid x\nendsolid x\n")
    plain = _upload(native_client, stl)
    assert native_client.get(f"/api/v1/print/library/{plain.id}/thumbnail").status_code == 404


def test_an_entry_names_its_live_output(
    native_client: TestClient, uploaded_with_output: NativeFile
) -> None:
    # The row is inserted with output_id of a saved output (Task 8 adds the filing that
    # does it for real).
    assert (
        native_client.get("/api/v1/print/library").json()["files"][0]["output_id"]
        == uploaded_with_output.output_id
    )
    hits = native_client.get("/api/v1/print/library/search", params={"q": "bracket"}).json()
    assert hits["files"][0]["output_id"] == uploaded_with_output.output_id
