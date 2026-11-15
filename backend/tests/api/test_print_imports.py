"""#4698 — Import print: adopt a Bambuddy archive ScadBuddy did not start, choose where
its 3MF comes from, and link it to a model. Bambuddy is mocked with respx; its shapes are
the recordings, and the printer's file list is ``routes/printers.py`` lines 1604-1629 at
maziggy/bambuddy@f9080d1."""

from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy import print_links
from scadbuddy.bambuddy.print_links import PrintLink, PrintSend
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.bambuddy.uploads import LibraryCopy, SlicedCopy
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.print_library.store import NativeFile
from scadbuddy.print_watch.component import AUTO_IMPORTS
from scadbuddy.print_watch.store import AutoImport
from tests.api.test_library_native import _native, _native_settings, _three_mf, _upload
from tests.api.test_print_history import NATIVE_HASH, link, mock_archive, native_slice, state
from tests.api.test_send import API, configure, make_output
from tests.bambuddy.conftest import recording
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

ARCHIVE = 193
HASH = "c0ffee" * 10 + "abcd"
FILE = "demo_elan.gcode.3mf"


def a_3mf() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
    return buffer.getvalue()


def bambuddy(
    *,
    library: list[dict[str, Any]] | None = None,
    printer_files: dict[str, Any] | None = None,
    **fields: Any,
) -> None:
    """Archive 193, a print of ``demo`` started outside ScadBuddy, still printing;
    Bambuddy's library (``library``), and the printer's ``/cache`` (``printer_files``)."""
    defaults: dict[str, Any] = {
        "filename": FILE,
        "print_name": "demo_elan",
        "content_hash": HASH,
        "file_size": 4242,
        "status": "printing",
        "source_3mf_path": None,
    }
    mock_archive(ARCHIVE, **{**defaults, **fields})
    respx.get(f"{API}/library/files/", params={"include_root": "false"}).mock(
        return_value=httpx.Response(200, json=library or [])
    )
    for row in library or []:
        respx.get(f"{API}/library/files/{row['id']}").mock(
            return_value=httpx.Response(
                200,
                json={"id": row["id"], "filename": row["filename"], "file_hash": row["hash"]},
            )
        )
    respx.get(f"{API}/printers/1/files", params={"path": "/cache"}).mock(
        return_value=httpx.Response(
            200, json=printer_files or {"path": "/cache", "files": [], "warnings": []}
        )
    )
    respx.get(f"{API}/printers/1/files", params={"path": "/"}).mock(
        return_value=httpx.Response(200, json={"path": "/", "files": [], "warnings": []})
    )


def library_row(file_id: int, filename: str, file_hash: str, **fields: Any) -> dict[str, Any]:
    row = {**recording("library-files-root.json")[0], "id": file_id, "filename": filename}
    return {**row, "hash": file_hash, **fields}


