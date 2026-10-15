"""Renaming and moving library files, and making, changing and deleting folders
(#2686), through ScadBuddy's operations."""

import asyncio
import io
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.uploads import LibraryCopy
from tests.api.test_library_delete import API, files, queue
from tests.api.test_send import configure
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

OUTPUT = "b" * 32


def folders(*tree: dict[str, object]) -> None:
    respx.get(f"{API}/library/folders").mock(return_value=httpx.Response(200, json=list(tree)))


def folder_files(folder_id: int, *rows: tuple[int, bool]) -> None:
    """The folder's files on the delete's check; every later listing is empty, as it is
    once they are deleted."""
    listed = [
        {
            "id": i,
            "filename": f"file-{i}.3mf",
            "file_type": "3mf",
            "folder_id": folder_id,
            "is_external": ext,
        }
        for i, ext in rows
    ]
    calls = iter([listed])
    respx.get(f"{API}/library/files", params={"folder_id": folder_id}).mock(
        side_effect=lambda request: httpx.Response(200, json=next(calls, []))
    )


@respx.mock
def test_rename_and_move_send_only_what_was_given(client: TestClient) -> None:
    configure(client)
    files((7, False))
    put = respx.put(f"{API}/library/files/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "filename": "b.3mf", "folder_id": 3})
    )
    response = client.patch(
        "/api/v1/print/library/7", json={"filename": "b.3mf", "folder_id": 3}, headers=press()
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 7, "filename": "b.3mf", "folder_id": 3}
    assert json.loads(put.calls.last.request.content) == {"filename": "b.3mf", "folder_id": 3}


@respx.mock
def test_a_move_keeps_the_upload_rows_right(client: TestClient, app: FastAPI) -> None:
    """As ``/move`` does (#2670): an inbox copy moved out follows to its new folder, and
    a copy moved into the inbox stops being recorded, so no send deletes it."""
    configure(client)  # the inbox is folder 2
    state: AppState = getattr(app.state, STATE_ATTR)
    asyncio.run(state.uploads.record(OUTPUT, LibraryCopy(id=7, folder_id=2, target_key="k")))
    asyncio.run(state.uploads.record(OUTPUT, LibraryCopy(id=8, folder_id=3, target_key="j")))
    files((7, False), (8, False))
    respx.put(f"{API}/library/files/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "filename": "a.3mf", "folder_id": 3})
    )
    respx.put(f"{API}/library/files/8").mock(
        return_value=httpx.Response(200, json={"id": 8, "filename": "b.3mf", "folder_id": 2})
    )

    for file_id, folder_id in ((7, 3), (8, 2)):
        response = client.patch(
            f"/api/v1/print/library/{file_id}", json={"folder_id": folder_id}, headers=press()
        )
        assert response.status_code == 200, response.text

    copies = asyncio.run(state.uploads.for_output(OUTPUT))
    assert [(copy.id, copy.folder_id) for copy in copies] == [(7, 3)]


def test_a_filename_with_a_slash_is_refused(client: TestClient) -> None:
    configure(client)
    response = client.patch(
        "/api/v1/print/library/7", json={"filename": "a/b.3mf"}, headers=press()
    )
    assert response.status_code == 422


