"""#309 — photos and videos of the real finished object, added to a print: a photo onto
the archive in Bambuddy and a copy into its project's Media folder, a video into Media
only; ScadBuddy keeps which files they are and their captions (plan §2.5)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.conftest import PNG_BYTES
from tests.api.test_media import WEBM
from tests.api.test_print_history import link, mock_archive
from tests.api.test_send import API, configure, make_output
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

PNG = PNG_BYTES + b"\x00"
PROJECT_FOLDER = 10
MEDIA_FOLDER = 11
URL = "/api/v1/prints/35/attachments"


class Bambuddy:
    """What the attachments write to, and read back."""

    def __init__(self, *, media_exists: bool = False) -> None:
        folders: list[dict[str, Any]] = [
            {"id": PROJECT_FOLDER, "name": "3 keychains", "parent_id": None, "project_id": 1}
        ]
        if media_exists:
            folders.append(
                {"id": MEDIA_FOLDER, "name": "Media", "parent_id": PROJECT_FOLDER, "project_id": 1}
            )
        self.by_project = respx.get(f"{API}/library/folders/by-project/1").mock(
            return_value=httpx.Response(200, json=folders)
        )
        self.made = respx.post(f"{API}/library/folders/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": MEDIA_FOLDER,
                    "name": "Media",
                    "parent_id": PROJECT_FOLDER,
                    "project_id": 1,
                },
            )
        )
        self.photo = respx.post(f"{API}/archives/35/photos").mock(
            return_value=httpx.Response(
                200, json={"status": "ok", "filename": "ab12cd34.png", "photos": ["ab12cd34.png"]}
            )
        )
        self.library = respx.post(f"{API}/library/files").mock(
            return_value=httpx.Response(200, json={"id": 501, "filename": "x"})
        )
        self.photo_delete = respx.delete(f"{API}/archives/35/photos/ab12cd34.png").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        self.library_delete = respx.delete(f"{API}/library/files/501").mock(
            return_value=httpx.Response(200, json={"trashed": True})
        )


def linked(client: TestClient, model: str, **archive: Any) -> None:
    configure(client)
    link(client, make_output(client, model), 35)
    mock_archive(35, **archive)


def attach(client: TestClient, name: str, data: bytes, caption: str = "") -> httpx.Response:
    response: httpx.Response = client.post(
        URL,
        files={"file": (name, data, "application/octet-stream")},
        data={"caption": caption},
        headers=press(),
    )
    return response


@respx.mock
def test_a_photo_goes_onto_the_print_and_into_a_new_media_folder(
    client: TestClient, model: str
) -> None:
    linked(client, model)
    bambuddy = Bambuddy()

    added = attach(client, "my desk photo!.jpeg", PNG, "On the desk")

    assert added.status_code == 201, added.text
    body = added.json()
    assert body["kind"] == "photo"
    # The bytes say PNG, whatever the name said.
    assert body["filename"] == "my-desk-photo.png"
    assert body["caption"] == "On the desk"
    assert body["size_bytes"] == len(PNG)
    assert body["url"] == f"{URL}/{body['id']}/file"
    assert bambuddy.photo.call_count == 1
    # Media/ was made under the project's folder, linked to the project.
    assert json.loads(bambuddy.made.calls.last.request.content) == {
        "name": "Media",
        "parent_id": PROJECT_FOLDER,
        "project_id": 1,
    }
    upload = bambuddy.library.calls.last.request
    assert upload.url.params["folder_id"] == str(MEDIA_FOLDER)
    assert PNG in upload.content

    detail = client.get("/api/v1/prints/35").json()
    assert detail["attachment_count"] == 1
    assert [each["caption"] for each in detail["media"]["attachments"]] == ["On the desk"]
    listed = client.get("/api/v1/prints").json()["items"]
    assert listed[0]["attachment_count"] == 1


@respx.mock
def test_a_video_goes_into_the_existing_media_folder_only(client: TestClient, model: str) -> None:
    linked(client, model)
    bambuddy = Bambuddy(media_exists=True)

    added = attach(client, "spin.webm", WEBM)

    assert added.status_code == 201, added.text
    assert added.json()["kind"] == "video"
    assert added.json()["filename"] == "spin.webm"
    assert not bambuddy.photo.called, "the archive takes images only"
    assert not bambuddy.made.called
    assert bambuddy.library.calls.last.request.url.params["folder_id"] == str(MEDIA_FOLDER)


@respx.mock
def test_a_print_with_no_project_attaches_into_the_inbox(client: TestClient, model: str) -> None:
    linked(client, model, project_id=None)
    bambuddy = Bambuddy()

    assert attach(client, "spin.webm", WEBM).status_code == 201

    assert not bambuddy.by_project.called
    # `configure` sets the inbox folder.
    inbox = client.get("/api/v1/settings").json()["library_folder_id"]
    assert bambuddy.library.calls.last.request.url.params["folder_id"] == str(inbox)


@respx.mock
def test_a_photo_whose_media_copy_fails_is_taken_off_the_print(
    client: TestClient, model: str
) -> None:
    linked(client, model)
    bambuddy = Bambuddy(media_exists=True)
    bambuddy.library.mock(return_value=httpx.Response(500, json={"detail": "disk full"}))

    failed = attach(client, "p.png", PNG)

    assert failed.status_code >= 400
    assert bambuddy.photo_delete.called
    assert client.get("/api/v1/prints/35").json()["attachment_count"] == 0


@respx.mock
@pytest.mark.parametrize("step", ["by_project", "made"])
def test_a_photo_is_not_posted_when_its_media_folder_cannot_be_had(
    client: TestClient, model: str, step: str
) -> None:
    """Review on #2831: a key without Manage Library, or Bambuddy failing to make
    ``Media/``, must not leave a photo on the archive that no row names."""
    linked(client, model)
    bambuddy = Bambuddy()
    getattr(bambuddy, step).mock(return_value=httpx.Response(403, json={"detail": "no"}))

    failed = attach(client, "p.png", PNG)

    assert failed.status_code >= 400
    assert not bambuddy.photo.called and not bambuddy.library.called
    assert client.get("/api/v1/prints/35").json()["attachment_count"] == 0


@respx.mock
def test_what_is_not_a_photo_or_video_is_refused_before_bambuddy(
    client: TestClient, model: str
) -> None:
    linked(client, model)
    bambuddy = Bambuddy()

    refused = attach(client, "notes.txt", b"hello there, not media")

    assert refused.status_code == 415
    assert not bambuddy.photo.called and not bambuddy.library.called


@respx.mock
def test_an_upload_over_the_settings_limit_is_refused_naming_it(
    client: TestClient, model: str
) -> None:
    linked(client, model)
    Bambuddy()
    saved = client.put("/api/v1/settings", json={"media_upload_max_bytes": 1000})
    assert saved.status_code == 200, saved.text

    refused = attach(client, "big.webm", WEBM + b"\x00" * 4000)

    assert refused.status_code == 413
    assert "a print's photo or video is at most" in refused.json()["detail"]
    assert "SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES" in refused.json()["detail"]


@respx.mock
def test_an_attachment_is_captioned_served_and_removed(client: TestClient, model: str) -> None:
    linked(client, model)
    bambuddy = Bambuddy(media_exists=True)
    attachment = attach(client, "p.png", PNG).json()
    served = respx.get(f"{API}/library/files/501/download").mock(
        return_value=httpx.Response(
            200,
            content=PNG,
            headers={
                "content-type": "application/octet-stream",
                "content-disposition": "attachment",
            },
        )
    )

    captioned = client.patch(f"{URL}/{attachment['id']}", json={"caption": " Side view "})
    assert captioned.json()["caption"] == "Side view"

    file = client.get(attachment["url"])
    assert file.status_code == 200
    assert file.content == PNG
    # Inline, typed by what it is, for an <img>.
    assert file.headers["content-type"] == "image/png"
    assert "content-disposition" not in file.headers
    assert served.called

    # Another print of ScadBuddy's cannot reach it by its id.
    other = make_output(client, model)
    link(client, other, 36)
    assert client.get(f"/api/v1/prints/36/attachments/{attachment['id']}/file").status_code == 404

    removed = client.delete(f"{URL}/{attachment['id']}", headers=press())
    assert removed.status_code == 204, removed.text
    assert bambuddy.photo_delete.called and bambuddy.library_delete.called
    assert client.get(attachment["url"]).status_code == 404
    assert client.get("/api/v1/prints/35").json()["media"]["attachments"] == []


@respx.mock
def test_the_photo_is_listed_once_as_an_attachment_not_also_as_an_archive_photo(
    client: TestClient, model: str
) -> None:
    linked(client, model)
    Bambuddy(media_exists=True)
    attach(client, "p.png", PNG)
    mock_archive(35, photos=["finish_20260927_015703_93372185.jpg", "ab12cd34.png"])

    media = client.get("/api/v1/prints/35").json()["media"]

    assert media["photos"] == []
    assert len(media["attachments"]) == 1


def test_another_print_s_attachment_or_a_print_not_scadbuddy_s_is_a_404(
    client: TestClient,
) -> None:
    assert client.get(f"{URL}/1/file").status_code == 404
    assert client.patch(f"{URL}/1", json={"caption": "x"}).status_code == 404
    assert attach(client, "p.png", PNG).status_code == 404


@respx.mock
def test_promote_copies_an_attachment_into_the_template_gallery(
    client: TestClient, model: str
) -> None:
    linked(client, model)
    Bambuddy(media_exists=True)
    attachment = attach(client, "p.png", PNG, "Real print").json()
    respx.get(f"{API}/library/files/501/download").mock(
        return_value=httpx.Response(200, content=PNG)
    )

    promoted = client.post(
        f"{URL}/{attachment['id']}/promote", json={"slug": model}, headers=press()
    )

    assert promoted.status_code == 200, promoted.text
    media = promoted.json()["media"]
    assert [item["caption"] for item in media] == ["Real print"]
    gallery = client.get(f"/api/v1/models/{model}/media/{media[0]['id']}")
    assert gallery.content == PNG
    # The print keeps its attachment.
    assert client.get("/api/v1/prints/35").json()["attachment_count"] == 1


@respx.mock
def test_a_photo_name_bambuddy_answers_that_could_leave_its_folder_is_not_kept(
    client: TestClient, model: str
) -> None:
    """The name goes into the proxy's path to Bambuddy, so only Bambuddy's own shape of
    name is kept; anything else is refused before a row names it."""
    linked(client, model)
    bambuddy = Bambuddy(media_exists=True)
    bambuddy.photo.mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "filename": "../../library/files/1/download", "photos": []}
        )
    )

    refused = attach(client, "p.png", PNG)

    assert refused.status_code == 502, refused.text
    assert not bambuddy.library.called
    assert client.get("/api/v1/prints/35").json()["attachment_count"] == 0