def sources(client: TestClient, **query: Any) -> dict[str, Any]:
    response = client.get(f"/api/v1/print-imports/{ARCHIVE}/sources", params=query)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def by_kind(body: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for source in body["sources"]:
        found.setdefault(source["kind"], []).append(source)
    return found


# --- the sources --------------------------------------------------------------------


@respx.mock
def test_every_source_is_listed_and_the_printer_only_when_asked(
    client: TestClient, model: str
) -> None:
    configure(client)
    bambuddy(
        library=[
            library_row(7, "demo_elan.gcode.3mf", HASH),
            library_row(8, "demo_elan.3mf", "other", file_type="3mf"),
            library_row(9, "unrelated.3mf", HASH),
        ]
    )
    body = sources(client)

    assert (body["status"], body["linked"], body["imported"]) == ("printing", None, None)
    assert body["suggested_models"] == [{"slug": model, "name": "Demo"}]
    kinds = by_kind(body)
    [archive] = kinds["archive"]
    assert archive | {"modified_at": None} == {
        "kind": "archive",
        "name": FILE,
        "size": 4242,
        "modified_at": None,
        "available": True,
        "reason": None,
        "sliced": True,
        "sha256": HASH,
        "library_file_id": None,
        "path": None,
        "matched_by": None,
        "preselected": False,
    }
    [project] = kinds["archive_source"]
    assert project["available"] is False and "no slicer project" in project["reason"]
    assert [(row["library_file_id"], row["matched_by"]) for row in kinds["library"]] == [
        (7, "hash"),
        (8, "name"),
    ]
    [on_printer] = kinds["printer"]
    assert on_printer["available"] is False and "not checked" in on_printer["reason"]
    assert not [call for call in respx.calls if "/printers/" in call.request.url.path]


@respx.mock
def test_the_printer_is_read_when_asked(client: TestClient, model: str) -> None:
    configure(client)
    bambuddy(
        printer_files={
            "path": "/cache",
            "files": [
                {"name": FILE, "path": f"/cache/{FILE}", "is_directory": False, "size": 4242},
                {"name": "other.gcode.3mf", "path": "/cache/other.gcode.3mf", "size": 1},
            ],
            "warnings": [],
        }
    )

    [on_printer] = by_kind(sources(client, printer="true"))["printer"]

    assert on_printer["available"] is True
    assert (on_printer["path"], on_printer["size"], on_printer["sliced"]) == (
        f"/cache/{FILE}",
        4242,
        True,
    )


@respx.mock
def test_an_offline_printer_says_why_and_hides_nothing_else(client: TestClient, model: str) -> None:
    configure(client)
    bambuddy(printer_files={"path": "/cache", "files": [], "warnings": ["printer_unavailable"]})

    body = by_kind(sources(client, printer="true"))

    [on_printer] = body["printer"]
    assert on_printer == on_printer | {
        "available": False,
        "reason": "the printer is offline or did not answer",
    }
    [archive] = body["archive"]
    assert archive["available"] is True and archive["preselected"] is True


@respx.mock
def test_an_archive_without_its_3mf_says_so(client: TestClient, model: str) -> None:
    configure(client)
    bambuddy(extra_data={"no_3mf_available": True, "no_3mf_reason": "FTPS served nothing"})

    [archive] = by_kind(sources(client))["archive"]

    assert (archive["available"], archive["reason"]) == (False, "FTPS served nothing")


@respx.mock
def test_an_archive_bambuddy_lacks_is_a_404(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/archives/{ARCHIVE}").mock(
        return_value=httpx.Response(404, json={"detail": "Archive not found"})
    )

    response = client.get(f"/api/v1/print-imports/{ARCHIVE}/sources")

    assert response.status_code == 404


# --- the import ----------------------------------------------------------------------


def import_print(client: TestClient, body: dict[str, Any], **headers: str) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/api/v1/print-imports/{ARCHIVE}", json=body, headers=headers or None
    )
    return response


@respx.mock
def test_an_import_onto_a_model_makes_it_a_scadbuddy_print(client: TestClient, model: str) -> None:
    configure(client)
    bambuddy()
    download = respx.get(f"{API}/archives/{ARCHIVE}/download")
    assert client.get(f"/api/v1/prints/{ARCHIVE}").status_code == 404

    response = import_print(client, {"source": {"kind": "archive"}, "slug": model})

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["slug"], body["print_url"], body["created_model"]) == (
        model,
        f"/api/v1/prints/{ARCHIVE}",
        False,
    )
    assert body["imported"] | {"imported_at": None} == {
        "archive_id": ARCHIVE,
        "subject": f"model:{model}",
        "slug": model,
        "source": "archive",
        "library_file_id": None,
        "printer_id": None,
        "printer_path": None,
        "filename": FILE,
        "size": 4242,
        "sha256": HASH,
        "created_model": False,
        "content_hash": HASH,
        "imported_at": None,
    }
    assert not download.called, "Bambuddy keeps the archive's file: nothing is copied"

    detail = client.get(f"/api/v1/prints/{ARCHIVE}")
    assert detail.status_code == 200, detail.text
    record = detail.json()
    assert (record["slug"], record["output_id"], record["provenance"]) == (model, None, None)
    assert record["status"] == "printing", "a print still printing settles from Bambuddy"
    assert record["imported"]["source"] == "archive"
    assert record["links"]["customize_url"] == f"/m/{model}"
    listed = client.get("/api/v1/prints", params={"slug": model}).json()["items"]
    assert [item["archive_id"] for item in listed] == [ARCHIVE]
    # The media proxy takes it now, as `workspace_save` print_image reads it.
    respx.get(f"{API}/archives/{ARCHIVE}/thumbnail").mock(
        return_value=httpx.Response(200, content=b"png", headers={"content-type": "image/png"})
    )
    assert client.get(f"/api/v1/prints/{ARCHIVE}/thumbnail").status_code == 200