@respx.mock
def test_mkdir_and_folder_rename(client: TestClient) -> None:
    configure(client)
    made = respx.post(f"{API}/library/folders/").mock(
        return_value=httpx.Response(200, json={"id": 9, "name": "Parts", "parent_id": 2})
    )
    response = client.post(
        "/api/v1/print/library/folders", json={"name": "Parts", "parent_id": 2}, headers=press()
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 9, "name": "Parts", "parent_id": 2}
    assert json.loads(made.calls.last.request.content) == {"name": "Parts", "parent_id": 2}

    respx.put(f"{API}/library/folders/9").mock(
        return_value=httpx.Response(200, json={"id": 9, "name": "Bits", "parent_id": 2})
    )
    renamed = client.patch(
        "/api/v1/print/library/folders/9", json={"name": "Bits"}, headers=press()
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Bits"


class FakeLibrary:
    """Bambuddy 1.2.5.5's library as a folder delete meets it, cascade and all. Trashing
    a file only marks it, and ``DELETE /library/folders/{id}`` drops every file row,
    trashed or not, whose folder is in the subtree (``routes/library.py``
    ``delete_folder``); each such row is recorded in ``destroyed``. A trashed file
    cannot be moved (``update_file`` reads only active rows), and a file in
    ``not_owned`` is refused a move or a delete, as for a key with only ``*_own``. A
    trashed file in ``others`` is another user's: the trash lists it only while
    ``permissions`` holds ``library:delete_all`` (``list_trash``)."""

    def __init__(self) -> None:
        self.folders: dict[int, dict[str, Any]] = {}
        self.files: dict[int, dict[str, Any]] = {}
        self.not_owned: set[int] = set()
        self.others: set[int] = set()
        #: Files whose move Bambuddy refuses, with the status it answers.
        self.refuse_move: dict[int, int] = {}
        self.permissions = ["library:delete_all", "library:delete_own"]
        #: What ``GET /auth/me`` answers instead, when set.
        self.me: httpx.Response | None = None
        self.log: list[tuple[str, int]] = []
        self.destroyed: list[int] = []
        #: Runs once, at the first move: what someone else does after the check.
        self.on_move: Callable[[], None] | None = None
        self._mount()

    def folder(self, folder_id: int, parent_id: int | None = None) -> None:
        self.folders[folder_id] = {"name": f"folder-{folder_id}", "parent_id": parent_id}

    def file(
        self, file_id: int, folder_id: int | None, *, external: bool = False, trashed: bool = False
    ) -> None:
        self.files[file_id] = {"folder_id": folder_id, "external": external, "trashed": trashed}

    def live(self, file_id: int) -> bool:
        return file_id in self.files and not self.files[file_id]["trashed"]

    def _row(self, file_id: int) -> dict[str, Any]:
        found = self.files[file_id]
        return {
            "id": file_id,
            "filename": f"file-{file_id}.3mf",
            "file_type": "3mf",
            "folder_id": found["folder_id"],
            "is_external": found["external"],
        }

    def _tree(self, parent_id: int | None) -> list[dict[str, Any]]:
        return [
            {"id": i, "name": f["name"], "parent_id": parent_id, "children": self._tree(i)}
            for i, f in self.folders.items()
            if f["parent_id"] == parent_id
        ]

    def _subtree(self, folder_id: int) -> set[int]:
        found = {folder_id}
        for i, f in self.folders.items():
            if f["parent_id"] == folder_id:
                found |= self._subtree(i)
        return found

    def _delete(self, file_id: int) -> bool:
        external = self.files[file_id]["external"]
        if external:
            del self.files[file_id]
        else:
            self.files[file_id]["trashed"] = True
        self.log.append(("delete", file_id))
        return not external

    def _mount(self) -> None:
        def missing() -> httpx.Response:
            return httpx.Response(404, json={"detail": "File not found"})

        def read(request: httpx.Request, file_id: str) -> httpx.Response:
            return (
                httpx.Response(200, json=self._row(int(file_id)))
                if self.live(int(file_id))
                else missing()
            )

        def move(request: httpx.Request, file_id: str) -> httpx.Response:
            i = int(file_id)
            if not self.live(i):
                return missing()
            if i in self.not_owned:
                return httpx.Response(403, json={"detail": "You can only update your own files"})
            if i in self.refuse_move:
                return httpx.Response(self.refuse_move[i], json={"detail": "refused"})
            folder_id = json.loads(request.content)["folder_id"]
            self.files[i]["folder_id"] = None if folder_id == 0 else folder_id
            self.log.append(("move", i))
            if self.on_move is not None:
                then, self.on_move = self.on_move, None
                then()
            return httpx.Response(200, json=self._row(i))

        def delete_one(request: httpx.Request, file_id: str) -> httpx.Response:
            i = int(file_id)
            if not self.live(i):
                return missing()
            if i in self.not_owned:
                return httpx.Response(403, json={"detail": "You can only delete your own files"})
            return httpx.Response(200, json={"status": "success", "trashed": self._delete(i)})

        def bulk(request: httpx.Request) -> httpx.Response:
            ids = json.loads(request.content)["file_ids"]
            done = [i for i in ids if self.live(i) and i not in self.not_owned]
            for i in done:
                self._delete(i)
            return httpx.Response(200, json={"deleted_files": len(done)})

        def listing(request: httpx.Request) -> httpx.Response:
            folder_id = int(request.url.params["folder_id"])
            rows = [
                self._row(i)
                for i, f in self.files.items()
                if f["folder_id"] == folder_id and not f["trashed"]
            ]
            return httpx.Response(200, json=rows)

        def trash(request: httpx.Request) -> httpx.Response:
            items = [
                {**self._row(i), "file_size": 1, "deleted_at": "2026-10-13T00:00:00Z"}
                for i, f in self.files.items()
                if f["trashed"]
                and (i not in self.others or "library:delete_all" in self.permissions)
            ]
            return httpx.Response(
                200, json={"items": items, "total": len(items), "retention_days": 30}
            )

        def rmdir(request: httpx.Request, folder_id: str) -> httpx.Response:
            subtree = self._subtree(int(folder_id))
            for i in [i for i, f in self.files.items() if f["folder_id"] in subtree]:
                self.destroyed.append(i)
                del self.files[i]
            for i in subtree:
                del self.folders[i]
            self.log.append(("rmdir", int(folder_id)))
            return httpx.Response(200, json={"status": "success"})

        library = "/api/v1/library"
        respx.get(f"{API}/library/folders").mock(
            side_effect=lambda request: httpx.Response(200, json=self._tree(None))
        )
        respx.get(path__regex=rf"^{library}/files/(?P<file_id>\d+)$").mock(side_effect=read)
        respx.put(path__regex=rf"^{library}/files/(?P<file_id>\d+)$").mock(side_effect=move)
        respx.delete(path__regex=rf"^{library}/files/(?P<file_id>\d+)$").mock(
            side_effect=delete_one
        )
        respx.post(f"{API}/library/bulk-delete").mock(side_effect=bulk)
        respx.get(path=f"{library}/files").mock(side_effect=listing)
        respx.get(path=f"{library}/trash").mock(side_effect=trash)
        respx.delete(path__regex=rf"^{library}/folders/(?P<folder_id>\d+)$").mock(side_effect=rmdir)
        respx.get(f"{API}/auth/status").mock(
            return_value=httpx.Response(200, json={"auth_enabled": True, "requires_setup": False})
        )
        respx.get(f"{API}/auth/me").mock(
            side_effect=lambda request: (
                self.me or httpx.Response(200, json={"id": 3, "permissions": self.permissions})
            )
        )
        queue()

    def moved_before_deleted(self, file_id: int) -> bool:
        return self.log.index(("move", file_id)) < self.log.index(("delete", file_id))


def delete_folder(client: TestClient, folder_id: int) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/api/v1/print/library/folders/{folder_id}/delete", headers=press()
    )
    return response


