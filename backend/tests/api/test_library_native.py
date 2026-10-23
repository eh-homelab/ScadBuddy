"""The `/print/library` routes on `library_backend=native` (plan
2026-10-17-native-print-library, Tasks 4 and 5): the same shapes as on Bambuddy, answered
from ScadBuddy's own library over the S3 content store. The writes go through the routes
with an `Idempotency-Key`, so the operation kinds really run."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import respx
import trimesh
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.api.deps import STATE_ATTR, get_render
from scadbuddy.api.library_print import start_library_run
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.print_library.component import PRINT_LIBRARY
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.remote import RemoteLibrary
from scadbuddy.print_library.store import NativeFile, Unset
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import render_plate_thumbnails
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import ReuseLostError, StoreFullError
from tests.api.conftest import wait_for_job
from tests.api.test_analyzers import SILK_REQUEST
from tests.api.test_analyzers import bambuddy_routes as analysis_routes
from tests.api.test_analyzers import library_routes as analysis_library_routes
from tests.api.test_arrange_api import Arranger
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_library import flow_copy_routes, run_library, uploaded_name
from tests.api.test_print_run_choices import body as run_body
from tests.api.test_print_run_choices import run_routes
from tests.api.test_send import configure
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS
from tests.support.no_bambuddy import forbid_bambuddy
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


# --- Task 7a: printing a native file ------------------------------------------------------


def _one_colour(path: Path) -> Path:
    parts = [ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box(extents=(10, 10, 4)))]
    write_bambu_3mf(parts, path, thumbnails=None, model_name="plug")
    return path


def _asked_bambuddy_for(file_id: int) -> list[str]:
    return [
        call.request.url.path
        for call in respx.calls
        if call.request.url.path.startswith(f"/api/v1/library/files/{file_id}")
    ]


def _settled(client: TestClient, started: httpx.Response) -> dict[str, Any]:
    assert started.status_code == 202, started.text
    op: dict[str, Any] = started.json()
    deadline = time.monotonic() + 30
    while op["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
        op = client.get(f"/api/v1/operations/{op['id']}").json()
    return op


SLOT_1 = {"slots": [{"slot_id": 1, "spool_id": 9}]}


@respx.mock
def test_print_dialog_choices_for_a_native_file(native_client: TestClient, tmp_path: Path) -> None:
    configure(native_client)
    run_routes()
    plug = _upload(native_client, _one_colour(tmp_path / "plug.3mf"))

    choices = native_client.get(f"/api/v1/print/library/{plug.id}/choices")

    assert choices.status_code == 200, choices.text
    # Its slots are read from the file itself; Bambuddy never hears of the file.
    assert [slot["slot_id"] for slot in choices.json()["filaments"]["slots"]] == [1]
    assert _asked_bambuddy_for(plug.id) == []


@respx.mock
def test_the_print_check_and_filaments_of_a_native_file(
    native_client: TestClient, tmp_path: Path
) -> None:
    configure(native_client)
    run_routes()
    plug = _upload(native_client, _one_colour(tmp_path / "plug.3mf"))

    filaments = native_client.get(f"/api/v1/print/library/{plug.id}/filaments")
    check = native_client.post(
        f"/api/v1/print/library/{plug.id}/check", json={**run_body(), "filament_plan": SLOT_1}
    )

    assert filaments.status_code == 200, filaments.text
    assert [slot["slot_id"] for slot in filaments.json()["slots"]] == [1]
    assert check.status_code == 200, check.text
    assert _asked_bambuddy_for(plug.id) == []


@respx.mock
def test_a_preview_slice_of_a_native_file_starts(native_client: TestClient, tmp_path: Path) -> None:
    configure(native_client)
    upload = flow_copy_routes()
    run_routes()
    sliced = slice_routes()
    plug = _upload(native_client, _one_colour(tmp_path / "plug.3mf"))

    started = native_client.post(
        f"/api/v1/print/library/{plug.id}/preview-slice",
        json={**run_body(), "filament_plan": SLOT_1},
        headers=press(),
    )

    op = _settled(native_client, started)
    assert op["status"] == "succeeded", op
    assert uploaded_name(upload) == "plug (ScadBuddy).3mf"
    assert "/library/files/141/slice" in str(sliced.calls.last.request.url)
    assert _asked_bambuddy_for(plug.id) == []


@respx.mock
def test_a_native_file_prints_through_the_run(native_client: TestClient, tmp_path: Path) -> None:
    """The run on the in-process print worker reads the native file from the API's own
    library, and slices and queues its laid-out copy."""
    configure(native_client)
    upload = flow_copy_routes()
    run_routes()
    slice_routes()
    queued = queue_route()
    plug = _upload(native_client, _one_colour(tmp_path / "plug.3mf"))

    response = run_library(native_client, plug.id, json={**run_body(), "filament_plan": SLOT_1})

    assert response.status_code == 200, response.text
    assert response.json()["library_file_id"] == 141
    assert upload.call_count == 1
    assert json.loads(queued.calls.last.request.content)["library_file_id"] == 77
    assert _asked_bambuddy_for(plug.id) == []


def test_the_internal_routes_read_a_native_file(
    native_client: TestClient, uploaded: NativeFile, tmp_path: Path
) -> None:
    """What the print worker's `RemoteLibrary` reads, through the API's hidden routes."""

    async def read() -> tuple[Any, ...]:
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=native_client.app))
        remote = RemoteLibrary("http://scadbuddy", http)
        try:
            file = await remote.library_file(uploaded.id)
            plates = await remote.library_plates(uploaded.id)
            data = b"".join([c async for c in remote.download_library_file(uploaded.id)])
            with pytest.raises(ApiError) as missing:
                await remote.library_file(uploaded.id + 1)
        finally:
            await remote.aclose()
        return file, plates, data, missing.value

    assert native_client.portal is not None
    file, plates, data, missing = native_client.portal.call(read)

    assert file.filename == "bracket.3mf"
    assert [plate.index for plate in plates.plates] == [1]
    assert data == (tmp_path / "bracket.3mf").read_bytes()
    assert missing.status == 404
    paths = native_client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if path.startswith("/api/v1/internal/library")]


def test_the_internal_routes_on_bambuddy_answer_404(client: TestClient) -> None:
    response = client.get("/api/v1/internal/library/1000000001")
    assert response.status_code == 404
    assert response.json()["file_id"] == 1000000001


# --- Task 7b: the other readers of a native file ------------------------------------------


@pytest.fixture
def arranger(native_client: TestClient) -> Iterator[Arranger]:
    recorded = Arranger()
    native_client.app.dependency_overrides[get_render] = lambda: recorded  # type: ignore[attr-defined]
    yield recorded
    native_client.app.dependency_overrides.pop(get_render, None)  # type: ignore[attr-defined]


@respx.mock
def test_arrange_reads_a_native_files_objects(
    native_client: TestClient, uploaded: NativeFile, model: str, arranger: Arranger
) -> None:

    listed = native_client.get(f"/api/v1/print/library/{uploaded.id}/objects")
    response = native_client.post(
        "/api/v1/outputs/arrange",
        json={"objects": [{"library_file_id": uploaded.id}], "slug": model},
    )

    assert listed.status_code == 200, listed.text
    assert listed.json()["filename"] == "bracket.3mf"
    assert response.status_code == 202, response.text
    [inputs] = arranger.inputs
    assert inputs.sources == [f"library:{uploaded.id}"]
    assert _asked_bambuddy_for(uploaded.id) == []


@respx.mock
def test_arrange_resolves_a_filed_files_output(
    native_client: TestClient,
    uploaded_with_output: NativeFile,
    arranger: Arranger,
    model: str,
    paths: DataPaths,
) -> None:
    """The file is the output's copy, so its objects come from the output's manifest:
    this one records none, which is the output's own 409, naming it."""
    assert uploaded_with_output.output_id is not None
    meta = paths.outputs / model / uploaded_with_output.output_id / "meta.json"
    meta.write_text(
        OutputMeta(
            id=uploaded_with_output.output_id,
            slug=model,
            job_id="j",
            created_at=datetime.now(UTC),
            bbox_mm=BoundingBox(min=(0, 0, 0), max=(10, 10, 4), size=(10, 10, 4)),
        ).model_dump_json(),
        encoding="utf-8",
    )

    response = native_client.post(
        "/api/v1/outputs/arrange", json={"objects": [{"library_file_id": uploaded_with_output.id}]}
    )

    assert response.status_code == 409, response.text
    assert response.json()["output_ids"] == [uploaded_with_output.output_id]
    assert arranger.inputs == []