@respx.mock
def test_importing_again_is_idempotent_and_another_model_is_refused(
    client: TestClient, model: str
) -> None:
    configure(client)
    bambuddy()
    assert import_print(client, {"source": {"kind": "archive"}, "slug": model}).status_code == 201

    again = import_print(client, {"source": {"kind": "archive"}, "slug": model})
    elsewhere = import_print(client, {"source": {"kind": "archive"}, "slug": "other"})

    assert again.status_code == 200
    assert again.json()["imported"]["source"] == "archive"
    assert elsewhere.status_code == 409
    assert elsewhere.json()["detail"] == f"archive {ARCHIVE} is a print of model {model!r} already"


@respx.mock
def test_a_print_scadbuddy_started_is_not_imported(client: TestClient, model: str) -> None:
    configure(client)
    bambuddy()
    output_id = make_output(client, model)
    link(client, output_id, ARCHIVE)

    response = import_print(client, {"source": {"kind": "archive"}, "slug": model})

    assert response.status_code == 409
    assert response.json()["owner"]["output_id"] == output_id


@respx.mock
def test_a_model_that_does_not_exist_is_a_404(client: TestClient) -> None:
    configure(client)
    bambuddy()

    response = import_print(client, {"source": {"kind": "archive"}, "slug": "nope"})

    assert response.status_code == 404


@respx.mock
def test_a_library_source_is_recorded_by_its_file(client: TestClient, model: str) -> None:
    configure(client)
    bambuddy(library=[library_row(7, "demo_elan.gcode.3mf", HASH)])

    response = import_print(
        client, {"source": {"kind": "library", "library_file_id": 7}, "slug": model}
    )

    assert response.status_code == 201, response.text
    imported = response.json()["imported"]
    assert (imported["source"], imported["library_file_id"], imported["sha256"]) == (
        "library",
        7,
        HASH,
    )


@respx.mock
def test_only_a_listed_source_is_read(client: TestClient, model: str) -> None:
    configure(client)
    bambuddy(library=[library_row(7, "demo_elan.gcode.3mf", HASH)])
    any_file = respx.get(f"{API}/library/files/99/download")
    any_path = respx.get(f"{API}/printers/1/files/download")

    library = import_print(
        client, {"source": {"kind": "library", "library_file_id": 99}, "slug": model}
    )
    printer = import_print(
        client, {"source": {"kind": "printer", "path": "/etc/secret.3mf"}, "slug": model}
    )
    traversal = import_print(
        client, {"source": {"kind": "printer", "path": "/cache/../x.txt"}, "slug": model}
    )

    assert (library.status_code, printer.status_code, traversal.status_code) == (422, 422, 422)
    assert not any_file.called and not any_path.called