@respx.mock
def test_folder_delete_takes_its_files_and_subfolders(client: TestClient) -> None:
    configure(client)
    bambuddy = FakeLibrary()
    bambuddy.folder(4)
    bambuddy.folder(5, parent_id=4)
    bambuddy.file(10, 4)
    bambuddy.file(11, 5, external=True)
    bambuddy.file(12, 5)
    bambuddy.file(30, None, trashed=True)  # elsewhere in the trash: no reason to refuse

    response = delete_folder(client, 4)

    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted((f["id"], f["trashed"]) for f in body["deleted"]) == [
        (10, True),
        (11, False),
        (12, True),
    ]
    assert body["skipped"] == []
    assert body["folders"] == [5, 4]
    # Nothing was left for the cascade: the managed files wait in the trash, at the top
    # level, where Undo restores them.
    assert bambuddy.destroyed == []
    assert bambuddy.moved_before_deleted(10) and bambuddy.moved_before_deleted(12)
    assert ("move", 11) not in bambuddy.log
    for file_id in (10, 12):
        assert bambuddy.files[file_id] == {"folder_id": None, "external": False, "trashed": True}
    assert 11 not in bambuddy.files


@respx.mock
def test_folder_delete_keeps_a_folder_whose_file_was_skipped(client: TestClient) -> None:
    configure(client)
    bambuddy = FakeLibrary()
    bambuddy.folder(4)
    bambuddy.folder(5, parent_id=4)
    bambuddy.file(10, 4)
    bambuddy.file(11, 5)
    bambuddy.not_owned.add(11)

    response = delete_folder(client, 4)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [f["id"] for f in body["deleted"]] == [10]
    assert [f["id"] for f in body["skipped"]] == [11]
    assert body["folders"] == []
    assert bambuddy.destroyed == []
    assert bambuddy.files[11] == {"folder_id": 5, "external": False, "trashed": False}
    assert not any(entry[0] == "rmdir" for entry in bambuddy.log)