@respx.mock
def test_create_model_from_a_native_library_file(
    native_client: TestClient, uploaded: NativeFile
) -> None:

    created = native_client.post(
        "/api/v1/models/from-mesh",
        json={"name": "Bracket", "library_file_id": uploaded.id},
        headers=press(),
    )

    assert created.status_code == 201, created.text
    scene = native_client.get(f"/api/v1/models/{created.json()['slug']}/scene").json()
    assert scene["scene"]["source"]["file_id"] == uploaded.id
    assert _asked_bambuddy_for(uploaded.id) == []


@respx.mock
def test_all_plates_analysis_of_a_native_file_reads_native_plates(
    native_client: TestClient, uploaded: NativeFile
) -> None:
    configure(native_client)
    analysis_routes()
    # The printer and its loaded spools; the file's own routes are there to be refused.
    analysis_library_routes(uploaded.id, b"")

    response = native_client.post(
        "/api/v1/analyzers/run",
        json={
            "target": {"library_file_id": uploaded.id},
            "request": {**SILK_REQUEST, "all_plates": True},
        },
    )

    assert response.status_code == 200, response.text
    report = response.json()
    assert report["library_file_id"] == uploaded.id
    inputs = {row["name"]: row for row in report["inputs"]}
    assert inputs["geometry"]["available"], inputs["geometry"]
    assert inputs["inventory"]["available"], inputs["inventory"]
    assert _asked_bambuddy_for(uploaded.id) == []