@respx.mock
def test_a_printer_file_is_kept_in_the_library_inbox(client: TestClient, model: str) -> None:
    configure(client)
    bambuddy(
        printer_files={
            "path": "/cache",
            "files": [{"name": FILE, "path": f"/cache/{FILE}", "size": 4242}],
            "warnings": [],
        }
    )
    data = a_3mf()
    download = respx.get(
        f"{API}/printers/1/files/download", params={"path": f"/cache/{FILE}"}
    ).mock(return_value=httpx.Response(200, content=data))
    respx.get(f"{API}/library/files", params={"folder_id": "2"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    upload = respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(200, json={"id": 55, "filename": FILE})
    )

    response = import_print(
        client, {"source": {"kind": "printer", "path": f"/cache/{FILE}"}, "slug": model}
    )

    assert response.status_code == 201, response.text
    assert download.called
    assert upload.calls.last.request.url.params["folder_id"] == "2"
    imported = response.json()["imported"]
    assert (imported["source"], imported["library_file_id"], imported["printer_path"]) == (
        "printer",
        55,
        f"/cache/{FILE}",
    )
    assert imported["printer_id"] == 1
    assert imported["size"] == len(data)


@respx.mock
def test_a_new_model_needs_a_key_and_an_unsliced_file(client: TestClient, model: str) -> None:
    configure(client)
    bambuddy()
    body = {"source": {"kind": "archive"}, "new_model": {"name": "Trophy"}}

    without_key = import_print(client, body)
    sliced = import_print(client, body, **press())

    assert without_key.status_code == 428
    assert sliced.status_code == 422
    assert "sliced file" in sliced.json()["detail"]


@pytest.mark.requires_git
@respx.mock
def test_a_new_model_is_made_from_the_slicer_project(client: TestClient, model: str) -> None:
    from tests.api.test_mesh_models import painted_3mf

    configure(client)
    bambuddy(source_3mf_path="archive/x/source/demo_elan.3mf")
    respx.get(f"{API}/archives/{ARCHIVE}/source").mock(
        return_value=httpx.Response(200, content=painted_3mf())
    )
    body = {"source": {"kind": "archive_source"}, "new_model": {"name": "Trophy"}}
    key = press()

    response = import_print(client, body, **key)

    assert response.status_code == 201, response.text
    made = response.json()
    assert (made["slug"], made["created_model"], made["made_now"]) == ("trophy", True, True)
    assert made["imported"]["source"] == "archive_source"
    assert client.get("/api/v1/models/trophy/scene").status_code == 200
    assert client.get(f"/api/v1/prints/{ARCHIVE}").json()["slug"] == "trophy"
    again = import_print(client, body, **key)
    assert (again.status_code, again.json()["slug"]) == (200, "trophy")
    assert (again.json()["created_model"], again.json()["made_now"]) == (True, False)


@respx.mock
def test_slug_and_new_model_are_exclusive(client: TestClient, model: str) -> None:
    configure(client)

    both = import_print(
        client,
        {"source": {"kind": "archive"}, "slug": model, "new_model": {"name": "Trophy"}},
    )
    neither = import_print(client, {"source": {"kind": "archive"}})

    assert (both.status_code, neither.status_code) == (422, 422)


# --- the archives list -----------------------------------------------------------------


@respx.mock
def test_the_archives_say_which_are_scadbuddy_prints(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    respx.get(f"{API}/archives/").mock(
        return_value=httpx.Response(
            200,
            json=[
                {**recording("archive-detail.json"), "id": 35},
                {**recording("archive-detail.json"), "id": 36},
            ],
        )
    )

    rows = client.get("/api/v1/farm/archives").json()

    assert [(row["id"], row["scadbuddy_print"]) for row in rows] == [(35, True), (36, False)]


# --- a print ScadBuddy made, not linked yet (review of #5951) -------------------------


def sliced_output(client: TestClient, model: str, file_hash: str | None) -> str:
    """An output with one Bambuddy slice, sliced file 81, its hash ``file_hash`` (None:
    not read from Bambuddy yet), as a send records it."""
    output_id = make_output(client, model)
    uploads = state(client).uploads
    asyncio.run(uploads.record(output_id, LibraryCopy(id=12, folder_id=None, target_key="H2C")))
    asyncio.run(uploads.record_sliced(output_id, 12, SlicedCopy(id=81)))
    if file_hash is not None:
        asyncio.run(uploads.record_slice_hash(output_id, 81, file_hash))
    return output_id


def assert_refused_for(client: TestClient, model: str, output_id: str) -> None:
    linked = sources(client)["linked"]
    assert linked is not None and linked["output_id"] == output_id

    response = import_print(client, {"source": {"kind": "archive"}, "slug": model})

    assert response.status_code == 409, response.text
    assert response.json()["owner"]["output_id"] == output_id
    links = state(client).print_links
    assert asyncio.run(links.import_of(ARCHIVE)) is None
    assert asyncio.run(links.for_subject(PrintSubject.model(model))) == []
    assert client.get(f"/api/v1/prints/{ARCHIVE}").json()["output_id"] == output_id


@respx.mock
def test_a_print_of_an_output_s_slice_is_linked_to_it_not_imported(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = sliced_output(client, model, HASH)
    bambuddy()
    assert asyncio.run(state(client).print_links.linked(ARCHIVE)) is None

    assert_refused_for(client, model, output_id)


@respx.mock
def test_a_slice_whose_hash_was_not_read_yet_is_read_for_the_archive(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = sliced_output(client, model, None)
    bambuddy(created_at=datetime.now(UTC).isoformat())
    respx.get(f"{API}/library/files/81").mock(
        return_value=httpx.Response(200, json={"id": 81, "filename": FILE, "file_hash": HASH})
    )

    assert_refused_for(client, model, output_id)


@respx.mock
def test_a_lan_start_s_print_is_linked_to_its_output_not_imported(
    client: TestClient, model: str, pg_conninfo: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    native_slice(client, pg_conninfo, output_id)
    bambuddy(content_hash=NATIVE_HASH)

    assert_refused_for(client, model, output_id)


@respx.mock
def test_a_repeat_of_a_new_model_import_says_it_made_the_model(
    client: TestClient, model: str
) -> None:
    configure(client)
    bambuddy()
    links = state(client).print_links
    from scadbuddy.bambuddy.print_links import PrintImport

    record = PrintImport(
        archive_id=ARCHIVE, slug=model, source="archive_source", filename=FILE, created_model=True
    )
    assert asyncio.run(links.record_import(record)) is None

    again = import_print(client, {"source": {"kind": "archive"}, "slug": model})

    assert again.status_code == 200, again.text
    assert again.json()["created_model"] is True


@respx.mock
def test_a_printer_file_is_deleted_when_the_import_is_refused(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(client)
    bambuddy(
        printer_files={
            "path": "/cache",
            "files": [{"name": FILE, "path": f"/cache/{FILE}", "size": 4242}],
            "warnings": [],
        }
    )
    respx.get(f"{API}/printers/1/files/download", params={"path": f"/cache/{FILE}"}).mock(
        return_value=httpx.Response(200, content=a_3mf())
    )
    respx.get(f"{API}/library/files", params={"folder_id": "2"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(200, json={"id": 55, "filename": FILE})
    )
    deleted = respx.delete(f"{API}/library/files/55").mock(return_value=httpx.Response(200))
    links = state(client).print_links
    output_id = make_output(client, model)

    async def lost_race(record: Any, **_: Any) -> Any:
        await links.record(
            PrintSubject.output(output_id), PrintLink(archive_id=ARCHIVE, matched_by="queue_item")
        )
        return await links.linked(ARCHIVE)

    monkeypatch.setattr(links, "record_import", lost_race)

    response = import_print(
        client, {"source": {"kind": "printer", "path": f"/cache/{FILE}"}, "slug": model}
    )

    assert response.status_code == 409, response.text
    assert deleted.called, "the library copy no import names is deleted"


@respx.mock
def test_a_printer_file_already_in_the_inbox_is_reused(client: TestClient, model: str) -> None:
    configure(client)
    data = a_3mf()
    digest = hashlib.sha256(data).hexdigest()
    bambuddy(
        printer_files={
            "path": "/cache",
            "files": [{"name": FILE, "path": f"/cache/{FILE}", "size": len(data)}],
            "warnings": [],
        }
    )
    respx.get(f"{API}/printers/1/files/download", params={"path": f"/cache/{FILE}"}).mock(
        return_value=httpx.Response(200, content=data)
    )
    kept = {**library_row(56, FILE, digest), "file_size": len(data)}
    respx.get(f"{API}/library/files", params={"folder_id": "2"}).mock(
        return_value=httpx.Response(200, json=[kept])
    )
    respx.get(f"{API}/library/files/56").mock(
        return_value=httpx.Response(200, json={"id": 56, "filename": FILE, "file_hash": digest})
    )
    upload = respx.post(f"{API}/library/files")

    response = import_print(
        client, {"source": {"kind": "printer", "path": f"/cache/{FILE}"}, "slug": model}
    )

    assert response.status_code == 201, response.text
    assert response.json()["imported"]["library_file_id"] == 56
    assert not upload.called, "a retry's copy is the first one's"


@respx.mock
def test_a_run_s_link_found_after_an_import_hides_the_import(
    client: TestClient, model: str
) -> None:
    """#4698 review: once a run's link owns the print, the sources name it alone."""
    configure(client)
    bambuddy()
    assert import_print(client, {"source": {"kind": "archive"}, "slug": model}).status_code == 201
    output_id = make_output(client, model)
    link(client, output_id, ARCHIVE)

    body = sources(client)

    assert body["linked"]["output_id"] == output_id
    assert body["imported"] is None


@respx.mock
def test_a_printer_file_is_not_read_without_the_inbox_folder(
    client: TestClient, model: str
) -> None:
    configure(client, library_folder_id=None)
    bambuddy(
        printer_files={
            "path": "/cache",
            "files": [{"name": FILE, "path": f"/cache/{FILE}", "size": 4242}],
            "warnings": [],
        }
    )
    download = respx.get(f"{API}/printers/1/files/download")

    response = import_print(
        client, {"source": {"kind": "printer", "path": f"/cache/{FILE}"}, "slug": model}
    )

    assert response.status_code == 409, response.text
    assert not download.called


@respx.mock
def test_an_import_waiting_on_another_is_refused_in_time(
    client: TestClient, model: str, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4698 review: a second import of one archive waits `IMPORT_LOCK_WAIT_S`, then 409."""
    monkeypatch.setattr(print_links, "IMPORT_LOCK_WAIT_S", 0.2)
    configure(client)
    bambuddy()
    with psycopg.connect(pg_conninfo, autocommit=True) as held:
        held.execute(
            "SELECT pg_advisory_lock(%s, %s)", (print_links.IMPORT_SESSION_LOCK_CLASS, ARCHIVE)
        )
        response = import_print(client, {"source": {"kind": "archive"}, "slug": model})

    assert response.status_code == 409, response.text
    assert "still running" in response.json()["detail"]
    assert import_print(client, {"source": {"kind": "archive"}, "slug": model}).status_code == 201


@respx.mock
def test_a_retry_whose_source_changed_names_the_model_and_links_nothing(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5979: a first request makes the model but records no import; its retry with the
    same key reads the source again, now other bytes, as a printer file overwritten in
    between would be. The retry links nothing, and names the model only while it exists."""
    from scadbuddy.api import print_imports as route
    from tests.api.test_mesh_models import painted_3mf

    configure(client)
    bambuddy(source_3mf_path="archive/x/source/demo_elan.3mf")
    content = {"bytes": painted_3mf()}
    respx.get(f"{API}/archives/{ARCHIVE}/source").mock(
        side_effect=lambda request: httpx.Response(200, content=content["bytes"])
    )
    record = route._record

    async def lost(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the answer never arrived")

    body = {"source": {"kind": "archive_source"}, "new_model": {"name": "Trophy"}}
    key = press()
    monkeypatch.setattr(route, "_record", lost)
    with pytest.raises(RuntimeError):
        import_print(client, body, **key)
    monkeypatch.setattr(route, "_record", record)
    assert client.get("/api/v1/models/trophy/scene").status_code == 200
    content["bytes"] = a_3mf()

    made = import_print(client, body, **key)

    assert made.status_code == 409, made.text
    assert "the model 'trophy' was made from" in made.json()["detail"]
    assert asyncio.run(state(client).print_links.linked(ARCHIVE)) is None

    assert client.delete("/api/v1/models/trophy", headers=press()).status_code == 204
    gone = import_print(client, body, **key)

    assert gone.status_code == 409, gone.text
    assert "that request made the model 'trophy', which was deleted since" in gone.json()["detail"]
    assert gone.json()["operation_id"] == made.json()["operation_id"]
    assert asyncio.run(state(client).print_links.linked(ARCHIVE)) is None


# --- the print watcher's activity row (#6029) -----------------------------------------


def watcher_activity(client: TestClient) -> httpx.Response:
    response: httpx.Response = client.get(
        f"/api/v1/activity/print_auto_import:{ARCHIVE}", params={"advanced": 1}
    )
    return response


@respx.mock
def test_an_import_rewrites_the_print_watcher_s_activity_row(
    client: TestClient, model: str
) -> None:
    """#6029: Import print of an archive the watcher dismissed rewrites its activity row
    in the link's transaction, so the "Where" facet says the print is on its model."""
    configure(client)
    bambuddy()
    store = state(client).components.get(AUTO_IMPORTS)
    row = AutoImport(archive_id=ARCHIVE, printer_id=1, print_name="demo_elan", state="inbox")
    assert asyncio.run(store.record(row))
    assert asyncio.run(store.dismiss(ARCHIVE)) is not None
    assert watcher_activity(client).json()["row"]["facets"]["state"] == "dismissed"

    response = import_print(client, {"source": {"kind": "archive"}, "slug": model})

    assert response.status_code == 201, response.text
    item = watcher_activity(client).json()["row"]
    assert item["facets"] == {"state": "linked", "source": "archive", "matched_by": "import"}
    assert item["title"] == f"Imported print demo_elan onto {model}"
    assert item["subject"] == f"model:{model}"


@respx.mock
def test_an_import_rewrites_an_inbox_print_s_activity_row(client: TestClient, model: str) -> None:
    """#6030 review: an inbox row (never dismissed) is rewritten too."""
    configure(client)
    bambuddy()
    store = state(client).components.get(AUTO_IMPORTS)
    row = AutoImport(archive_id=ARCHIVE, printer_id=1, print_name="demo_elan", state="inbox")
    assert asyncio.run(store.record(row))
    assert watcher_activity(client).json()["row"]["facets"]["state"] == "inbox"

    response = import_print(client, {"source": {"kind": "archive"}, "slug": model})

    assert response.status_code == 201, response.text
    item = watcher_activity(client).json()["row"]
    assert item["facets"] == {"state": "linked", "source": "archive", "matched_by": "import"}
    assert item["title"] == f"Imported print demo_elan onto {model}"
    assert item["subject"] == f"model:{model}"


@respx.mock
def test_an_import_writes_no_watcher_row_the_watcher_never_had(
    client: TestClient, model: str
) -> None:
    configure(client)
    bambuddy()

    response = import_print(client, {"source": {"kind": "archive"}, "slug": model})

    assert response.status_code == 201, response.text
    assert watcher_activity(client).status_code == 404


# --- #6394: onto a native library file ----------------------------------------------


@pytest.fixture
def native_api(settings: Settings, s3: S3Settings) -> Iterator[TestClient]:
    """An app on `library_backend=native` over moto's S3."""
    with TestClient(create_app(_native_settings(settings, s3))) as client:
        yield client


def native_file(client: TestClient, tmp_path: Path, name: str = "demo_elan.3mf") -> NativeFile:
    return _upload(client, _three_mf(tmp_path / name))


@respx.mock
def test_an_import_onto_a_native_library_file_shows_under_that_file(
    native_api: TestClient, tmp_path: Path
) -> None:
    client = native_api
    configure(client)
    bambuddy()
    file = native_file(client, tmp_path)
    native_file(client, tmp_path, "unrelated.3mf")

    suggested = sources(client)["suggested_library_files"]
    response = import_print(client, {"source": {"kind": "archive"}, "library_file_id": file.id})

    assert suggested == [{"id": file.id, "filename": "demo_elan.3mf", "folder_id": None}]
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["subject"], body["slug"], body["library_file_id"]) == (
        f"library:{file.id}",
        None,
        file.id,
    )
    assert (body["imported"]["subject"], body["imported"]["slug"]) == (f"library:{file.id}", None)
    # The source is where the 3MF was read from, not the target.
    assert body["imported"]["library_file_id"] is None

    detail = client.get(f"/api/v1/prints/{ARCHIVE}")
    assert detail.status_code == 200, detail.text
    record = detail.json()
    assert (record["library_file_id"], record["library_file_name"], record["slug"]) == (
        file.id,
        "demo_elan.3mf",
        None,
    )
    assert record["imported"]["subject"] == f"library:{file.id}"
    listed = client.get("/api/v1/prints", params={"library_file_id": file.id}).json()["items"]
    assert [(item["archive_id"], item["library_file_name"]) for item in listed] == [
        (ARCHIVE, "demo_elan.3mf")
    ]
    # The media proxy takes it, as `workspace_save` print_image reads it.
    respx.get(f"{API}/archives/{ARCHIVE}/thumbnail").mock(
        return_value=httpx.Response(200, content=b"png", headers={"content-type": "image/png"})
    )
    respx.get(f"{API}/archives/{ARCHIVE}/photos/finish_1.jpg").mock(
        return_value=httpx.Response(200, content=b"jpg", headers={"content-type": "image/jpeg"})
    )
    assert client.get(f"/api/v1/prints/{ARCHIVE}/thumbnail").status_code == 200
    assert client.get(f"/api/v1/prints/{ARCHIVE}/photos/finish_1.jpg").status_code == 200

    again = import_print(client, {"source": {"kind": "archive"}, "library_file_id": file.id})
    elsewhere = import_print(client, {"source": {"kind": "archive"}, "slug": "other"})
    assert (again.status_code, again.json()["made_now"]) == (200, False)
    assert elsewhere.status_code == 409
    assert elsewhere.json()["detail"] == (
        f"archive {ARCHIVE} is a print of library file {file.id} already"
    )


@respx.mock
def test_a_library_target_is_a_native_file_that_stands(
    native_api: TestClient, tmp_path: Path
) -> None:
    client = native_api
    configure(client)
    bambuddy()
    trashed = native_file(client, tmp_path)
    assert client.portal is not None
    client.portal.call(_native(client).trash, [trashed.id])

    bambuddy_id = import_print(client, {"source": {"kind": "archive"}, "library_file_id": 7})
    missing = import_print(
        client, {"source": {"kind": "archive"}, "library_file_id": 1_999_999_999}
    )
    gone = import_print(client, {"source": {"kind": "archive"}, "library_file_id": trashed.id})

    assert bambuddy_id.status_code == 422
    assert (missing.status_code, gone.status_code) == (404, 404)
    assert client.get(f"/api/v1/prints/{ARCHIVE}").status_code == 404


@respx.mock
def test_a_run_s_per_print_copy_is_never_a_target(native_api: TestClient, tmp_path: Path) -> None:
    """#6394 review: the sweep trashes a per-print copy, and its purge would forget the
    import and the print's attachments, so the import is refused up front."""
    client = native_api
    configure(client)
    bambuddy()
    path = _three_mf(tmp_path / "demo_elan.3mf")
    assert client.portal is not None
    with path.open("rb") as data:
        copy: NativeFile = client.portal.call(
            partial(_native(client).upload, path.name, data, None, print_subject="output:abc")
        )

    response = import_print(client, {"source": {"kind": "archive"}, "library_file_id": copy.id})

    assert response.status_code == 422
    assert "per-print copy" in response.json()["detail"]
    assert sources(client)["suggested_library_files"] == []
    assert client.get(f"/api/v1/prints/{ARCHIVE}").status_code == 404
    # A search marks it, so the dialog's file search leaves it out.
    found = client.get("/api/v1/print/library/search", params={"q": "demo", "all": "true"})
    assert [(hit["id"], hit["per_print"]) for hit in found.json()["files"]] == [(copy.id, True)]


@respx.mock
def test_a_library_target_needs_the_native_library(client: TestClient) -> None:
    configure(client)
    bambuddy()

    response = import_print(
        client, {"source": {"kind": "archive"}, "library_file_id": 1_000_000_001}
    )

    assert response.status_code == 404
    assert sources(client)["suggested_library_files"] == []


@respx.mock
def test_a_restore_keeps_the_imports_and_a_purge_forgets_them(
    native_api: TestClient, tmp_path: Path
) -> None:
    """#6394 review: a trash can be undone, so only a purge forgets the imports onto a
    file, with the print's link and its import record."""
    client = native_api
    configure(client)
    bambuddy()
    file = native_file(client, tmp_path)
    body = {"source": {"kind": "archive"}, "library_file_id": file.id}
    assert import_print(client, body).status_code == 201
    native = _native(client)
    assert client.portal is not None

    client.portal.call(native.trash, [file.id])
    assert client.get(f"/api/v1/prints/{ARCHIVE}").status_code == 200
    client.portal.call(native.restore, [file.id])
    assert client.get(f"/api/v1/prints/{ARCHIVE}").json()["library_file_name"] == "demo_elan.3mf"
    assert asyncio.run(state(client).print_links.import_of(ARCHIVE)) is not None

    client.portal.call(native.trash, [file.id])
    # Not past the cutoff: the purge keeps the row, and the imports with it.
    assert not client.portal.call(native.purge, file.id, datetime(2000, 1, 1, tzinfo=UTC))
    assert client.get(f"/api/v1/prints/{ARCHIVE}").status_code == 200
    assert client.portal.call(native.purge, file.id, datetime(2999, 1, 1, tzinfo=UTC))

    assert client.get(f"/api/v1/prints/{ARCHIVE}").status_code == 404
    assert asyncio.run(state(client).print_links.import_of(ARCHIVE)) is None
    # The print can be imported again, elsewhere.
    other = native_file(client, tmp_path, "demo_elan_v2.3mf")
    response = import_print(client, {"source": {"kind": "archive"}, "library_file_id": other.id})
    assert response.status_code == 201, response.text


@respx.mock
def test_a_repeat_import_names_the_send_that_took_the_import_over(
    native_api: TestClient, tmp_path: Path
) -> None:
    """#6394 review: ScadBuddy's own send of the file names the archive after the import:
    its link takes the import's over, and a repeat import is a 409 naming that print."""
    client = native_api
    configure(client)
    bambuddy()
    file = native_file(client, tmp_path)
    body = {"source": {"kind": "archive"}, "library_file_id": file.id}
    assert import_print(client, body).status_code == 201
    links = state(client).print_links
    subject = PrintSubject.library(file.id)
    asyncio.run(links.record_sends(subject, [PrintSend(queue_item_id=51, plate_id=1)]))
    asyncio.run(links.link_library(51, ARCHIVE, "demo_elan.3mf"))

    again = import_print(client, body)

    assert again.status_code == 409, again.text
    assert again.json()["owner"]["subject"] == subject.key
    assert asyncio.run(links.import_of(ARCHIVE)) is None
    detail = client.get(f"/api/v1/prints/{ARCHIVE}").json()
    assert detail["imported"] is None


@respx.mock
def test_one_target_only(client: TestClient, model: str) -> None:
    configure(client)

    both = import_print(
        client,
        {"source": {"kind": "archive"}, "slug": model, "library_file_id": 1_000_000_001},
    )

    assert both.status_code == 422
