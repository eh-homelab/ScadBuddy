"""#5475 -- an image for a template's README, written into its ``images/`` folder as one
revision by the ``model_image_add`` operation."""

from __future__ import annotations

import io
import uuid

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from scadbuddy.api.lsp import MAX_MODEL_IMAGE_BYTES
from scadbuddy.library.readme_images import MAX_README_IMAGES, stored_name
from tests.api.test_model_operations import _claims, _commits, _state, _workflow_ids
from tests.support.operations import press

SOURCE = "cube(10);\n"


def _image(fmt: str, colour: str = "red", size: tuple[int, int] = (4, 3)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, format=fmt)
    return out.getvalue()


def _model(client: TestClient, name: str) -> str:
    created = client.post("/api/v1/models", json={"name": name, "source": SOURCE}, headers=press())
    assert created.status_code == 201, created.text
    slug: str = created.json()["slug"]
    return slug


def _add(
    client: TestClient,
    slug: str,
    data: bytes,
    name: str = "photo.png",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/api/v1/models/{slug}/images",
        files={"file": (name, data, "application/octet-stream")},
        headers=headers or press(),
    )
    return response


@pytest.mark.parametrize(
    ("fmt", "upload", "stored"),
    [
        ("PNG", "photo.png", "images/photo.png"),
        ("JPEG", "photo.jpeg", "images/photo.jpg"),
        ("WEBP", "photo.webp", "images/photo.webp"),
        ("GIF", "photo.gif", "images/photo.gif"),
    ],
)
def test_an_image_is_stored_in_the_templates_images_folder_as_one_revision(
    client: TestClient, app: FastAPI, fmt: str, upload: str, stored: str
) -> None:
    slug = _model(client, f"Pictured {fmt}")
    data = _image(fmt)
    commits = _commits(app)

    answered = _add(client, slug, data, upload)

    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["path"] == stored
    assert body["model"]["slug"] == slug
    assert _commits(app) == commits + 1
    assert (_state(app).paths.model_dir(slug) / stored).read_bytes() == data
    # The path the README links is the one the image route serves.
    served = client.get(f"/api/v1/models/{slug}/images/{stored}")
    assert served.status_code == 200
    assert served.content == data
    assert _workflow_ids(app, "model_image_add")
    # The claim is dropped once the answer is final.
    assert _claims(app) == set()


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "not a PNG"),
        (b"just text, named like a picture", "not a PNG"),
        (b"\x89PNG\r\n\x1a\n" + b"\0" * 64, "not a readable PNG"),
    ],
)
def test_a_file_that_is_not_an_image_of_a_taken_type_is_refused(
    client: TestClient, app: FastAPI, data: bytes, why: str
) -> None:
    slug = _model(client, f"Refused {uuid.uuid4().hex[:6]}")
    commits = _commits(app)

    answered = _add(client, slug, data, "photo.png")

    assert answered.status_code == 422, answered.text
    assert why in answered.json()["detail"]
    assert _commits(app) == commits
    assert not (_state(app).paths.model_dir(slug) / "images").exists()
    assert _workflow_ids(app, "model_image_add") == []


def test_an_image_over_the_cap_is_refused(client: TestClient, app: FastAPI) -> None:
    slug = _model(client, "Too Big")
    data = _image("PNG") + b"\0" * MAX_MODEL_IMAGE_BYTES

    answered = _add(client, slug, data)

    assert answered.status_code == 422, answered.text
    assert "too large" in answered.json()["detail"]
    assert not (_state(app).paths.model_dir(slug) / "images").exists()


def test_the_name_loses_its_folders_and_takes_the_types_extension(
    client: TestClient, app: FastAPI
) -> None:
    slug = _model(client, "Named")

    answered = _add(client, slug, _image("JPEG"), "../../etc/Évil Name!!.PNG")

    assert answered.status_code == 200, answered.text
    assert answered.json()["path"] == "images/vil-name.jpg"
    assert (_state(app).paths.model_dir(slug) / "images" / "vil-name.jpg").is_file()


@pytest.mark.parametrize(
    ("upload", "stored"),
    [
        ("a/b\\c.png", "c.png"),
        (".hidden.png", "hidden.png"),
        ("....png", "image.png"),
        ("", "image.png"),
        (None, "image.png"),
        ("no extension", "no-extension.png"),
        ("x" * 200 + ".png", "x" * 64 + ".png"),
    ],
)
def test_a_stored_name_is_plain(upload: str | None, stored: str) -> None:
    assert stored_name(upload, "image/png") == stored


def test_a_taken_name_gets_a_number_and_nothing_is_written_over(
    client: TestClient, app: FastAPI
) -> None:
    slug = _model(client, "Collided")
    first, second, third = _image("PNG", "red"), _image("PNG", "blue"), _image("PNG", "green")

    paths = [_add(client, slug, data).json()["path"] for data in (first, second, third)]

    assert paths == ["images/photo.png", "images/photo-2.png", "images/photo-3.png"]
    folder = _state(app).paths.model_dir(slug) / "images"
    assert (folder / "photo.png").read_bytes() == first
    assert (folder / "photo-2.png").read_bytes() == second
    assert (folder / "photo-3.png").read_bytes() == third


def test_a_full_images_folder_is_refused(client: TestClient, app: FastAPI) -> None:
    slug = _model(client, "Full")
    folder = _state(app).paths.model_dir(slug) / "images"
    folder.mkdir()
    for index in range(MAX_README_IMAGES):
        (folder / f"{index}.png").write_bytes(b"x")

    answered = _add(client, slug, _image("PNG"))

    assert answered.status_code == 409, answered.text
    assert not (folder / "photo.png").exists()


def test_an_images_entry_that_is_a_link_is_refused(client: TestClient, app: FastAPI) -> None:
    slug = _model(client, "Linked")
    elsewhere = _state(app).paths.model_dir(slug).parent / f"elsewhere-{uuid.uuid4().hex}"
    elsewhere.mkdir()
    (_state(app).paths.model_dir(slug) / "images").symlink_to(elsewhere)

    answered = _add(client, slug, _image("PNG"))

    assert answered.status_code == 409, answered.text
    assert list(elsewhere.iterdir()) == []


def test_a_built_in_template_is_refused(client: TestClient, app: FastAPI) -> None:
    answered = _add(client, "builtin:keychain", _image("PNG"))

    assert answered.status_code == 403, answered.text
    assert "read-only" in answered.json()["detail"]
    assert _workflow_ids(app, "model_image_add") == []


def test_a_retry_with_the_same_key_writes_once(client: TestClient, app: FastAPI) -> None:
    slug = _model(client, "Retried")
    data = _image("PNG")
    key = {"Idempotency-Key": uuid.uuid4().hex}

    first = _add(client, slug, data, headers=key)
    assert first.status_code == 200, first.text
    commits = _commits(app)
    again = _add(client, slug, data, headers=key)

    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert _commits(app) == commits
    assert sorted(p.name for p in (_state(app).paths.model_dir(slug) / "images").iterdir()) == [
        "photo.png"
    ]
    assert len(_workflow_ids(app, "model_image_add")) == 1