@respx.mock
def test_a_native_files_geometry_is_judged_with_no_bambuddy(
    native_client: TestClient, uploaded: NativeFile
) -> None:
    """With no Bambuddy at all, a native file's mesh is still read; only what Bambuddy
    alone answers (printers, spools) is unavailable."""
    response = native_client.post(
        "/api/v1/analyzers/run", json={"target": {"library_file_id": uploaded.id}}
    )

    assert response.status_code == 200, response.text
    inputs = {row["name"]: row for row in response.json()["inputs"]}
    assert inputs["geometry"]["available"], inputs["geometry"]
    assert not inputs["printer"]["available"]


@respx.mock
def test_a_native_files_preview_needs_no_bambuddy(
    native_client: TestClient, uploaded: NativeFile
) -> None:
    response = native_client.get(f"/api/v1/print/library/{uploaded.id}/preview.glb")

    assert response.status_code == 200, response.text
    assert response.content[:4] == b"glTF"


@respx.mock
def test_a_bambuddy_range_id_is_404_to_the_readers_on_native(native_client: TestClient) -> None:
    """One id space at a time: on native, Bambuddy's file 5 is no file, whatever reads it."""
    configure(native_client)

    objects = native_client.get("/api/v1/print/library/5/objects")
    preview = native_client.get("/api/v1/print/library/5/preview.glb")
    arrange = native_client.post(
        "/api/v1/outputs/arrange", json={"objects": [{"library_file_id": 5}], "slug": "x"}
    )

    created = native_client.post(
        "/api/v1/models/from-mesh", json={"name": "X", "library_file_id": 5}, headers=press()
    )

    assert (objects.status_code, preview.status_code, arrange.status_code) == (404, 404, 404)
    assert created.status_code == 404, created.text
    assert list(respx.calls) == []


@respx.mock
def test_a_native_id_is_404_to_arrange_on_bambuddy(client: TestClient) -> None:
    configure(client)

    response = client.get("/api/v1/print/library/1000000001/objects")

    assert response.status_code == 404, response.text
    assert list(respx.calls) == []


# --- Task 8: filing saved outputs ----------------------------------------------------------


#: A valid body for each `/print/library/{file_id}/*` route that takes one, by its path's
#: end and method; any other takes `{}`. A route added with a body of its own needs an
#: entry, or the sweep below sees its 422.
ROUTE_BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("run", "POST"): run_body(),
    ("check", "POST"): run_body(),
    ("preview-slice", "POST"): run_body(),
    ("{file_id}", "PATCH"): {"filename": "x.3mf"},
}


def _file_id_routes() -> list[tuple[str, str]]:
    """Every `/print/library/{file_id}…` route of the app, as (method, path), read from
    its OpenAPI (FastAPI 0.143 keeps an included router's routes inside it)."""
    app = create_app(
        Settings(
            data_dir=Path("/nonexistent"),
            frontend_dir=Path("/nonexistent"),
            database_url=UNUSED_DATABASE_URL,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
        )
    )
    return sorted(
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/v1/print/library/{file_id}")
        for method in item
    )


