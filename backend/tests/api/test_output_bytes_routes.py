"""The output routes read an output's bytes through `OutputStore` (#7256, outputs-in-
Postgres spec 2026-11-23 §4.4): the blob the row names first, the NFS file when the row
names none or the blob is lost. A stream sets its length, and one that breaks off past
the verified prefetch ends short, never clean."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import AsyncGenerator, Iterator, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.jobs import GLB_MEDIA_TYPE
from scadbuddy.api.media import IMMUTABLE_CACHE_CONTROL
from scadbuddy.api.output_streams import stream_response
from scadbuddy.api.template_ui import UI_FILE_HEADERS
from scadbuddy.library.output_bytes import _PREFETCH, OutputBytes, OutputStream
from scadbuddy.library.output_rows import ByteWrite, output_blob_key, output_file_key
from scadbuddy.library.outputs import (
    FILES_DIR,
    MODEL_NAME,
    PREVIEW_NAME,
    THUMBNAIL_NAME,
    OutputStore,
    download_filename,
)
from scadbuddy.store.content import ContentStore
from scadbuddy.store.index import Pool
from tests.api.conftest import PNG_BYTES, wait_for_job
from tests.support.operations import press
from tests.support.store import flip_first_byte, local_content, store_pool

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    return tmp_path / "spool"


@pytest.fixture
def content(tmp_path: Path, pool: Pool, spool: Path) -> ContentStore:
    store = local_content(tmp_path / "remote", pool)
    store.spool = spool
    return store


def _state(client: TestClient) -> AppState:
    app = client.app
    assert isinstance(app, FastAPI)
    state: AppState = getattr(app.state, STATE_ATTR)
    return state


@pytest.fixture
def outputs(client: TestClient, content: ContentStore) -> OutputStore:
    """The app's store, with a `local_content` blob store for the outputs' bytes."""
    store = _state(client).outputs
    assert store.rows is not None
    store.bytes = OutputBytes(content)
    return store


def _saved(client: TestClient, model: str) -> str:
    queued = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    assert queued.status_code == 202, queued.text
    job_id: str = queued.json()["job_id"]
    wait_for_job(client, job_id)
    saved = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id}, headers=press())
    assert saved.status_code == 201, saved.text
    output_id: str = saved.json()["id"]
    return output_id


def _copy(client: TestClient, outputs: OutputStore, output_id: str) -> None:
    """What the byte copy does: put the folder, then name every blob in the row."""
    assert outputs.bytes is not None and outputs.rows is not None
    directory = outputs.directory(output_id)
    slug = outputs.get(output_id).slug
    portal = client.portal
    assert portal is not None
    blobs = portal.call(outputs.bytes.put_folder, directory, output_id, slug)
    assert outputs.rows.write_byte_copy([ByteWrite(output_id, blobs, "stamp", [])])[0] == {
        output_id
    }


def _copied(
    client: TestClient,
    model: str,
    outputs: OutputStore,
    *,
    thumbnail: bool = True,
    model_bytes: bytes | None = None,
) -> str:
    """A saved output with a thumbnail and two extra files, its bytes all blobs."""
    output_id = _saved(client, model)
    directory = outputs.directory(output_id)
    if thumbnail:
        (directory / THUMBNAIL_NAME).write_bytes(PNG_BYTES)
    (directory / FILES_DIR).mkdir(exist_ok=True)
    (directory / FILES_DIR / "a.stl").write_bytes(b"a stl")
    (directory / FILES_DIR / "b.svg").write_bytes(b"<svg/>")
    if model_bytes is not None:
        (directory / MODEL_NAME).write_bytes(model_bytes)
    _copy(client, outputs, output_id)
    return output_id


def _lose(client: TestClient, content: ContentStore, key: str) -> None:
    """The store lost the blob the row names: removed by hand, a bad sweep."""
    portal = client.portal
    assert portal is not None
    found = portal.call(content.stat, key)
    assert found is not None
    portal.call(content.backend.remove, found.ref.backend_id)


def _spooled(spool: Path) -> list[str]:
    return sorted(os.listdir(spool)) if spool.is_dir() else []


def test_download_streams_blob_with_length_and_etag(
    client: TestClient, model: str, outputs: OutputStore
) -> None:
    output_id = _copied(client, model, outputs)
    path = outputs.directory(output_id) / MODEL_NAME
    data = path.read_bytes()
    path.unlink()
    response = client.get(f"/api/v1/outputs/{output_id}/model.3mf")
    assert response.status_code == 200, response.text
    assert response.content == data
    assert response.headers["content-length"] == str(len(data))
    assert response.headers["etag"] == f'"{hashlib.sha256(data).hexdigest()}"'
    assert response.headers["accept-ranges"] == "none"
    assert response.headers["content-type"] == "model/3mf"
    filename = download_filename(outputs.get(output_id))
    assert response.headers["content-disposition"] == f'attachment; filename="{filename}"'


@pytest.mark.parametrize("blob", ["lost", "none"])
def test_download_falls_back_to_file_when_blob_missing(
    client: TestClient,
    model: str,
    outputs: OutputStore,
    content: ContentStore,
    caplog: pytest.LogCaptureFixture,
    blob: str,
) -> None:
    if blob == "lost":
        output_id = _copied(client, model, outputs)
        _lose(client, content, output_blob_key(output_id, "model"))
    else:
        # Saved with no blob store: the row names no blob, the file holds the bytes.
        outputs.bytes = None
        output_id = _saved(client, model)
        outputs.bytes = OutputBytes(content)
    # The file's own bytes, so a read of the blob cannot pass as one of the file.
    (outputs.directory(output_id) / MODEL_NAME).write_bytes(b"file bytes")
    with caplog.at_level(logging.WARNING, logger="scadbuddy.library.outputs"):
        response = client.get(f"/api/v1/outputs/{output_id}/model.3mf")
    assert response.status_code == 200, response.text
    assert response.content == b"file bytes"
    assert response.headers["content-length"] == str(len(b"file bytes"))
    # A file has no recorded sha256: a weak validator from its size and mtime.
    info = (outputs.directory(output_id) / MODEL_NAME).stat()
    assert response.headers["etag"] == f'W/"{info.st_size:x}-{info.st_mtime_ns:x}"'
    if blob == "lost":
        assert "blob is lost" in caplog.text


@pytest.mark.parametrize("role", ["preview", "thumbnail"])
def test_preview_and_thumbnail_fall_back_to_file_when_blob_lost(
    client: TestClient, model: str, outputs: OutputStore, content: ContentStore, role: str
) -> None:
    output_id = _copied(client, model, outputs)
    _lose(client, content, output_blob_key(output_id, role))  # type: ignore[arg-type]
    name, url = {
        "preview": (PREVIEW_NAME, "preview.glb"),
        "thumbnail": (THUMBNAIL_NAME, "thumbnail"),
    }[role]
    data = b"\x89PNG\r\n\x1a\nfile " + role.encode()
    (outputs.directory(output_id) / name).write_bytes(data)
    response = client.get(f"/api/v1/outputs/{output_id}/{url}")
    assert response.status_code == 200, response.text
    assert response.content == data


def test_download_404_when_neither_blob_nor_file(
    client: TestClient, model: str, outputs: OutputStore, content: ContentStore
) -> None:
    output_id = _copied(client, model, outputs)
    _lose(client, content, output_blob_key(output_id, "model"))
    (outputs.directory(output_id) / MODEL_NAME).unlink()
    response = client.get(f"/api/v1/outputs/{output_id}/model.3mf")
    assert response.status_code == 404
    assert response.json()["detail"] == f"output {output_id!r} has no 3MF"


def test_preview_streams_blob(client: TestClient, model: str, outputs: OutputStore) -> None:
    output_id = _copied(client, model, outputs)
    path = outputs.directory(output_id) / PREVIEW_NAME
    data = path.read_bytes()
    path.unlink()
    response = client.get(f"/api/v1/outputs/{output_id}/preview.glb")
    assert response.status_code == 200, response.text
    assert response.content == data
    assert response.headers["content-type"] == GLB_MEDIA_TYPE
    # The blob and its file both gone: the 404 text stays.
    outputs.bytes = None
    response = client.get(f"/api/v1/outputs/{output_id}/preview.glb")
    assert response.status_code == 404
    assert response.json()["detail"] == f"output {output_id!r} has no preview mesh"


def test_output_file_streams_blob(client: TestClient, model: str, outputs: OutputStore) -> None:
    output_id = _copied(client, model, outputs)
    files = outputs.directory(output_id) / FILES_DIR
    (files / "a.stl").unlink()
    (files / "b.svg").unlink()
    stl = client.get(f"/api/v1/outputs/{output_id}/files/a.stl")
    assert stl.status_code == 200, stl.text
    assert stl.content == b"a stl"
    assert stl.headers["content-type"] == "application/octet-stream"
    assert stl.headers["content-length"] == str(len(b"a stl"))
    assert stl.headers["etag"] == f'"{hashlib.sha256(b"a stl").hexdigest()}"'
    assert stl.headers["content-disposition"] == 'attachment; filename="a.stl"'
    for header, value in UI_FILE_HEADERS.items():
        assert stl.headers[header] == value
    svg = client.get(f"/api/v1/outputs/{output_id}/files/b.svg")
    assert svg.status_code == 200, svg.text
    assert svg.content == b"<svg/>"
    assert svg.headers["content-type"].startswith("image/svg+xml")
    missing = client.get(f"/api/v1/outputs/{output_id}/files/c.stl")
    assert missing.status_code == 404
    assert missing.json()["detail"] == f"no file 'c.stl' on output {output_id}"


def test_output_file_falls_back_to_file_when_blob_lost(
    client: TestClient, model: str, outputs: OutputStore, content: ContentStore
) -> None:
    output_id = _copied(client, model, outputs)
    _lose(client, content, output_file_key(output_id, 0))
    (outputs.directory(output_id) / FILES_DIR / "a.stl").write_bytes(b"file stl")
    response = client.get(f"/api/v1/outputs/{output_id}/files/a.stl")
    assert response.status_code == 200, response.text
    assert response.content == b"file stl"


def test_thumbnail_streams_blob_and_draws_from_preview_blob(
    client: TestClient, model: str, outputs: OutputStore, spool: Path
) -> None:
    output_id = _copied(client, model, outputs)
    (outputs.directory(output_id) / THUMBNAIL_NAME).unlink()
    response = client.get(f"/api/v1/outputs/{output_id}/thumbnail")
    assert response.status_code == 200, response.text
    assert response.content == PNG_BYTES
    assert response.headers["content-type"] == "image/png"
    detail = client.get(f"/api/v1/outputs/{output_id}").json()
    assert detail["has_thumbnail"] is True
    assert detail["thumbnail_version"] == hashlib.sha256(PNG_BYTES).hexdigest()[:16]

    # One with no thumbnail, whose preview is only a blob: drawn from that blob.
    outputs.draw_thumbnails = True
    drawn = _copied(client, model, outputs, thumbnail=False)
    (outputs.directory(drawn) / PREVIEW_NAME).unlink()
    detail = client.get(f"/api/v1/outputs/{drawn}").json()
    assert detail["has_thumbnail"] is True
    assert detail["thumbnail_version"] is None
    response = client.get(f"/api/v1/outputs/{drawn}/thumbnail")
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert outputs.thumbnail_version(drawn) is not None
    assert _spooled(spool) == []


def test_thumbnail_drawn_when_its_blob_is_lost_and_no_file(
    client: TestClient, model: str, outputs: OutputStore, content: ContentStore
) -> None:
    # The index still names the thumbnail blob, but the store lost it and no file holds
    # it: the thumbnail is drawn from the preview, not counted as drawn meanwhile.
    outputs.draw_thumbnails = True
    output_id = _copied(client, model, outputs)
    _lose(client, content, output_blob_key(output_id, "thumbnail"))
    (outputs.directory(output_id) / THUMBNAIL_NAME).unlink()
    response = client.get(f"/api/v1/outputs/{output_id}/thumbnail")
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert response.content != PNG_BYTES


def test_thumbnail_404_without_preview(
    client: TestClient, model: str, outputs: OutputStore, content: ContentStore
) -> None:
    outputs.draw_thumbnails = True
    output_id = _copied(client, model, outputs, thumbnail=False)
    _lose(client, content, output_blob_key(output_id, "preview"))
    (outputs.directory(output_id) / PREVIEW_NAME).unlink()
    # The index still names the lost blob, so the detail may offer a thumbnail; the
    # read finds no preview to draw from and answers 404, never 500.
    response = client.get(f"/api/v1/outputs/{output_id}/thumbnail")
    assert response.status_code == 404
    assert response.json()["detail"] == f"output {output_id!r} has no thumbnail"


def test_view_and_plates_read_blob_through_temp_file(
    client: TestClient, model: str, outputs: OutputStore, spool: Path
) -> None:
    output_id = _copied(client, model, outputs)
    directory = outputs.directory(output_id)
    (directory / PREVIEW_NAME).unlink()
    (directory / MODEL_NAME).unlink()
    view = client.get(f"/api/v1/outputs/{output_id}/views/iso.png")
    assert view.status_code == 200, view.text
    assert view.content.startswith(b"\x89PNG\r\n\x1a\n")
    plates = client.get(f"/api/v1/outputs/{output_id}/plates")
    assert plates.status_code == 200, plates.text
    assert [plate["index"] for plate in plates.json()] == [1]
    cover = client.get(f"/api/v1/outputs/{output_id}/plates/1/thumbnail")
    # The saved 3MF carries plate 1's cover, read from the blob's temporary file.
    assert cover.status_code == 200, cover.text
    assert cover.content.startswith(b"\x89PNG\r\n\x1a\n")
    # Each temporary file went when its reader was done.
    assert _spooled(spool) == []


def test_view_and_plates_404_without_bytes(
    client: TestClient, model: str, outputs: OutputStore
) -> None:
    output_id = _copied(client, model, outputs)
    directory = outputs.directory(output_id)
    (directory / PREVIEW_NAME).unlink()
    (directory / MODEL_NAME).unlink()
    outputs.bytes = None
    view = client.get(f"/api/v1/outputs/{output_id}/views/iso.png")
    assert view.status_code == 404
    assert view.json()["detail"] == f"the preview for output {output_id!r} is gone"
    plates = client.get(f"/api/v1/outputs/{output_id}/plates")
    assert plates.status_code == 404
    assert plates.json()["detail"] == f"output {output_id!r} has no 3MF"


def test_download_of_corrupt_large_blob_ends_short(
    app: FastAPI,
    client: TestClient,
    model: str,
    outputs: OutputStore,
    content: ContentStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data = os.urandom(4 * _PREFETCH)
    output_id = _copied(client, model, outputs, model_bytes=data)
    portal = client.portal
    assert portal is not None
    portal.call(flip_first_byte, content, output_blob_key(output_id, "model"))
    with caplog.at_level(logging.ERROR, logger="scadbuddy.api.output_streams"):
        sent = portal.call(_raw_get, app, f"/api/v1/outputs/{output_id}/model.3mf")
    assert sent.status == 200
    assert sent.headers["content-length"] == str(len(data))
    # The body breaks off short of its length, and the response never ends clean.
    assert 0 < len(sent.body) < len(data)
    assert not sent.ended
    assert sent.error is not None
    assert any(record.levelno == logging.ERROR for record in caplog.records)


@dataclass
class _Sent:
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    #: Whether the last body message said the body ended.
    ended: bool = False
    error: BaseException | None = None


async def _raw_get(app: FastAPI, path: str) -> _Sent:
    """What the app sends for a GET, message by message: the test client drops a body
    that breaks off."""
    sent = _Sent()
    never = anyio.Event()
    asked = False

    async def receive() -> dict[str, Any]:
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await never.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            sent.status = message["status"]
            sent.headers = {k.decode(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            sent.body += message.get("body", b"")
            sent.ended = not message.get("more_body", False)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    try:
        await app(scope, receive, send)
    except Exception as error:
        sent.error = error
    return sent


async def test_stream_response_closes_its_stream_when_never_sent() -> None:
    """A client gone before the first byte cancels the response, so the body never
    runs: the response still closes the stream. The close runs in a thread
    (`_file_stream`'s), which only the shield lets a cancelled response wait for."""
    closed: list[bool] = []

    async def chunks() -> AsyncGenerator[bytes]:
        yield b"never sent"

    async def on_close() -> None:
        await asyncio.to_thread(lambda: None)
        # Reached only when the wait for the thread was not cancelled.
        closed.append(True)

    stream = OutputStream(size=10, sha256=None, chunks=chunks(), _on_close=on_close)
    response = stream_response(stream, media_type="model/3mf", headers={})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}}
    with anyio.CancelScope() as cancelled:

        async def send(message: MutableMapping[str, Any]) -> None:
            # The client went away: the server cancels the response.
            cancelled.cancel()
            await anyio.sleep(0)

        await response(scope, receive, send)
    assert cancelled.cancelled_caught
    assert closed == [True]


