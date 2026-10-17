"""The `/print/library` routes on `library_backend=native` (plan
2026-10-17-native-print-library, Tasks 4 and 5): the same shapes as on Bambuddy, answered
from ScadBuddy's own library over the S3 content store. The writes go through the routes
with an `Idempotency-Key`, so the operation kinds really run."""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

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
from scadbuddy.print_library.store import NativeFile, Unset
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import render_plate_thumbnails
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import ReuseLostError
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres


def _native_settings(settings: Settings, s3: S3Settings, **extra: Any) -> Settings:
    return settings.model_copy(
        update={
            "store_backend": "s3",
            "store_s3_endpoint": s3.endpoint,
            "store_s3_bucket": s3.bucket,
            "store_s3_prefix": s3.prefix,
            "store_s3_access_key_id": s3.access_key_id,
            "store_s3_secret_access_key": s3.secret_access_key,
            "library_backend": "native",
            **extra,
        }
    )


@pytest.fixture
def native_client(settings: Settings, s3: S3Settings) -> Iterator[TestClient]:
    with TestClient(create_app(_native_settings(settings, s3))) as client:
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


# writes (Task 5)


@pytest.fixture
def three_mf(tmp_path: Path) -> Path:
    return _three_mf(tmp_path / "bracket.3mf")


