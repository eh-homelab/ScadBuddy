"""#309 — photos and videos of the real finished object, added to a print: a photo onto
the archive in Bambuddy and a copy into its project's Media folder, a video into Media
only; ScadBuddy keeps which files they are and their captions (plan §2.5)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from functools import partial
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy import main
from scadbuddy.api import operations as operations_api
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.attachments.component import PRINT_ATTACHMENTS
from scadbuddy.bambuddy.attachments import MAX_ATTACHMENTS, Attachment
from scadbuddy.core.problems import ApiError
from scadbuddy.workflows.commands import start_command
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


def linked(client: TestClient, model: str, **archive: Any) -> str:
    """Print 35, linked to a new output of ``model``: the output's id."""
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    mock_archive(35, **archive)
    return output_id


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def _full_print(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Print 35 at the cap, after an upload's check saw it empty: the race at insert."""
    store = _state(client).components.get(PRINT_ATTACHMENTS)
    for n in range(MAX_ATTACHMENTS):
        asyncio.run(
            store.add(
                Attachment(
                    id=0,
                    archive_id=35,
                    kind="video",
                    library_file_id=1000 + n,
                    filename=f"{n}.webm",
                    size_bytes=1,
                )
            )
        )

    async def none_yet(archive_id: int) -> list[Attachment]:
        return []

    monkeypatch.setattr(store, "for_archive", none_yet)


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
    # The print keeps its attachment, and says where it went.
    detail = client.get("/api/v1/prints/35").json()
    assert detail["attachment_count"] == 1
    assert detail["media"]["attachments"][0]["promoted_to"] == [model]
    # A second copy is refused (review on #2832): a reload offered it again.
    again = client.post(f"{URL}/{attachment['id']}/promote", json={"slug": model}, headers=press())
    assert again.status_code == 409, again.text
    assert "gallery already" in again.json()["detail"]
    assert len(client.get(f"/api/v1/models/{model}").json()["media"]) == 1


@respx.mock
def test_a_promote_sent_again_with_its_key_is_its_first_answer(
    client: TestClient, model: str
) -> None:
    """#2831 review: a browser re-sending a promote whose answer it lost gets the record
    it made, not "already in the gallery"."""
    linked(client, model)
    Bambuddy(media_exists=True)
    attachment = attach(client, "p.png", PNG, "Real print").json()
    download = respx.get(f"{API}/library/files/501/download").mock(
        return_value=httpx.Response(200, content=PNG)
    )
    url = f"{URL}/{attachment['id']}/promote"
    key = press()

    first = client.post(url, json={"slug": model}, headers=key)
    again = client.post(url, json={"slug": model}, headers=key)

    assert first.status_code == 200, first.text
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    # Answered from the record: nothing read from Bambuddy, nothing copied again.
    assert download.call_count == 1
    assert len(client.get(f"/api/v1/models/{model}").json()["media"]) == 1


@respx.mock
def test_a_promote_answered_202_is_marked_once_it_lands(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2831 review: the mark is the operation's, so a promote past the answer deadline
    still says where it went, and a second press is refused, not a second copy."""
    linked(client, model)
    Bambuddy(media_exists=True)
    attachment = attach(client, "p.png", PNG, "Real print").json()

    respx.get(f"{API}/library/files/501/download").mock(
        return_value=httpx.Response(200, content=PNG)
    )
    # The copy outlives the answer deadline: a commit on a slow volume, say.
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    catalogue = state.catalogue
    add_media = catalogue.add_media

    def slowly(*args: Any, **kwargs: Any) -> Any:
        time.sleep(3)
        return add_media(*args, **kwargs)

    monkeypatch.setattr(catalogue, "add_media", slowly)
    monkeypatch.setattr(
        operations_api, "start_command", partial(start_command, deadline=timedelta(seconds=1))
    )
    url = f"{URL}/{attachment['id']}/promote"
    key = press()
    started = client.post(url, json={"slug": model}, headers=key)
    resend_until = time.monotonic() + 60
    while started.json().get("type") == operations_api.STILL_ACCEPTING_PROBLEM:
        assert time.monotonic() < resend_until, started.text
        started = client.post(url, json={"slug": model}, headers=key)
    assert started.status_code == 202, started.text

    deadline = time.monotonic() + 60
    while (op := client.get(f"/api/v1/operations/{started.json()['id']}").json())[
        "status"
    ] == "running":
        assert time.monotonic() < deadline, op
        time.sleep(0.2)
    assert op["status"] == "succeeded", op
    detail = client.get("/api/v1/prints/35").json()
    assert detail["media"]["attachments"][0]["promoted_to"] == [model]
    again = client.post(url, json={"slug": model}, headers=press())
    assert again.status_code == 409, again.text
    assert len(client.get(f"/api/v1/models/{model}").json()["media"]) == 1


@respx.mock
def test_a_photo_name_bambuddy_answers_that_could_leave_its_folder_is_not_kept(
    client: TestClient, model: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The name goes into the proxy's path to Bambuddy, so only Bambuddy's own shape of
    name is kept; anything else is refused before a row names it, and the photo left on
    the archive is logged with it (#2831 review)."""
    caplog.set_level(logging.WARNING, logger="scadbuddy.bambuddy.attachments")
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
    left = [r for r in caplog.records if "left on the archive" in r.getMessage()]
    assert [(r.archive_id, r.archive_photo) for r in left] == [  # type: ignore[attr-defined]
        (35, "../../library/files/1/download")
    ]


@respx.mock
def test_the_attachment_cap_holds_at_insert_and_the_late_upload_is_taken_off(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2831 review: two uploads with different keys can both pass the check; the insert
    refuses the one past the cap, and its files come off Bambuddy again."""
    linked(client, model)
    bambuddy = Bambuddy(media_exists=True)
    _full_print(client, monkeypatch)

    refused = attach(client, "p.png", PNG)

    assert refused.status_code == 409, refused.text
    assert f"at most {MAX_ATTACHMENTS}" in refused.json()["detail"]
    assert bambuddy.photo_delete.called and bambuddy.library_delete.called


# --- #3047: the attachments of archives deleted in Bambuddy ---------------------------


def _attach(state: AppState, archive_id: int) -> None:
    store = state.components.get(PRINT_ATTACHMENTS)
    asyncio.run(
        store.add(
            Attachment(
                id=0,
                archive_id=archive_id,
                kind="video",
                library_file_id=archive_id * 10,
                filename=f"{archive_id}.webm",
                size_bytes=1,
            )
        )
    )


@respx.mock
def test_the_sweep_forgets_the_attachments_of_an_archive_bambuddy_deleted(
    client: TestClient,
) -> None:
    configure(client)
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    _attach(state, 35)
    _attach(state, 36)
    mock_archive(35)
    respx.get(f"{API}/archives/36").mock(
        return_value=httpx.Response(404, json={"detail": "Archive not found"})
    )

    asyncio.run(main._sweep_print_attachments_logged(state))

    store = state.components.get(PRINT_ATTACHMENTS)
    assert [a.archive_id for a in asyncio.run(store.for_archive(35))] == [35]
    assert asyncio.run(store.for_archive(36)) == []


@pytest.mark.parametrize(
    "archive",
    [
        # A 404 that is not Bambuddy's "Archive not found": a proxy, or no Bambuddy
        # at that URL, would answer every archive so.
        httpx.Response(404, text="Not Found"),
        httpx.Response(404, json={"detail": "Not Found"}),
        # Bambuddy, failing: no answer about the archive is not "gone".
        httpx.Response(503),
    ],
)
@respx.mock
def test_the_sweep_forgets_nothing_without_bambuddys_own_404(
    client: TestClient, archive: httpx.Response
) -> None:
    configure(client)
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    _attach(state, 36)
    respx.get(f"{API}/archives/36").mock(return_value=archive)

    with pytest.raises(ApiError):
        asyncio.run(main._sweep_print_attachments_logged(state))

    store = state.components.get(PRINT_ATTACHMENTS)
    assert len(asyncio.run(store.for_archive(36))) == 1


@respx.mock
def test_a_late_upload_whose_cleanup_fails_is_still_a_409_and_names_what_it_left(
    client: TestClient,
    model: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#3402: the cleanup after a lost cap race is best-effort; its failure neither
    hides the 409 nor goes unlogged."""
    caplog.set_level(logging.WARNING, logger="scadbuddy.library.attachment_operations")
    linked(client, model)
    bambuddy = Bambuddy(media_exists=True)
    bambuddy.photo_delete.mock(return_value=httpx.Response(500, json={"detail": "down"}))
    _full_print(client, monkeypatch)

    refused = attach(client, "p.png", PNG)

    assert refused.status_code == 409, refused.text
    left = [r for r in caplog.records if "could not be taken off" in r.getMessage()]
    assert [(r.archive_id, r.archive_photo, r.library_file_id) for r in left] == [  # type: ignore[attr-defined]
        (35, "ab12cd34.png", 501)
    ]


@respx.mock
def test_a_blank_caption_is_stored_as_none_on_upload_too(client: TestClient, model: str) -> None:
    """#3402: an upload strips its caption as PATCH does."""
    linked(client, model)
    Bambuddy(media_exists=True)

    added = attach(client, "p.png", PNG, "   ")

    assert added.status_code == 201, added.text
    assert added.json()["caption"] is None


@respx.mock
def test_a_promote_whose_mark_fails_copies_nothing_and_can_be_sent_again(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3401: the mark goes first, so a promote that fails between mark and copy leaves no
    unmarked copy for a second promote to copy again."""
    linked(client, model)
    Bambuddy(media_exists=True)
    attachment = attach(client, "p.png", PNG, "Real print").json()
    respx.get(f"{API}/library/files/501/download").mock(
        return_value=httpx.Response(200, content=PNG)
    )
    store = _state(client).components.get(PRINT_ATTACHMENTS)
    promoted = store.promoted
    calls = 0

    async def fails_once(archive_id: int, attachment_id: int, slug: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("the database went away")
        await promoted(archive_id, attachment_id, slug)

    monkeypatch.setattr(store, "promoted", fails_once)
    url = f"{URL}/{attachment['id']}/promote"

    failed = client.post(url, json={"slug": model}, headers=press())
    assert failed.status_code >= 500, failed.text
    assert client.get(f"/api/v1/models/{model}").json()["media"] == []

    again = client.post(url, json={"slug": model}, headers=press())
    assert again.status_code == 200, again.text
    assert len(client.get(f"/api/v1/models/{model}").json()["media"]) == 1


@respx.mock
def test_a_promote_whose_copy_fails_is_not_left_marked(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3401: marked first, so a copy that does not land takes the mark back."""
    linked(client, model)
    Bambuddy(media_exists=True)
    attachment = attach(client, "p.png", PNG, "Real print").json()
    respx.get(f"{API}/library/files/501/download").mock(
        return_value=httpx.Response(200, content=PNG)
    )

    def no_room(*args: Any, **kwargs: Any) -> Any:
        raise OSError("no space left on device")

    monkeypatch.setattr(_state(client).catalogue, "add_media", no_room)

    failed = client.post(f"{URL}/{attachment['id']}/promote", json={"slug": model}, headers=press())

    assert failed.status_code >= 500, failed.text
    detail = client.get("/api/v1/prints/35").json()
    assert detail["media"]["attachments"][0]["promoted_to"] == []


@respx.mock
def test_a_promote_of_a_file_grown_past_the_upload_cap_is_refused_as_it_streams(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3401: the copy in Bambuddy's library can be replaced after the upload; the
    download stops at the cap rather than filling the cache volume."""
    linked(client, model)
    Bambuddy(media_exists=True)
    attachment = attach(client, "p.png", PNG, "Real print").json()
    respx.get(f"{API}/library/files/501/download").mock(
        return_value=httpx.Response(200, content=PNG + b"\x00" * 4096)
    )
    state = _state(client)
    monkeypatch.setattr(
        state, "settings", state.settings.model_copy(update={"media_upload_max_bytes": len(PNG)})
    )

    refused = client.post(
        f"{URL}/{attachment['id']}/promote", json={"slug": model}, headers=press()
    )

    assert refused.status_code == 413, refused.text
    assert "upload limit" in refused.json()["detail"]
    assert client.get(f"/api/v1/models/{model}").json()["media"] == []


@respx.mock
def test_a_print_no_link_names_any_more_loses_its_attachments(
    client: TestClient, model: str
) -> None:
    """#3047: deleting the last output linked to a print forgets the print's attachment
    rows with its link; Bambuddy's files stay."""
    output_id = linked(client, model)
    bambuddy = Bambuddy(media_exists=True)
    assert attach(client, "p.png", PNG).status_code == 201
    store = _state(client).components.get(PRINT_ATTACHMENTS)

    asyncio.run(_state(client).print_links.delete_outputs([output_id]))

    assert asyncio.run(store.for_archive(35)) == []
    assert not bambuddy.photo_delete.called and not bambuddy.library_delete.called