FILE_ID_ROUTES = _file_id_routes()


def _call(client: TestClient, method: str, path: str, file_id: int) -> httpx.Response:
    url = path.format(file_id=file_id, job_id=1, index=1)
    body = ROUTE_BODIES.get((path.rsplit("/", 1)[-1], method), {})
    response: httpx.Response = (
        client.get(url)
        if method == "GET"
        else client.request(method, url, json=body, headers=press())
    )
    return response


@respx.mock
@pytest.mark.parametrize(("method", "path"), FILE_ID_ROUTES)
def test_every_file_route_is_404_for_a_bambuddy_id_on_native(
    native_client: TestClient, method: str, path: str
) -> None:
    """One id space at a time (#3827): on native, Bambuddy's file 5 is no file to any
    route, and none asks Bambuddy about it."""
    configure(native_client)

    response = _call(native_client, method, path, 5)

    assert response.status_code == 404, response.text
    assert list(respx.calls) == []


@respx.mock
@pytest.mark.parametrize(("method", "path"), FILE_ID_ROUTES)
def test_every_file_route_is_404_for_a_native_id_on_bambuddy(
    client: TestClient, method: str, path: str
) -> None:
    configure(client)

    response = _call(client, method, path, 1_000_000_005)

    assert response.status_code == 404, response.text
    assert list(respx.calls) == []


@pytest.mark.parametrize(("file_id", "native"), [(1_000_000_005, None), (5, object())])
async def test_a_batch_run_of_the_other_librarys_file_is_404(file_id: int, native: Any) -> None:
    """A batch's Queue all starts runs past the routes, so `start_library_run` refuses
    the other library's file itself, before it accepts anything."""
    unused: Any = None
    with pytest.raises(ApiError) as raised:
        await start_library_run(
            file_id,
            PrintRunRequest.model_validate(run_body()),
            runs=unused,
            activity=unused,
            store=unused,
            paths=unused,
            budget=unused,
            downloads=None,
            native=native,
        )
    assert raised.value.status == 404


@respx.mock
@pytest.mark.parametrize("all_files", [False, True])
def test_listing_a_bambuddy_folder_on_native_is_404(
    native_client: TestClient, all_files: bool
) -> None:
    """One id space at a time: the other library's folder is no folder, not an empty one."""
    configure(native_client)

    response = native_client.get("/api/v1/print/library", params={"folder_id": 5, "all": all_files})

    assert response.status_code == 404, response.text
    assert response.json()["folder_id"] == 5
    assert list(respx.calls) == []


@respx.mock
def test_listing_a_native_folder_on_bambuddy_is_404(client: TestClient) -> None:
    configure(client)

    response = client.get("/api/v1/print/library", params={"folder_id": 1_000_000_005})

    assert response.status_code == 404, response.text
    assert response.json()["folder_id"] == 1_000_000_005
    assert list(respx.calls) == []


def test_the_sweep_covers_the_print_routes() -> None:
    assert ("POST", "/api/v1/print/library/{file_id}/run") in FILE_ID_ROUTES
    assert ("GET", "/api/v1/print/library/{file_id}/choices") in FILE_ID_ROUTES


@pytest.fixture
def widget(native_client: TestClient) -> str:
    """A committed template (the S3 store renders from a git snapshot) titled `Widget`."""
    created = native_client.post(
        "/api/v1/models",
        json={"name": "Widget", "source": "width = 10;\n"},
        headers=press(),
    )
    assert created.status_code == 201, created.text
    slug: str = created.json()["slug"]
    return slug


def _save(client: TestClient, slug: str, name: str) -> Any:
    job_id = client.post(f"/api/v1/models/{slug}/render", json={"params": {"width": 12}}).json()[
        "job_id"
    ]
    wait_for_job(client, job_id)
    return client.post(
        f"/api/v1/models/{slug}/outputs", json={"job_id": job_id, "name": name}, headers=press()
    )


def _all_files(client: TestClient) -> list[dict[str, Any]]:
    """Every live file in every folder, as the rows hold them."""
    rows = _native(client).rows.files(None, every_folder=True)
    return [{"id": f.id, "output_id": f.output_id, "folder_id": f.folder_id} for f in rows]