@respx.mock
def test_a_move_refused_with_another_status_keeps_the_folder(client: TestClient) -> None:
    configure(client)
    bambuddy = FakeLibrary()
    bambuddy.folder(4)
    bambuddy.file(10, 4)
    bambuddy.file(11, 4)
    bambuddy.file(12, 4)
    bambuddy.refuse_move[11] = 422

    response = delete_folder(client, 4)

    assert response.status_code == 200, response.text
    body = response.json()
    # The others were moved to the top level and trashed there; the refused one stayed.
    assert sorted(f["id"] for f in body["deleted"]) == [10, 12]
    assert [f["id"] for f in body["skipped"]] == [11]
    assert "422" in body["skipped"][0]["reason"]
    assert "stayed in its folder" in body["skipped"][0]["reason"]
    assert body["folders"] == []
    assert bambuddy.moved_before_deleted(10) and bambuddy.moved_before_deleted(12)
    for file_id in (10, 12):
        assert bambuddy.files[file_id] == {"folder_id": None, "external": False, "trashed": True}
    assert bambuddy.files[11] == {"folder_id": 4, "external": False, "trashed": False}
    assert bambuddy.destroyed == []
    assert 4 in bambuddy.folders


@respx.mock
def test_folder_delete_refuses_when_the_trash_holds_its_files(client: TestClient) -> None:
    configure(client)
    bambuddy = FakeLibrary()
    bambuddy.folder(4)
    bambuddy.folder(5, parent_id=4)
    bambuddy.file(10, 4)
    bambuddy.file(20, 5, trashed=True)

    response = delete_folder(client, 4)

    assert response.status_code == 409, response.text
    assert "1 file in Bambuddy's trash" in response.json()["detail"]
    assert bambuddy.log == []
    assert bambuddy.files[10]["folder_id"] == 4


@respx.mock
def test_folder_delete_refuses_a_key_that_sees_only_its_own_trash(client: TestClient) -> None:
    """The listing leaves out another user's file trashed from the folder, so the trash
    check alone would pass and Bambuddy's cascade would empty that file for good."""
    configure(client)
    bambuddy = FakeLibrary()
    bambuddy.permissions = ["library:delete_own"]
    bambuddy.folder(4)
    bambuddy.file(10, 4)
    bambuddy.file(20, 4, trashed=True)
    bambuddy.others.add(20)

    response = delete_folder(client, 4)

    assert response.status_code == 403, response.text
    problem = response.json()
    assert problem["type"] == "https://scadbuddy.dev/problems/bambuddy-scope"
    assert problem["required_permission"] == "library:delete_all"
    assert "library:delete_all" in problem["detail"]
    assert bambuddy.log == []
    assert bambuddy.destroyed == []
    assert 20 in bambuddy.files and bambuddy.files[10]["folder_id"] == 4