def upload_via_route(
    client: TestClient, path: Path, folder_id: int | None = None, key: str | None = None
) -> dict[str, Any]:
    headers = {"Idempotency-Key": key} if key is not None else press()
    with path.open("rb") as data:
        r = client.post(
            "/api/v1/print/library/files",
            files={"file": (path.name, data, "application/octet-stream")},
            data={} if folder_id is None else {"folder_id": str(folder_id)},
            headers=headers,
        )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def mkdir(client: TestClient, name: str, parent_id: int | None = None) -> dict[str, Any]:
    r = client.post(
        "/api/v1/print/library/folders",
        json={"name": name, **({} if parent_id is None else {"parent_id": parent_id})},
        headers=press(),
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def test_upload_rename_move_delete_restore(native_client: TestClient, three_mf: Path) -> None:
    f = upload_via_route(native_client, three_mf, folder_id=None)
    assert f["filename"] == "bracket.3mf" and f["folder_id"] is None
    d = mkdir(native_client, "Dinos")
    assert d["parent_id"] is None
    moved = native_client.patch(
        f"/api/v1/print/library/{f['id']}",
        json={"filename": "rex.3mf", "folder_id": d["id"]},
        headers=press(),
    ).json()
    assert moved == {"id": f["id"], "filename": "rex.3mf", "folder_id": d["id"]}
    gone = native_client.post(
        "/api/v1/print/library/delete", json={"file_ids": [f["id"]]}, headers=press()
    ).json()
    assert gone == {
        "deleted": [{"id": f["id"], "filename": "rex.3mf", "trashed": True}],
        "skipped": [],
    }
    in_d = {"all": True, "folder_id": d["id"]}
    assert native_client.get("/api/v1/print/library", params=in_d).json()["files"] == []
    back = native_client.post(
        "/api/v1/print/library/restore", json={"file_ids": [f["id"]]}, headers=press()
    ).json()
    assert back == {"restored": [f["id"]], "skipped": []}
    [listed] = native_client.get("/api/v1/print/library", params=in_d).json()["files"]
    assert listed["id"] == f["id"] and listed["folder_id"] == d["id"]


def test_a_restore_of_a_file_not_in_the_trash_is_skipped(
    native_client: TestClient, three_mf: Path
) -> None:
    f = upload_via_route(native_client, three_mf)
    back = native_client.post(
        "/api/v1/print/library/restore", json={"file_ids": [f["id"], 42]}, headers=press()
    ).json()
    assert back == {
        "restored": [],
        "skipped": [
            {"id": f["id"], "filename": None, "reason": "it is not in the trash"},
            {"id": 42, "filename": None, "reason": "it is not in the trash"},
        ],
    }


def test_a_delete_of_a_missing_file_is_404(native_client: TestClient) -> None:
    r = native_client.post(
        "/api/v1/print/library/delete", json={"file_ids": [1_999_999_999]}, headers=press()
    )
    assert r.status_code == 404, r.text


def test_a_folder_cannot_move_under_itself(native_client: TestClient) -> None:
    a = mkdir(native_client, "a")
    b = mkdir(native_client, "b", parent_id=a["id"])
    r = native_client.patch(
        f"/api/v1/print/library/folders/{a['id']}", json={"parent_id": b["id"]}, headers=press()
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "a folder cannot move into itself or a folder under it"


def test_a_folder_renames(native_client: TestClient) -> None:
    a = mkdir(native_client, "a")
    r = native_client.patch(
        f"/api/v1/print/library/folders/{a['id']}", json={"name": "z"}, headers=press()
    )
    assert r.json() == {"id": a["id"], "name": "z", "parent_id": None}


def test_patch_folder_id_0_moves_to_the_top(native_client: TestClient, three_mf: Path) -> None:
    d = mkdir(native_client, "d")
    f = upload_via_route(native_client, three_mf, folder_id=d["id"])
    assert f["folder_id"] == d["id"]
    moved = native_client.patch(
        f"/api/v1/print/library/{f['id']}", json={"folder_id": 0}, headers=press()
    ).json()
    assert moved["folder_id"] is None
    sub = mkdir(native_client, "s", parent_id=d["id"])
    assert (
        native_client.patch(
            f"/api/v1/print/library/folders/{sub['id']}", json={"parent_id": 0}, headers=press()
        ).json()["parent_id"]
        is None
    )


def test_a_missing_target_folder_is_404(native_client: TestClient, three_mf: Path) -> None:
    f = upload_via_route(native_client, three_mf)
    a = mkdir(native_client, "a")
    missing = 1_999_999_999
    for method, url, body in [
        ("patch", f"/api/v1/print/library/{f['id']}", {"folder_id": missing}),
        ("patch", f"/api/v1/print/library/folders/{a['id']}", {"parent_id": missing}),
        ("post", "/api/v1/print/library/folders", {"name": "x", "parent_id": missing}),
        ("post", "/api/v1/print/library/move", {"file_ids": [f["id"]], "folder_id": missing}),
        ("post", "/api/v1/print/library/copy", {"file_ids": [f["id"]], "folder_id": missing}),
        ("patch", f"/api/v1/print/library/folders/{missing}", {"name": "y"}),
        ("post", f"/api/v1/print/library/folders/{missing}/delete", None),
        ("patch", f"/api/v1/print/library/{missing}", {"filename": "y.3mf"}),
    ]:
        r = native_client.request(method, url, json=body, headers=press())
        assert r.status_code == 404, (url, body, r.text)
    with three_mf.open("rb") as data:
        r = native_client.post(
            "/api/v1/print/library/files",
            files={"file": ("x.3mf", data, "application/octet-stream")},
            data={"folder_id": str(missing)},
            headers=press(),
        )
    assert r.status_code == 404, r.text


def test_a_missing_parent_raced_away_is_404_not_a_raw_fk_error(
    native_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The existence check passes; the row is gone by the insert: the FK refuses it.
    native = _native(native_client)
    a = mkdir(native_client, "a")
    native.rows.delete_folder(a["id"])
    monkeypatch.setattr(native, "_folder_must_exist", _passes)
    r = native_client.post(
        "/api/v1/print/library/folders", json={"name": "x", "parent_id": a["id"]}, headers=press()
    )
    assert r.status_code == 404, r.text


async def _passes(folder_id: int | None) -> None:
    return None


def test_folder_delete_trashes_its_files_deepest_first(
    native_client: TestClient, three_mf: Path
) -> None:
    a = mkdir(native_client, "a")
    b = mkdir(native_client, "b", parent_id=a["id"])
    c = mkdir(native_client, "c", parent_id=b["id"])
    f = upload_via_route(native_client, three_mf, folder_id=b["id"])
    g = upload_via_route(native_client, three_mf, folder_id=a["id"])
    top = upload_via_route(native_client, three_mf)
    r = native_client.post(f"/api/v1/print/library/folders/{a['id']}/delete", headers=press())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["folders"] == [c["id"], b["id"], a["id"]]
    assert sorted(e["id"] for e in body["deleted"]) == sorted([f["id"], g["id"]])
    assert all(e["trashed"] for e in body["deleted"]) and body["skipped"] == []
    # Nothing in the subtree fell to the top level through the FK's SET NULL: the only
    # live file is the one that was there already.
    listed = native_client.get("/api/v1/print/library", params={"all": True}).json()
    assert [e["id"] for e in listed["files"]] == [top["id"]] and listed["folders"] == []
    rows = _native(native_client).rows
    assert {x.id for x in rows.files(None)} == {top["id"]}
    assert rows.file(f["id"], trashed=True) is not None


def test_copy_is_a_new_id_with_the_same_bytes(native_client: TestClient, three_mf: Path) -> None:
    f = upload_via_route(native_client, three_mf)
    d = mkdir(native_client, "d")
    r = native_client.post(
        "/api/v1/print/library/copy",
        json={"file_ids": [f["id"]], "folder_id": d["id"]},
        headers=press(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    [copied] = body["copied"]
    assert body["folder_id"] == d["id"] and body["skipped"] == []
    assert copied["source_id"] == f["id"] and copied["id"] != f["id"]
    assert copied["filename"] == "bracket.3mf"
    original = native_client.get(f"/api/v1/print/library/{f['id']}/file")
    copy = native_client.get(f"/api/v1/print/library/{copied['id']}/file")
    assert copy.content == original.content and copy.headers["etag"] == original.headers["etag"]


def test_copy_by_folder_path(native_client: TestClient, three_mf: Path) -> None:
    f = upload_via_route(native_client, three_mf)
    a = mkdir(native_client, "a")
    b = mkdir(native_client, "b", parent_id=a["id"])
    r = native_client.post(
        "/api/v1/print/library/copy",
        json={"file_ids": [f["id"]], "folder_path": "a/b"},
        headers=press(),
    )
    assert r.json()["folder_id"] == b["id"], r.text


def test_a_copy_whose_source_was_freed_is_skipped(
    native_client: TestClient, three_mf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = upload_via_route(native_client, three_mf)

    async def lost(self: ContentStore, *args: Any, **kwargs: Any) -> Any:
        raise ReuseLostError("freed")

    monkeypatch.setattr(ContentStore, "link", lost)
    body = native_client.post(
        "/api/v1/print/library/copy", json={"file_ids": [f["id"]]}, headers=press()
    ).json()
    assert body["copied"] == []
    assert body["skipped"] == [
        {
            "id": f["id"],
            "filename": "bracket.3mf",
            "reason": "the file was deleted while it was copied",
        }
    ]


def test_move_clears_filed_and_names_no_project(native_client: TestClient, three_mf: Path) -> None:
    native = _native(native_client)
    f = _upload(native_client, three_mf)
    native.rows.update_file(f.id, name=None, folder_id=Unset, filed=True)
    d = mkdir(native_client, "d")
    r = native_client.post(
        "/api/v1/print/library/move",
        json={"file_ids": [f.id], "folder_id": d["id"]},
        headers=press(),
    )
    assert r.json() == {
        "folder_id": d["id"],
        "moved": [{"id": f.id, "filename": "bracket.3mf", "left_project": None}],
        "skipped": [],
    }
    row = native.rows.file(f.id)
    assert row is not None and row.folder_id == d["id"] and row.filed is False
    # And to the top level, with neither folder_id nor folder_path.
    native_client.post("/api/v1/print/library/move", json={"file_ids": [f.id]}, headers=press())
    row = native.rows.file(f.id)
    assert row is not None and row.folder_id is None


def test_rename_keeps_the_extension(native_client: TestClient, three_mf: Path) -> None:
    f = upload_via_route(native_client, three_mf)
    r = native_client.post(
        "/api/v1/print/library/rename",
        json={"file_id": f["id"], "filename": "rex.stl"},
        headers=press(),
    )
    assert r.status_code == 422, r.text
    r = native_client.post(
        "/api/v1/print/library/rename",
        json={"file_id": f["id"], "filename": "rex.3mf"},
        headers=press(),
    )
    assert r.json() == {"id": f["id"], "filename": "rex.3mf", "previous": "bracket.3mf"}


def test_a_large_upload_streams(native_client: TestClient, tmp_path: Path) -> None:
    big = tmp_path / "big.stl"
    big.write_bytes(b"")
    os.truncate(big, 80 * 1024 * 1024)
    f = upload_via_route(native_client, big, folder_id=None)
    with native_client.stream("GET", f"/api/v1/print/library/{f['id']}/file") as r:
        assert int(r.headers["content-length"]) == 80 * 1024 * 1024


def test_the_same_idempotency_key_answers_the_first_upload(
    native_client: TestClient, three_mf: Path
) -> None:
    a = upload_via_route(native_client, three_mf, key="k1")
    b = upload_via_route(native_client, three_mf, key="k1")
    assert a == b
    assert (
        len(native_client.get("/api/v1/print/library", params={"all": True}).json()["files"]) == 1
    )


@pytest.fixture
def tiny_store_client(settings: Settings, s3: S3Settings) -> Iterator[TestClient]:
    with TestClient(create_app(_native_settings(settings, s3, store_max_total_bytes=16))) as client:
        yield client


def test_a_full_store_is_507(tiny_store_client: TestClient, three_mf: Path) -> None:
    with three_mf.open("rb") as data:
        r = tiny_store_client.post(
            "/api/v1/print/library/files",
            files={"file": ("bracket.3mf", data, "application/octet-stream")},
            headers=press(),
        )
    assert r.status_code == 507, r.text
    assert (
        tiny_store_client.get("/api/v1/print/library", params={"all": True}).json()["files"] == []
    )