@pytest.mark.requires_git
def test_a_saved_output_is_filed_under_its_template_title(
    native_client: TestClient, widget: str
) -> None:
    saved = _save(native_client, widget, "First").json()
    listing = native_client.get("/api/v1/print/library").json()
    folder = next(f for f in listing["folders"] if f["name"] == "Widget")
    files = native_client.get("/api/v1/print/library", params={"folder_id": folder["id"]}).json()[
        "files"
    ]
    assert [(f["filename"], f["output_id"]) for f in files] == [
        (f"{widget}-first.3mf", saved["id"])
    ]


@pytest.mark.requires_git
def test_two_saves_share_one_folder(native_client: TestClient, widget: str) -> None:
    _save(native_client, widget, "One")
    _save(native_client, widget, "Two")
    folders = native_client.get("/api/v1/print/library").json()["folders"]
    assert [f["name"] for f in folders] == ["Widget"]
    assert len(_all_files(native_client)) == 2


@pytest.mark.requires_git
def test_output_delete_trashes_only_unmoved_copies(native_client: TestClient, widget: str) -> None:
    out = _save(native_client, widget, "One").json()
    kept = _save(native_client, widget, "Two").json()
    (kept_file,) = [f for f in _all_files(native_client) if f["output_id"] == kept["id"]]
    native_client.post(
        "/api/v1/print/library/move", json={"file_ids": [kept_file["id"]]}, headers=press()
    )
    for output in (out, kept):
        r = native_client.delete(
            f"/api/v1/outputs/{output['id']}?delete_inbox_copies=true", headers=press()
        )
        assert r.status_code == 204, r.text
    # The moved file stays, with no live output to name.
    assert [f["output_id"] for f in _all_files(native_client)] == [None]


@pytest.mark.requires_git
def test_a_failed_filing_does_not_fail_the_save(
    native_client: TestClient,
    widget: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def full(*_: Any, **__: Any) -> NativeFile:
        raise StoreFullError("the store is full")

    monkeypatch.setattr(NativeLibrary, "stage", full)
    saved = _save(native_client, widget, "One")
    assert saved.status_code == 201
    assert _all_files(native_client) == []
    (record,) = [r for r in caplog.records if "could not file a saved output" in r.getMessage()]
    assert record.id == saved.json()["id"]  # type: ignore[attr-defined]


def _file_into(client: TestClient, output_id: str, **body: int) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/api/v1/outputs/{output_id}/project-file", json=body, headers=press()
    )
    return response


@pytest.mark.requires_git
def test_generate_files_into_a_native_folder_with_no_bambuddy(
    native_client: TestClient, widget: str, spans: InMemorySpanExporter
) -> None:
    """#4507: no `bambuddy_url`, a native folder chosen: the 3MF is filed there by
    ScadBuddy alone, and the same folder again answers the file already there."""
    folder = mkdir(native_client, "Gifts")["id"]
    saved = _save(native_client, widget, "First").json()

    with forbid_bambuddy(spans, "http://bambuddy.test"):
        first = _file_into(native_client, saved["id"], folder_id=folder)
        again = _file_into(native_client, saved["id"], folder_id=folder)

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["project_id"] is None
    assert body["folder_id"] == folder
    assert body["created"] is True
    assert body["bambuddy_url"] is None
    assert body["edit_url"] is None
    assert body["filename"].endswith(".3mf")
    files = native_client.get("/api/v1/print/library", params={"folder_id": folder}).json()["files"]
    assert [(f["id"], f["filename"], f["output_id"]) for f in files] == [
        (body["library_file_id"], body["filename"], saved["id"])
    ]
    row = _native(native_client).rows.file(body["library_file_id"])
    assert row is not None and row.filed is True
    assert again.status_code == 200, again.text
    assert again.json() == {**body, "created": False}


@pytest.mark.requires_git
def test_generate_into_an_unknown_native_folder_is_404(
    native_client: TestClient, widget: str, spans: InMemorySpanExporter
) -> None:
    saved = _save(native_client, widget, "First").json()

    with forbid_bambuddy(spans, "http://bambuddy.test"):
        response = _file_into(native_client, saved["id"], folder_id=1_999_999_999)

    assert response.status_code == 404, response.text


@pytest.mark.requires_git
def test_generate_into_a_project_on_native_still_needs_bambuddy(
    native_client: TestClient, widget: str
) -> None:
    """A project is Bambuddy's: with no `bambuddy_url` it is the 409 it always was."""
    saved = _save(native_client, widget, "First").json()

    response = _file_into(native_client, saved["id"], project_id=7)

    assert response.status_code == 409, response.text