@respx.mock
def test_folder_delete_is_refused_when_the_key_s_permissions_cannot_be_read(
    client: TestClient,
) -> None:
    configure(client)
    bambuddy = FakeLibrary()
    bambuddy.me = httpx.Response(500, json={"detail": "boom"})
    bambuddy.folder(4)
    bambuddy.file(10, 4)

    response = delete_folder(client, 4)

    assert response.status_code >= 500, response.text
    assert bambuddy.log == []
    assert bambuddy.files[10]["folder_id"] == 4


@respx.mock
def test_a_key_that_loses_delete_all_after_the_check_keeps_the_folder(
    client: TestClient,
) -> None:
    configure(client)
    bambuddy = FakeLibrary()
    bambuddy.folder(4)
    bambuddy.file(10, 4)

    def demote() -> None:
        bambuddy.permissions = ["library:delete_own"]
        bambuddy.file(20, 4, trashed=True)
        bambuddy.others.add(20)

    bambuddy.on_move = demote

    response = delete_folder(client, 4)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [f["id"] for f in body["deleted"]] == [10]
    assert [(f["id"], f["filename"]) for f in body["skipped"]] == [(4, "folder-4")]
    assert "library:delete_all" in body["skipped"][0]["reason"]
    assert body["folders"] == []
    assert not any(entry[0] == "rmdir" for entry in bambuddy.log)
    assert bambuddy.destroyed == [] and 20 in bambuddy.files


@respx.mock
def test_folder_delete_refuses_before_anything_when_a_print_waits(client: TestClient) -> None:
    configure(client)
    folders({"id": 4, "name": "Top", "children": []})
    folder_files(4, (10, False))
    files((10, False))
    queue({"id": 1, "status": "pending", "library_file_id": 10})
    moved = respx.put(f"{API}/library/files/10")
    bulk = respx.post(f"{API}/library/bulk-delete")
    gone = respx.delete(f"{API}/library/folders/4")

    response = client.post("/api/v1/print/library/folders/4/delete", headers=press())

    assert response.status_code == 409
    assert not moved.called and not bulk.called and not gone.called


@respx.mock
def test_an_unknown_folder_is_a_404(client: TestClient) -> None:
    configure(client)
    bambuddy = FakeLibrary()
    bambuddy.folder(4)

    response = delete_folder(client, 99)

    assert response.status_code == 404, response.text
    assert bambuddy.log == []


@respx.mock
def test_an_empty_folder_delete_drops_only_the_folder(client: TestClient) -> None:
    configure(client)
    bambuddy = FakeLibrary()
    bambuddy.folder(4)
    response = delete_folder(client, 4)
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": [], "skipped": [], "folders": [4]}
    assert bambuddy.log == [("rmdir", 4)]


@respx.mock
def test_a_file_added_after_the_check_keeps_its_folder(client: TestClient) -> None:
    configure(client)
    bambuddy = FakeLibrary()
    bambuddy.folder(4)
    bambuddy.file(10, 4)
    bambuddy.on_move = lambda: bambuddy.file(12, 4)

    response = delete_folder(client, 4)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [f["id"] for f in body["deleted"]] == [10]
    assert [f["id"] for f in body["skipped"]] == [12]
    assert body["folders"] == []
    assert bambuddy.destroyed == []
    assert bambuddy.live(12)


@respx.mock
def test_a_file_trashed_from_the_folder_after_the_check_keeps_it(client: TestClient) -> None:
    configure(client)
    bambuddy = FakeLibrary()
    bambuddy.folder(4)
    bambuddy.file(10, 4)
    bambuddy.on_move = lambda: bambuddy.file(14, 4, trashed=True)

    response = delete_folder(client, 4)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [f["id"] for f in body["skipped"]] == [14]
    assert "trash" in body["skipped"][0]["reason"]
    assert body["folders"] == []
    assert bambuddy.destroyed == []
    assert 14 in bambuddy.files