#: Each byte route, as its URL under the output and the blob key's role or file index.
_BYTE_ROUTES = [
    ("model.3mf", "model"),
    ("preview.glb", "preview"),
    ("thumbnail", "thumbnail"),
    ("files/a.stl", "file-0"),
]


def _count_gets(content: ContentStore, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every read of a blob's bytes from the backend."""
    gets: list[str] = []
    get = content.get

    def counted(ref: Any) -> Any:
        gets.append(ref.sha256)
        return get(ref)

    monkeypatch.setattr(content, "get", counted)
    return gets


@pytest.mark.parametrize(("url", "role"), _BYTE_ROUTES)
def test_conditional_get_of_a_blob_is_304_with_no_blob_read(
    client: TestClient,
    model: str,
    outputs: OutputStore,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    role: str,
) -> None:
    output_id = _copied(client, model, outputs)
    first = client.get(f"/api/v1/outputs/{output_id}/{url}")
    assert first.status_code == 200, first.text
    etag = first.headers["etag"]
    gets = _count_gets(content, monkeypatch)
    again = client.get(f"/api/v1/outputs/{output_id}/{url}", headers={"if-none-match": etag})
    assert again.status_code == 304, again.text
    assert again.content == b""
    assert again.headers["etag"].removeprefix("W/") == etag.removeprefix("W/")
    assert gets == []
    # Another tag gets the bytes.
    other = client.get(f"/api/v1/outputs/{output_id}/{url}", headers={"if-none-match": '"x"'})
    assert other.status_code == 200
    assert other.content == first.content


def test_conditional_get_of_the_gzipped_preview_is_304(
    client: TestClient, model: str, outputs: OutputStore
) -> None:
    # `Compression` weakens the tag of a gzipped answer; the weak tag still matches.
    output_id = _copied(client, model, outputs)
    url = f"/api/v1/outputs/{output_id}/preview.glb"
    first = client.get(url, headers={"accept-encoding": "gzip"})
    assert first.status_code == 200, first.text
    assert first.headers["content-encoding"] == "gzip"
    etag = first.headers["etag"]
    assert etag.startswith('W/"')
    again = client.get(url, headers={"accept-encoding": "gzip", "if-none-match": etag})
    assert again.status_code == 304, again.text


@pytest.mark.parametrize(("url", "role"), _BYTE_ROUTES)
def test_conditional_get_of_a_file_is_304_by_its_weak_tag(
    client: TestClient,
    model: str,
    outputs: OutputStore,
    content: ContentStore,
    url: str,
    role: str,
) -> None:
    # Saved with no blob store: the files hold the bytes.
    outputs.bytes = None
    output_id = _saved(client, model)
    directory = outputs.directory(output_id)
    (directory / THUMBNAIL_NAME).write_bytes(PNG_BYTES)
    (directory / FILES_DIR).mkdir(exist_ok=True)
    (directory / FILES_DIR / "a.stl").write_bytes(b"a stl")
    outputs.bytes = OutputBytes(content)
    first = client.get(
        f"/api/v1/outputs/{output_id}/{url}", headers={"accept-encoding": "identity"}
    )
    assert first.status_code == 200, first.text
    etag = first.headers["etag"]
    assert etag.startswith('W/"')
    again = client.get(
        f"/api/v1/outputs/{output_id}/{url}",
        headers={"accept-encoding": "identity", "if-none-match": etag},
    )
    assert again.status_code == 304, again.text
    assert again.headers["etag"] == etag


@pytest.mark.parametrize("blob", [True, False])
@pytest.mark.parametrize("role", ["thumbnail", "preview"])
def test_versioned_thumbnail_and_preview_are_immutable(
    client: TestClient,
    model: str,
    outputs: OutputStore,
    content: ContentStore,
    role: str,
    blob: bool,
) -> None:
    if blob:
        output_id = _copied(client, model, outputs)
    else:
        outputs.bytes = None
        output_id = _saved(client, model)
        (outputs.directory(output_id) / THUMBNAIL_NAME).write_bytes(PNG_BYTES)
        outputs.bytes = OutputBytes(content)
    url = f"/api/v1/outputs/{output_id}/{'thumbnail' if role == 'thumbnail' else 'preview.glb'}"
    version = outputs.role_version(output_id, role)  # type: ignore[arg-type]
    assert version is not None
    # The detail names the version a page puts in the URL.
    detail = client.get(f"/api/v1/outputs/{output_id}").json()
    assert detail[f"{role}_version"] == version
    current = client.get(url, params={"v": version})
    assert current.status_code == 200, current.text
    assert current.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL
    # The 304 of a versioned read is immutable too.
    again = client.get(
        url, params={"v": version}, headers={"if-none-match": current.headers["etag"]}
    )
    assert again.status_code == 304
    assert again.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL
    for params in ({}, {"v": "old"}):
        plain = client.get(url, params=params)
        assert plain.status_code == 200
        assert "cache-control" not in plain.headers
