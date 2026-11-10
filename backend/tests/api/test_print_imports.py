"""#4698 — Import print: adopt a Bambuddy archive ScadBuddy did not start, choose where
its 3MF comes from, and link it to a model. Bambuddy is mocked with respx; its shapes are
the recordings, and the printer's file list is ``routes/printers.py`` lines 1604-1629 at
maziggy/bambuddy@f9080d1."""

from __future__ import annotations

import io
import zipfile
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_print_history import link, mock_archive
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
        "slug": model,
        "source": "archive",
        "library_file_id": None,
        "printer_id": None,
        "printer_path": None,
        "filename": FILE,
        "size": 4242,
        "sha256": HASH,
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
    assert (made["slug"], made["created_model"]) == ("trophy", True)
    assert made["imported"]["source"] == "archive_source"
    assert client.get("/api/v1/models/trophy/scene").status_code == 200
    assert client.get(f"/api/v1/prints/{ARCHIVE}").json()["slug"] == "trophy"
    again = import_print(client, body, **key)
    assert (again.status_code, again.json()["slug"]) == (200, "trophy")


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