@respx.mock
def test_a_subfolder_made_after_the_check_keeps_its_ancestors(client: TestClient) -> None:
    configure(client)
    bambuddy = FakeLibrary()
    bambuddy.folder(4)
    bambuddy.folder(5, parent_id=4)
    bambuddy.file(10, 5)

    def mkdir() -> None:
        bambuddy.folder(6, parent_id=5)
        bambuddy.file(13, 6)

    bambuddy.on_move = mkdir

    response = delete_folder(client, 4)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [f["id"] for f in body["deleted"]] == [10]
    assert [(f["id"], f["filename"]) for f in body["skipped"]] == [(6, "folder-6")]
    assert "folder" in body["skipped"][0]["reason"]
    assert body["folders"] == []
    assert bambuddy.destroyed == []
    assert bambuddy.live(13) and {4, 5, 6} <= set(bambuddy.folders)


@respx.mock
def test_upload_into_a_folder_goes_by_claim(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(client)
    up = respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(200, json={"id": 50, "filename": "box.3mf", "folder_id": 3})
    )
    sent_as: list[object] = []
    upload = BambuddyClient.upload_library_file

    async def recording(self: BambuddyClient, filename: str, content: Any, **kw: Any) -> Any:
        sent_as.append(content)
        return await upload(self, filename, content, **kw)

    monkeypatch.setattr(BambuddyClient, "upload_library_file", recording)
    payload = b"PK\x03\x04" + b"\x00" * 3_000_000
    response = client.post(
        "/api/v1/print/library/files",
        files={"file": ("box.3mf", payload, "application/octet-stream")},
        data={"folder_id": "3"},
        headers=press(),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 50, "filename": "box.3mf", "folder_id": 3}
    sent = up.calls.last.request
    assert sent.url.params["folder_id"] == "3"
    assert payload in sent.content
    # Streamed from the claim's file, never read into memory whole.
    assert len(sent_as) == 1 and not isinstance(sent_as[0], bytes)
    assert isinstance(sent_as[0], io.BufferedReader) and sent_as[0].closed


@respx.mock
def test_an_upload_answers_the_folder_bambuddy_put_it_in(client: TestClient) -> None:
    configure(client)
    respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(200, json={"id": 51, "filename": "box.3mf", "folder_id": 8})
    )
    response = client.post(
        "/api/v1/print/library/files",
        files={"file": ("box.3mf", b"PK\x03\x04data", "application/octet-stream")},
        data={"folder_id": "3"},
        headers=press(),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 51, "filename": "box.3mf", "folder_id": 8}


@respx.mock
def test_an_upload_answers_the_folder_it_asked_for_when_bambuddy_names_none(
    client: TestClient,
) -> None:
    # Bambuddy's FileUploadResponse carries no folder_id (bambuddy/models.py LibraryFile).
    configure(client)
    respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(200, json={"id": 52, "filename": "box.3mf"})
    )
    response = client.post(
        "/api/v1/print/library/files",
        files={"file": ("box.3mf", b"PK\x03\x04data", "application/octet-stream")},
        data={"folder_id": "3"},
        headers=press(),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 52, "filename": "box.3mf", "folder_id": 3}


@respx.mock
def test_a_repeated_upload_uploads_once(client: TestClient) -> None:
    configure(client)
    up = respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(200, json={"id": 50, "filename": "box.3mf"})
    )
    key = press()
    for _ in range(2):
        response = client.post(
            "/api/v1/print/library/files",
            files={"file": ("box.3mf", b"PK\x03\x04data", "application/octet-stream")},
            headers=key,
        )
        assert response.status_code == 200, response.text
    assert up.call_count == 1


def test_an_upload_needs_a_named_file(client: TestClient) -> None:
    configure(client)
    response = client.post(
        "/api/v1/print/library/files",
        files={"file": ("", b"x", "application/octet-stream")},
        headers=press(),
    )
    assert response.status_code == 422
