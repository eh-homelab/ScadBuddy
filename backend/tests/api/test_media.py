"""Several images and videos per template (#274): stored in ``media/`` beside the
source, ordered by `template_media` rows (a built-in's by its bundled model.json),
served with Range support, written through their own upload gate."""

from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from starlette.types import Message

from scadbuddy.api.limits import MAX_MULTIPART_BODY_BYTES
from scadbuddy.api.media import MAX_CONCURRENT_THUMBNAILS, THUMBNAIL_VERSION
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.history import GIT, git_env
from scadbuddy.main import create_app
from tests.api.conftest import PNG_BYTES
from tests.conftest import UNUSED_TEMPORAL_ADDRESS

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

# A NUL, as every real PNG has, so git stores it as binary.
PNG = PNG_BYTES + b"\x00"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x01" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00" + bytes(range(256)) * 4
WEBM = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01" + b"\x00" * 64
BUILTIN = "builtin:keychain"


@pytest.fixture
def bundled(seed_dir: Path) -> Path:
    directory = seed_dir / "keychain"
    (directory / "media").mkdir(parents=True)
    (directory / "model.scad").write_text("width = 10;\n", encoding="utf-8")
    (directory / "media" / "front.png").write_bytes(PNG)
    (directory / "model.json").write_text(
        json.dumps(
            {"name": "Keychain", "media": [{"id": "front", "file": "front.png", "kind": "image"}]}
        ),
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def settings(data_dir: Path, seed_dir: Path, fake_openscad: str, pg_conninfo: str) -> Settings:
    """The API tests' settings, with the Postgres the media list lives in."""
    return Settings(
        openscad=fake_openscad,
        data_dir=data_dir,
        seed_models_dir=seed_dir,
        frontend_dir=Path("/nonexistent"),
        database_url=pg_conninfo,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
    )


@pytest.fixture
def client(app: FastAPI, bundled: Path) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _upload(
    client: TestClient,
    slug: str,
    payload: bytes,
    name: str = "file.bin",
    *,
    caption: str | None = None,
    poster: bytes | None = None,
) -> Any:
    files: dict[str, tuple[str, bytes, str]] = {"file": (name, payload, "application/octet-stream")}
    if poster is not None:
        files["poster"] = ("poster.png", poster, "image/png")
    data = {"caption": caption} if caption is not None else None
    return client.post(f"/api/v1/models/{slug}/media", files=files, data=data)


def _media(client: TestClient, slug: str) -> list[dict[str, Any]]:
    listed: list[dict[str, Any]] = client.get(f"/api/v1/models/{slug}").json()["media"]
    return listed


def _tracked(paths: DataPaths) -> list[str]:
    return subprocess.run(
        [GIT, "ls-files"],
        cwd=paths.models,
        env=git_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()


def test_a_template_without_media_lists_none(client: TestClient, model: str) -> None:
    assert _media(client, model) == []


def test_a_legacy_thumbnail_is_listed_as_one_image(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    (paths.model_dir(model) / "thumbnail.png").write_bytes(PNG)

    assert _media(client, model) == [
        {
            "id": "thumbnail",
            "file": "thumbnail.png",
            "kind": "image",
            "caption": "",
            "poster": None,
            "missing": False,
            "readonly": False,
            "content_type": "image/png",
            "size": len(PNG),
        }
    ]
    served = client.get(f"/api/v1/models/{model}/media/thumbnail")
    assert served.status_code == 200
    assert served.content == PNG


@pytest.mark.parametrize(
    ("payload", "kind", "content_type"),
    [
        (PNG, "image", "image/png"),
        (JPEG, "image", "image/jpeg"),
        (WEBP, "image", "image/webp"),
        (MP4, "video", "video/mp4"),
        (WEBM, "video", "video/webm"),
    ],
)
def test_an_upload_is_typed_by_its_bytes(
    client: TestClient, model: str, payload: bytes, kind: str, content_type: str
) -> None:
    # The name and the declared type say nothing true: only the bytes count.
    response = _upload(client, model, payload, "misleading.txt", caption="Front")

    assert response.status_code == 200, response.text
    [item] = response.json()["media"]
    assert item["kind"] == kind
    assert item["content_type"] == content_type
    assert item["caption"] == "Front"
    assert item["size"] == len(payload)
    assert len(item["id"]) == 12
    served = client.get(f"/api/v1/models/{model}/media/{item['id']}")
    assert served.status_code == 200
    assert served.content == payload
    assert served.headers["content-type"] == content_type
    assert served.headers["cache-control"] == "private, max-age=31536000, immutable"


def test_an_upload_that_is_not_media_is_refused(client: TestClient, model: str) -> None:
    response = _upload(client, model, b"just some text, not a picture", "photo.png")

    assert response.status_code == 415, response.text
    assert _media(client, model) == []


def test_a_range_request_is_answered_with_that_range(client: TestClient, model: str) -> None:
    item = _upload(client, model, MP4).json()["media"][0]

    response = client.get(
        f"/api/v1/models/{model}/media/{item['id']}", headers={"Range": "bytes=0-99"}
    )

    assert response.status_code == 206
    assert response.content == MP4[:100]
    assert response.headers["content-range"] == f"bytes 0-99/{len(MP4)}"


def test_a_video_carries_its_poster(client: TestClient, model: str) -> None:
    item = _upload(client, model, WEBM, poster=JPEG).json()["media"][0]

    assert item["poster"] is not None
    poster = client.get(f"/api/v1/models/{model}/media/{item['id']}/poster")
    assert poster.status_code == 200
    assert poster.content == JPEG
    assert poster.headers["content-type"] == "image/jpeg"


def test_a_poster_must_be_an_image(client: TestClient, model: str) -> None:
    assert _upload(client, model, WEBM, poster=MP4).status_code == 415
    assert _media(client, model) == []


def test_an_image_has_no_poster(client: TestClient, model: str) -> None:
    image = _upload(client, model, PNG).json()["media"][0]

    assert _upload(client, model, PNG, poster=JPEG).status_code == 422
    assert client.get(f"/api/v1/models/{model}/media/{image['id']}/poster").status_code == 404


def _thumbnail_url(slug: str, item_id: str) -> str:
    return f"/api/v1/models/{slug}/media/{item_id}/thumbnail?v={THUMBNAIL_VERSION}"


def _real_image(size: tuple[int, int], fmt: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 40, 40)).save(out, fmt)
    return out.getvalue()


def test_a_thumbnail_is_a_small_copy_of_the_image(client: TestClient, model: str) -> None:
    original = _real_image((2000, 1500), "PNG")
    item = _upload(client, model, original).json()["media"][0]

    response = client.get(_thumbnail_url(model, item["id"]))

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/webp"
    assert "immutable" in response.headers["cache-control"]
    assert len(response.content) < len(original)
    with Image.open(io.BytesIO(response.content)) as small:
        assert small.size == (192, 144)


def test_a_videos_thumbnail_is_its_poster_shrunk(client: TestClient, model: str) -> None:
    item = _upload(client, model, WEBM, poster=_real_image((800, 600), "JPEG")).json()["media"][0]

    response = client.get(_thumbnail_url(model, item["id"]))

    assert response.status_code == 200, response.text
    with Image.open(io.BytesIO(response.content)) as small:
        assert small.size == (192, 144)


def test_a_thumbnail_is_turned_upright_by_its_exif_orientation(
    client: TestClient, model: str
) -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90 degrees clockwise to view.
    out = io.BytesIO()
    Image.new("RGB", (400, 200), (200, 40, 40)).save(out, "JPEG", exif=exif.tobytes())
    item = _upload(client, model, out.getvalue()).json()["media"][0]

    response = client.get(_thumbnail_url(model, item["id"]))

    assert response.status_code == 200, response.text
    with Image.open(io.BytesIO(response.content)) as small:
        assert small.size == (96, 192)


def test_a_video_with_no_poster_has_no_thumbnail(client: TestClient, model: str) -> None:
    item = _upload(client, model, WEBM).json()["media"][0]

    response = client.get(_thumbnail_url(model, item["id"]))

    assert response.status_code == 404
    assert response.json()["detail"] == f"{model!r} has no poster for {item['id']!r}"


def test_a_video_whose_poster_file_is_gone_has_no_thumbnail(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    item = _upload(client, model, WEBM, poster=JPEG).json()["media"][0]
    (paths.model_dir(model) / "media" / item["poster"]).unlink()

    response = client.get(_thumbnail_url(model, item["id"]))

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == f"{model!r} has no poster for {item['id']!r}"


@pytest.mark.parametrize("error", [ValueError, SyntaxError])
def test_an_image_whose_exif_cannot_be_read_is_its_own_thumbnail(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def malformed(*_: object, **__: object) -> None:
        raise error("malformed EXIF")

    monkeypatch.setattr("scadbuddy.api.media.ImageOps.exif_transpose", malformed)
    original = _real_image((400, 300), "JPEG")
    item = _upload(client, model, original).json()["media"][0]

    response = client.get(_thumbnail_url(model, item["id"]))

    assert response.status_code == 200, response.text
    assert response.content == original
    assert response.headers["content-type"] == "image/jpeg"


def test_an_undecodable_image_is_its_own_thumbnail(client: TestClient, model: str) -> None:
    item = _upload(client, model, PNG).json()["media"][0]

    response = client.get(_thumbnail_url(model, item["id"]))

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert "immutable" in response.headers["cache-control"]


def test_an_image_too_large_to_decode_cheaply_is_its_own_thumbnail(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scadbuddy.api.media.MAX_THUMBNAIL_SOURCE_PIXELS", 100 * 100)
    original = _real_image((101, 100), "PNG")
    item = _upload(client, model, original).json()["media"][0]

    response = client.get(_thumbnail_url(model, item["id"]))

    assert response.status_code == 200
    assert response.content == original
    assert response.headers["content-type"] == "image/png"
    assert "immutable" in response.headers["cache-control"]


def test_a_large_jpeg_that_draft_makes_cheap_is_still_shrunk(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1600x1600 is over the cap, but a JPEG drafts at 1/8 to 200x200, which is under it.
    monkeypatch.setattr("scadbuddy.api.media.MAX_THUMBNAIL_SOURCE_PIXELS", 250 * 250)
    item = _upload(client, model, _real_image((1600, 1600), "JPEG")).json()["media"][0]

    response = client.get(_thumbnail_url(model, item["id"]))

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(response.content)) as small:
        assert small.size == (192, 192)


def test_a_legacy_file_in_another_format_is_not_decoded(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    # A GIF Pillow can read, mislabeled as the legacy thumbnail.png: only PNG, JPEG and
    # WebP decoders may run, so it is served as it is, not shrunk.
    gif = _real_image((400, 300), "GIF")
    (paths.model_dir(model) / "thumbnail.png").write_bytes(gif)

    response = client.get(_thumbnail_url(model, "thumbnail"))

    assert response.status_code == 200, response.text
    assert response.content == gif


def test_a_thumbnail_is_cached_as_immutable_only_at_the_current_version(
    client: TestClient, model: str
) -> None:
    item = _upload(client, model, _real_image((400, 300), "PNG")).json()["media"][0]
    unversioned = f"/api/v1/models/{model}/media/{item['id']}/thumbnail"

    current = client.get(_thumbnail_url(model, item["id"]))
    stale = client.get(f"{unversioned}?v={THUMBNAIL_VERSION - 1}")
    bare = client.get(unversioned)

    assert "immutable" in current.headers["cache-control"]
    assert stale.headers["cache-control"] == "no-cache"
    assert bare.headers["cache-control"] == "no-cache"
    assert current.content == stale.content == bare.content


def test_the_legacy_items_thumbnail_answers_304_to_its_etag(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    legacy = paths.model_dir(model) / "thumbnail.png"
    legacy.write_bytes(_real_image((400, 300), "PNG"))

    first = client.get(_thumbnail_url(model, "thumbnail"))
    etag = first.headers["etag"]
    again = client.get(_thumbnail_url(model, "thumbnail"), headers={"If-None-Match": etag})

    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-cache"
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag

    legacy.write_bytes(_real_image((300, 400), "PNG"))
    replaced = client.get(_thumbnail_url(model, "thumbnail"), headers={"If-None-Match": etag})

    assert replaced.status_code == 200
    assert replaced.headers["etag"] != etag
    with Image.open(io.BytesIO(replaced.content)) as small:
        assert small.size == (144, 192)


def test_thumbnails_are_decoded_a_few_at_a_time(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _upload(client, model, _real_image((400, 300), "PNG")).json()["media"][0]
    lock = threading.Lock()
    running = 0
    most = 0

    def slow(path: Path) -> bytes | None:
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return None

    monkeypatch.setattr("scadbuddy.api.media._thumbnail_of", slow)
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(
            pool.map(lambda _: client.get(_thumbnail_url(model, item["id"])).status_code, range(8))
        )

    assert codes == [200] * 8
    assert 1 < most <= MAX_CONCURRENT_THUMBNAILS


def test_an_image_is_capped_because_it_is_committed(client: TestClient, model: str) -> None:
    response = _upload(client, model, PNG + b"\x00" * (10 * 1024 * 1024))

    assert response.status_code == 413, response.text
    assert _media(client, model) == []


def test_an_upload_that_is_not_multipart_is_refused(client: TestClient, model: str) -> None:
    response = client.post(f"/api/v1/models/{model}/media", content=PNG)

    assert response.status_code == 422, response.text


def test_the_legacy_item_is_not_cached_as_immutable(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """``thumbnail.png`` is replaced in place by a thumbnail PUT, unlike an item."""
    (paths.model_dir(model) / "thumbnail.png").write_bytes(PNG)

    served = client.get(f"/api/v1/models/{model}/media/thumbnail")

    assert served.headers["cache-control"] == "no-cache"


def test_an_unknown_item_is_a_404(client: TestClient, model: str) -> None:
    assert client.get(f"/api/v1/models/{model}/media/abcdefabcdef").status_code == 404
    assert client.delete(f"/api/v1/models/{model}/media/abcdefabcdef").status_code == 404
    assert (
        client.patch(f"/api/v1/models/{model}/media/abcdefabcdef", json={"caption": "x"})
    ).status_code == 404


def test_a_caption_is_edited(client: TestClient, model: str) -> None:
    item = _upload(client, model, PNG).json()["media"][0]

    response = client.patch(f"/api/v1/models/{model}/media/{item['id']}", json={"caption": "Top"})

    assert response.status_code == 200, response.text
    assert response.json()["media"][0]["caption"] == "Top"
    assert _media(client, model)[0]["caption"] == "Top"


def test_the_order_is_set_by_a_permutation(client: TestClient, model: str) -> None:
    ids = [_upload(client, model, payload).json()["media"][-1]["id"] for payload in (PNG, JPEG)]

    response = client.put(f"/api/v1/models/{model}/media/order", json={"ids": ids[::-1]})

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["media"]] == ids[::-1]


@pytest.mark.parametrize("change", ["drop", "duplicate", "unknown"])
def test_an_order_that_is_not_a_permutation_is_refused(
    client: TestClient, model: str, change: str
) -> None:
    ids = [_upload(client, model, payload).json()["media"][-1]["id"] for payload in (PNG, JPEG)]
    wrong = {
        "drop": ids[:1],
        "duplicate": [ids[0], ids[0]],
        "unknown": [ids[0], "abcdefabcdef"],
    }[change]

    response = client.put(f"/api/v1/models/{model}/media/order", json={"ids": wrong})

    assert response.status_code == 422, response.text
    assert [item["id"] for item in _media(client, model)] == ids


def test_a_delete_removes_the_file_and_the_entry(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    item = _upload(client, model, WEBM, poster=PNG).json()["media"][0]
    media_dir = paths.model_dir(model) / "media"
    assert sorted(path.name for path in media_dir.iterdir()) == sorted(
        [item["file"], item["poster"]]
    )

    response = client.delete(f"/api/v1/models/{model}/media/{item['id']}")

    assert response.status_code == 200, response.text
    assert response.json()["media"] == []
    assert list(media_dir.iterdir()) == []


def test_the_first_write_converts_a_legacy_thumbnail(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    (paths.model_dir(model) / "thumbnail.png").write_bytes(PNG)

    media = _upload(client, model, JPEG).json()["media"]

    assert [item["kind"] for item in media] == ["image", "image"]
    assert media[0]["id"] != "thumbnail"
    assert media[0]["file"] == f"{media[0]['id']}.png"
    assert not (paths.model_dir(model) / "thumbnail.png").exists()
    assert (paths.model_dir(model) / "media" / media[0]["file"]).read_bytes() == PNG


def test_the_legacy_id_can_be_reordered_and_removed(client: TestClient, model: str) -> None:
    thumbnail = {"file": ("t.png", PNG, "image/png")}
    assert client.put(f"/api/v1/models/{model}/thumbnail", files=thumbnail).status_code == 200
    added = _upload(client, model, JPEG).json()["media"]
    converted = added[0]["id"]

    reordered = client.put(
        f"/api/v1/models/{model}/media/order", json={"ids": [added[1]["id"], converted]}
    )
    assert reordered.status_code == 200, reordered.text
    assert client.delete(f"/api/v1/models/{model}/media/{converted}").status_code == 200
    assert [item["id"] for item in _media(client, model)] == [added[1]["id"]]


def test_a_legacy_item_is_deleted_by_its_legacy_id(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    (paths.model_dir(model) / "thumbnail.png").write_bytes(PNG)

    response = client.delete(f"/api/v1/models/{model}/media/thumbnail")

    assert response.status_code == 200, response.text
    assert response.json()["media"] == []
    assert response.json()["has_thumbnail"] is False
    assert not (paths.model_dir(model) / "thumbnail.png").exists()


def test_the_cover_follows_the_order(client: TestClient, model: str) -> None:
    first = _upload(client, model, PNG).json()["media"][0]
    second = _upload(client, model, JPEG).json()["media"][1]
    assert client.get(f"/api/v1/models/{model}/thumbnail").content == PNG

    client.put(f"/api/v1/models/{model}/media/order", json={"ids": [second["id"], first["id"]]})

    cover = client.get(f"/api/v1/models/{model}/thumbnail")
    assert cover.content == JPEG
    assert cover.headers["content-type"] == "image/jpeg"
    record = client.get(f"/api/v1/models/{model}").json()
    assert record["has_thumbnail"] is True
    assert record["thumbnail_source"] == "model"


def test_a_video_cover_shows_its_poster(client: TestClient, model: str) -> None:
    _upload(client, model, MP4, poster=WEBP)

    cover = client.get(f"/api/v1/models/{model}/thumbnail")

    assert cover.status_code == 200
    assert cover.content == WEBP
    assert cover.headers["content-type"] == "image/webp"


def test_a_video_with_no_poster_gives_way_to_the_next_image(client: TestClient, model: str) -> None:
    _upload(client, model, MP4)
    assert client.get(f"/api/v1/models/{model}/thumbnail").status_code == 404
    assert client.get(f"/api/v1/models/{model}").json()["has_thumbnail"] is False

    _upload(client, model, JPEG)

    assert client.get(f"/api/v1/models/{model}/thumbnail").content == JPEG


def test_setting_the_thumbnail_replaces_an_image_cover(client: TestClient, model: str) -> None:
    _upload(client, model, JPEG)
    video = _upload(client, model, WEBM).json()["media"][1]

    response = client.put(
        f"/api/v1/models/{model}/thumbnail", files={"file": ("t.png", PNG, "image/png")}
    )

    assert response.status_code == 200, response.text
    media = response.json()["media"]
    assert [item["kind"] for item in media] == ["image", "video"]
    assert media[1]["id"] == video["id"]
    assert client.get(f"/api/v1/models/{model}/thumbnail").content == PNG

    assert client.delete(f"/api/v1/models/{model}/thumbnail").status_code == 200
    assert [item["id"] for item in _media(client, model)] == [video["id"]]
    assert client.delete(f"/api/v1/models/{model}/thumbnail").status_code == 404


def test_a_video_whose_file_is_gone_is_reported_missing(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    item = _upload(client, model, MP4).json()["media"][0]
    (paths.model_dir(model) / "media" / item["file"]).unlink()

    [listed] = _media(client, model)

    assert listed["id"] == item["id"]
    assert listed["missing"] is True
    assert listed["size"] is None
    assert client.get(f"/api/v1/models/{model}/media/{item['id']}").status_code == 404
    removed = client.delete(f"/api/v1/models/{model}/media/{item['id']}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["media"] == []


def test_images_are_committed_and_videos_are_not(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    # The fixture's model was written straight to disk; a first write commits it.
    image = _upload(client, model, PNG).json()["media"][0]
    video = _upload(client, model, MP4, poster=JPEG).json()["media"][1]

    tracked = _tracked(paths)

    assert f"{model}/media/{image['file']}" in tracked
    assert f"{model}/media/{video['poster']}" in tracked
    assert f"{model}/media/{video['file']}" not in tracked
    # The list is rows in Postgres, not model.json.
    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    assert "media" not in meta


def test_a_restore_brings_back_a_file_but_not_its_row(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The list is not versioned: an image a restore puts back has no row, and a
    file with no row is ignored."""
    image = _upload(client, model, PNG).json()["media"][0]
    commit = client.get(f"/api/v1/models/{model}/versions").json()[0]["commit"]
    client.delete(f"/api/v1/models/{model}/media/{image['id']}")

    restored = client.post(f"/api/v1/models/{model}/versions/{commit}/restore")

    assert restored.status_code == 200, restored.text
    assert (paths.model_dir(model) / "media" / image["file"]).read_bytes() == PNG
    assert _media(client, model) == []


def test_a_built_ins_shipped_media_is_served_and_read_only(client: TestClient) -> None:
    [item] = _media(client, BUILTIN)
    assert (item["id"], item["readonly"]) == ("front", True)
    assert client.get(f"/api/v1/models/{BUILTIN}/media/front").content == PNG

    writes = [
        client.patch(f"/api/v1/models/{BUILTIN}/media/front", json={"caption": "x"}),
        client.delete(f"/api/v1/models/{BUILTIN}/media/front"),
    ]

    for response in writes:
        assert response.status_code == 403, response.text
        assert "built-in" in response.json()["detail"]
    assert _media(client, BUILTIN) == [item]


def test_media_is_added_to_a_built_in_without_moving_its_revision(
    client: TestClient, paths: DataPaths
) -> None:
    """#722: an overlay in the data directory, not a change to the image's mirror."""
    before = client.get(f"/api/v1/models/{BUILTIN}").json()
    tracked = _tracked(paths)

    response = _upload(client, BUILTIN, PNG, caption="On my keys")

    assert response.status_code == 200, response.text
    record = response.json()
    front, added = record["media"]
    assert (front["id"], front["readonly"]) == ("front", True)
    assert (added["caption"], added["readonly"]) == ("On my keys", False)
    assert record["version"] == before["version"]
    assert _tracked(paths) == tracked
    assert not (paths.model_dir(BUILTIN) / "media" / added["file"]).exists()
    assert (paths.builtin_media_dir(BUILTIN) / added["file"]).read_bytes() == PNG
    served = client.get(f"/api/v1/models/{BUILTIN}/media/{added['id']}")
    assert served.status_code == 200
    assert served.content == PNG

    video = _upload(client, BUILTIN, MP4, poster=JPEG).json()["media"][2]
    poster = client.get(f"/api/v1/models/{BUILTIN}/media/{video['id']}/poster")
    assert poster.content == JPEG

    caption = client.patch(
        f"/api/v1/models/{BUILTIN}/media/{added['id']}", json={"caption": "Keys"}
    )
    assert caption.status_code == 200, caption.text
    order = client.put(
        f"/api/v1/models/{BUILTIN}/media/order", json={"ids": [video["id"], added["id"]]}
    )
    assert order.status_code == 200, order.text
    assert [item["id"] for item in order.json()["media"]] == ["front", video["id"], added["id"]]
    removed = client.delete(f"/api/v1/models/{BUILTIN}/media/{video['id']}")
    assert removed.status_code == 200, removed.text
    assert [item["caption"] for item in removed.json()["media"]] == ["", "Keys"]
    assert client.get(f"/api/v1/models/{BUILTIN}").json()["version"] == before["version"]


def test_a_built_ins_cover_is_chosen_not_ordered(client: TestClient) -> None:
    added = _upload(client, BUILTIN, JPEG).json()["media"][1]

    response = client.put(f"/api/v1/models/{BUILTIN}/media/cover", json={"id": added["id"]})

    assert response.status_code == 200, response.text
    record = response.json()
    assert record["media_cover"] == added["id"]
    assert [item["id"] for item in record["media"]] == [added["id"], "front"]
    assert client.get(f"/api/v1/models/{BUILTIN}/thumbnail").content == JPEG
    listed = {model["slug"]: model for model in client.get("/api/v1/models").json()}
    assert listed[BUILTIN]["media_cover"] == added["id"]

    reset = client.put(f"/api/v1/models/{BUILTIN}/media/cover", json={"id": None})
    assert reset.json()["media_cover"] is None
    assert [item["id"] for item in reset.json()["media"]] == ["front", added["id"]]
    unknown = client.put(f"/api/v1/models/{BUILTIN}/media/cover", json={"id": "abcdefabcdef"})
    assert unknown.status_code == 404


def test_a_cover_of_mine_is_its_first_item(client: TestClient, model: str) -> None:
    first = _upload(client, model, PNG).json()["media"][0]
    second = _upload(client, model, JPEG).json()["media"][1]

    response = client.put(f"/api/v1/models/{model}/media/cover", json={"id": second["id"]})

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["media"]] == [second["id"], first["id"]]
    none = client.put(f"/api/v1/models/{model}/media/cover", json={"id": None})
    assert none.status_code == 422


def test_a_duplicate_of_a_built_in_takes_what_was_added_to_it(client: TestClient) -> None:
    added = _upload(client, BUILTIN, JPEG, caption="Mine").json()["media"][1]
    client.put(f"/api/v1/models/{BUILTIN}/media/cover", json={"id": added["id"]})

    response = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "Copy"})

    assert response.status_code == 201, response.text
    copy = response.json()
    assert [(item["id"], item["readonly"]) for item in copy["media"]] == [
        (added["id"], False),
        ("front", False),
    ]
    served = client.get(f"/api/v1/models/{copy['slug']}/media/{added['id']}")
    assert served.content == JPEG


def test_a_duplicate_copies_its_media_videos_included(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    image = _upload(client, model, PNG).json()["media"][0]
    video = _upload(client, model, MP4).json()["media"][1]

    response = client.post(f"/api/v1/models/{model}/duplicate", json={"name": "Copy"})

    assert response.status_code == 201, response.text
    copy = response.json()
    assert [item["id"] for item in copy["media"]] == [image["id"], video["id"]]
    assert not any(item["missing"] for item in copy["media"])
    copied = paths.model_dir(copy["slug"]) / "media"
    assert (copied / video["file"]).read_bytes() == MP4


def test_a_duplicate_of_a_built_in_copies_its_media(client: TestClient) -> None:
    response = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "Mine"})

    assert response.status_code == 201, response.text
    [item] = response.json()["media"]
    slug = response.json()["slug"]
    assert client.get(f"/api/v1/models/{slug}/media/{item['id']}").content == PNG


def test_a_bundled_entry_cannot_reach_outside_media(client: TestClient, paths: DataPaths) -> None:
    meta = json.loads(paths.model_meta(BUILTIN).read_text(encoding="utf-8"))
    meta["media"] = [
        {"id": "escape", "file": "../model.scad", "kind": "image"},
        {"id": "ok", "file": "ok.png", "kind": "image", "poster": "../../settings.json"},
    ]
    paths.model_meta(BUILTIN).write_text(json.dumps(meta), encoding="utf-8")

    listed = _media(client, BUILTIN)

    assert [item["id"] for item in listed] == []
    assert client.get(f"/api/v1/models/{BUILTIN}/media/escape").status_code == 404


def test_a_mine_model_json_media_entry_is_not_the_list(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Only a built-in's bundled model.json is read: a template of mine's is rows."""
    (paths.model_dir(model) / "media").mkdir()
    (paths.model_dir(model) / "media" / "front.png").write_bytes(PNG)
    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    meta["media"] = [{"id": "front", "file": "front.png", "kind": "image"}]
    paths.model_meta(model).write_text(json.dumps(meta), encoding="utf-8")

    assert _media(client, model) == []


# ── the upload gate ───────────────────────────────────────────────────────────


@pytest.fixture
def small_limit_client(
    data_dir: Path, seed_dir: Path, fake_openscad: str, model: str, pg_conninfo: str
) -> Iterator[TestClient]:
    settings = Settings(
        openscad=fake_openscad,
        data_dir=data_dir,
        seed_models_dir=seed_dir,
        frontend_dir=Path("/nonexistent"),
        media_upload_max_bytes=1024 * 1024,
        database_url=pg_conninfo,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_a_chunked_upload_over_the_limit_is_cut_off(
    small_limit_client: TestClient, model: str
) -> None:
    """No Content-Length, so the gate counts: it stops at the chunk that crosses the
    limit, having never read the rest. Driven at the ASGI level, since the test
    client reads a streamed body whole before the app sees any of it."""
    chunks = [
        b"--b\r\nContent-Disposition: form-data; name=file; filename=v.mp4\r\n\r\n",
        MP4,
        *[b"\x00" * (256 * 1024)] * 64,
        b"\r\n--b--\r\n",
    ]
    read = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal read
        if read == len(chunks):
            return {"type": "http.disconnect"}
        read += 1
        return {"type": "http.request", "body": chunks[read - 1], "more_body": read < len(chunks)}

    async def send(message: Message) -> None:
        sent.append(message)

    path = f"/api/v1/models/{model}/media"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"multipart/form-data; boundary=b"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    small_limit_client.portal.call(small_limit_client.app, scope, receive, send)  # type: ignore[union-attr]

    assert sent[0]["status"] == 413
    body = json.loads(b"".join(m.get("body", b"") for m in sent[1:]))
    assert "1 MB" in body["detail"]
    assert read < len(chunks), "the gate read the whole body before refusing it"
    assert _media(small_limit_client, model) == []
    assert not list((small_limit_client.app.state.scadbuddy.paths.cache).glob("media-upload-*"))  # type: ignore[attr-defined]


def test_a_declared_upload_over_the_limit_is_refused_on_its_headers(
    small_limit_client: TestClient, model: str
) -> None:
    response = _upload(small_limit_client, model, MP4 + b"\x00" * (1024 * 1024))

    assert response.status_code == 413, response.text
    assert "1 MB" in response.json()["detail"]


def test_the_media_limit_is_the_setting_not_the_multipart_cap(
    client: TestClient, model: str
) -> None:
    video = MP4 + b"\x00" * (MAX_MULTIPART_BODY_BYTES + 1024)

    response = _upload(client, model, video)

    assert response.status_code == 200, response.text
    assert response.json()["media"][0]["size"] == len(video)


def test_other_multipart_routes_keep_the_multipart_cap(
    small_limit_client: TestClient, model: str
) -> None:
    """The media route's own limit does not lift or lower anything else's."""
    oversized = PNG + b"\x00" * MAX_MULTIPART_BODY_BYTES

    response = small_limit_client.put(
        f"/api/v1/models/{model}/thumbnail", files={"file": ("t.png", oversized, "image/png")}
    )

    assert response.status_code == 413, response.text
    assert "reads at most" in response.json()["detail"]


def test_the_limit_comes_from_the_environment(
    data_dir: Path,
    seed_dir: Path,
    fake_openscad: str,
    model: str,
    pg_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES", "1024")
    settings = Settings(
        openscad=fake_openscad,
        data_dir=data_dir,
        seed_models_dir=seed_dir,
        frontend_dir=Path("/nonexistent"),
        database_url=pg_conninfo,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
    )
    with TestClient(create_app(settings)) as client:
        # Reported read-only, for the UI's own check before an upload.
        assert client.get("/api/v1/settings").json()["media_upload_max_bytes"] == 1024

        response = _upload(client, model, MP4 + b"\x00" * 2048)

    assert response.status_code == 413, response.text
    assert "SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES" in response.json()["detail"]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("media/order", {"ids": ["../escape"]}),
        ("media/cover", {"id": "../escape"}),
        ("media/cover", {"id": ""}),
    ],
)
def test_an_id_that_is_not_a_media_id_is_refused(
    client: TestClient, model: str, path: str, body: dict[str, object]
) -> None:
    response = client.put(f"/api/v1/models/{model}/{path}", json=body)

    assert response.status_code == 422
