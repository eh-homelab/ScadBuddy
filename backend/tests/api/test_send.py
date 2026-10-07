"""Issues #25 and #26 — POST /outputs/{id}/send, through the real app."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import pytest
import respx
import trimesh
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.split import ColourPart
from tests.api.conftest import wait_for_job
from tests.support.operations import press

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"


def make_output(client: TestClient, slug: str, name: str = "Elan") -> str:
    job_id: str = client.post(
        f"/api/v1/models/{slug}/render", json={"params": {"width": 12}}
    ).json()["job_id"]
    wait_for_job(client, job_id)
    created: str = client.post(
        f"/api/v1/models/{slug}/outputs", json={"job_id": job_id, "name": name}
    ).json()["id"]
    return created


def configure(client: TestClient, **extra: Any) -> None:
    body: dict[str, Any] = {
        "bambuddy_url": BASE,
        "bambuddy_api_key": "s3cret",
        "library_folder_id": 2,
    }
    body.update(extra)
    assert client.put("/api/v1/settings", json=body).status_code == 200


def upload_route(file_id: int = 41) -> respx.Route:
    """The upload, and the read that finds the copy still there when it is reused (#316)."""
    respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(
            200, json={"id": file_id, "filename": "demo-elan.3mf", "folder_id": 2}
        )
    )
    return respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": file_id,
                "filename": "demo-elan.3mf",
                "file_type": "3mf",
                "file_size": 9,
                "thumbnail_path": None,
            },
        )
    )


def plate_routes(*, printer_id: int = 1, model: str = "H2C") -> None:
    """Mock what the send reads to learn which printer's plate to lay out for (#105):
    the printer list, for the model of the printer set in Settings."""
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": printer_id,
                    "name": "3DP-31B-598",
                    "model": model,
                    "is_active": True,
                    "nozzle_count": 2,
                }
            ],
        )
    )


@pytest.mark.requires_postgres
@respx.mock
def test_links_point_at_the_first_web_url_not_the_api_url(client: TestClient, model: str) -> None:
    """#775: the API URL may be one only the server reaches (an in-cluster Service)."""
    configure(client, bambuddy_web_urls="https://bambuddy.sso.test/, https://bambuddy.lan.test")
    output_id = make_output(client, model)
    upload_route()

    body = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    ).json()

    assert body["bambuddy_url"] == "https://bambuddy.sso.test/library"


# --- #25 library mode ---------------------------------------------------------------


@pytest.mark.requires_postgres
@respx.mock
def test_library_mode_uploads_to_the_configured_folder_and_records_the_id(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client)
    output_id = make_output(client, model)
    route = upload_route()

    response = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"library_file_id", "filename", "bambuddy_url", "edit_url", "created"}
    assert body["library_file_id"] == 41
    assert body["created"] is True
    assert body["bambuddy_url"] == f"{BASE}/library"

    request = route.calls.last.request
    assert request.url.params["folder_id"] == "2"
    assert request.headers["X-API-Key"] == "s3cret"
    assert b"demo-elan.3mf" in request.content

    # Recorded in Postgres, not in meta.json (#455)...
    meta = json.loads(
        (paths.output_dir(model, output_id) / "meta.json").read_text(encoding="utf-8")
    )
    assert "library_files" not in meta

    # ...and the detail route reports it, so the UI can deep-link without re-sending.
    rows = client.get(f"/api/v1/outputs/{output_id}").json()["library_files"]
    assert [(row["id"], row["folder_id"]) for row in rows] == [(41, 2)]


@pytest.mark.requires_postgres
@respx.mock
def test_a_re_send_reuses_the_inbox_copy_rather_than_duplicating_it(
    client: TestClient, model: str
) -> None:
    """Same folder, same printer: the copy already there is this exact 3MF (#316)."""
    configure(client)
    output_id = make_output(client, model)
    upload = upload_route()
    delete = respx.delete(f"{API}/library/files/41").mock(return_value=httpx.Response(200, json={}))
    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press())

    respx.get(f"{API}/library/files/41").mock(
        return_value=httpx.Response(
            200, json={"id": 41, "filename": "renamed-in-bambuddy.3mf", "folder_id": 2}
        )
    )
    body = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    ).json()

    assert body["library_file_id"] == 41
    # Reused, not uploaded: the agent records it as changed rather than new (#931).
    assert body["created"] is False
    # The name is the one the existence read returned, not the one uploaded.
    assert body["filename"] == "renamed-in-bambuddy.3mf"
    assert upload.call_count == 1
    assert not delete.called


@pytest.mark.requires_postgres
@respx.mock
def test_a_re_send_survives_the_file_having_been_deleted_in_bambuddy(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload = upload_route()
    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press())

    respx.get(f"{API}/library/files/41").mock(
        return_value=httpx.Response(404, json={"detail": "Not found"})
    )
    upload_route(42)

    response = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    )

    assert response.status_code == 200
    assert response.json()["library_file_id"] == 42
    assert upload.call_count == 2


def test_sending_without_a_url_configured_is_a_conflict(client: TestClient, model: str) -> None:
    output_id = make_output(client, model)

    response = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    )

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"


def test_sending_an_unknown_output_is_a_404(client: TestClient) -> None:
    response = client.post(
        f"/api/v1/outputs/{'0' * 32}/send", json={"mode": "library"}, headers=press()
    )
    assert response.status_code == 404


# --- #312 the send bar only uploads --------------------------------------------------


@pytest.mark.requires_postgres
@respx.mock
def test_queue_mode_is_refused_and_nothing_is_uploaded(client: TestClient, model: str) -> None:
    """A stale client still asking to queue gets a 422, not a silent library upload."""
    configure(client)
    output_id = make_output(client, model)
    upload = upload_route()

    response = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "queue", "copies": 2}, headers=press()
    )

    assert response.status_code == 422
    assert not upload.called


@pytest.mark.requires_postgres
@respx.mock
def test_an_old_clients_extra_fields_are_ignored(client: TestClient, model: str) -> None:
    """The dialog used to send ``options`` with every library send; that still uploads."""
    configure(client)
    output_id = make_output(client, model)
    upload_route()

    response = client.post(
        f"/api/v1/outputs/{output_id}/send",
        json={"mode": "library", "options": {"timelapse": False}},
        headers=press(),
    )

    assert response.status_code == 200


@pytest.mark.requires_postgres
@respx.mock
def test_a_stored_pipeline_is_never_read_by_the_send(client: TestClient, model: str) -> None:
    """``pipeline_id`` is gone from Settings (#312), so a client that still sends one
    changes nothing: the plate is the Settings printer's, and no ``/slicer-pipelines/``
    route is mocked, so reading one would fail this test."""
    configure(client, pipeline_id=4, printer_id=1)
    plate_routes(printer_id=1, model="H2C")
    output_id = make_output(client, model)
    upload_route()

    assert client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    ).is_success
    assert all("slicer-pipelines" not in str(call.request.url) for call in respx.calls)


@pytest.mark.requires_postgres
@respx.mock
def test_a_send_starts_no_print(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press())

    assert client.get(f"/api/v1/print/outputs/{output_id}/progress").json() is None
    record = client.get(f"/api/v1/outputs/{output_id}").json()
    assert record["queue_item_id"] is None
    assert record["print_route"] is None


# --- #105 the plate follows the target printer --------------------------------------


@pytest.mark.requires_postgres
@respx.mock
def test_the_upload_is_laid_out_for_the_settings_printers_plate(
    client: TestClient, model: str
) -> None:
    """An H2C reaches x 25..325, so its centre is 175,160 — not the 256-plate's 128,128."""
    configure(client, printer_id=1)
    plate_routes(printer_id=1, model="H2C")
    output_id = make_output(client, model)
    upload = upload_route()

    assert client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    ).is_success

    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        root = ET.fromstring(archive.read("3D/3dmodel.model"))
    item = root.find(".//{*}item")
    assert item is not None
    transform = [float(value) for value in (item.get("transform") or "").split()]
    assert transform[9:11] == [175.0, 160.0]


@pytest.mark.requires_postgres
@respx.mock
def test_an_unknown_printer_model_still_uploads_on_the_default_plate(
    client: TestClient, model: str
) -> None:
    configure(client, printer_id=1)
    plate_routes(printer_id=1, model="SomeFuturePrinter")
    output_id = make_output(client, model)
    upload = upload_route()

    assert client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    ).status_code

    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        root = ET.fromstring(archive.read("3D/3dmodel.model"))
    item = root.find(".//{*}item")
    assert item is not None
    assert [float(v) for v in (item.get("transform") or "").split()][9:11] == [128.0, 128.0]


@pytest.mark.requires_postgres
@respx.mock
def test_a_model_too_big_for_the_printer_is_refused_before_the_upload(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client, printer_id=1)
    plate_routes(printer_id=1, model="A1 mini")
    output_id = make_output(client, model)
    # 200 mm across does not fit an A1 mini's 180 mm bed.
    write_bambu_3mf(
        [ColourPart(1, "Color 1", "#FF0000", trimesh.creation.box(extents=(200, 200, 4)))],
        paths.output_dir(model, output_id) / "model.3mf",
        thumbnails=None,
        model_name=model,
    )
    upload = upload_route()

    response = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    )

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert "A1 mini" in response.json()["detail"]
    assert not upload.called


# --- #126 the send bar states no nozzle; the print run does ------------------------


def _uploaded_nozzle(route: respx.Route) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(route))) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    nozzle: list[str] = settings["nozzle_diameter"]
    return nozzle


@pytest.mark.requires_postgres
@respx.mock
def test_the_send_bar_upload_keeps_the_placeholder_nozzle(client: TestClient, model: str) -> None:
    """The send bar chooses no nozzle, so the 3MF keeps the placeholder and the preset
    catalogue is never read (#126 now applies to the print run only)."""
    configure(client, printer_id=1)
    plate_routes(printer_id=1, model="A1")
    output_id = make_output(client, model)
    upload = upload_route()

    assert client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    ).is_success

    assert _uploaded_nozzle(upload) == ["0.4"]


def _uploaded_3mf(route: respx.Route) -> bytes:
    """The ``file`` part of the multipart upload Bambuddy received."""
    request = route.calls.last.request
    boundary = request.headers["content-type"].split("boundary=", 1)[1].encode()
    for part in request.read().split(b"--" + boundary):
        head, _, body = part.partition(b"\r\n\r\n")
        if b'name="file"' in head:
            return body.rsplit(b"\r\n", 1)[0]
    raise AssertionError("the upload carried no file part")


@pytest.mark.requires_postgres
@respx.mock
def test_a_refused_re_send_leaves_the_previous_file_in_place(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The fit check runs before anything touches Bambuddy, so a refusal costs nothing.

    Deleting first would leave the recorded copy pointing at a file that is no longer
    in Bambuddy: the button would report 409 and the deep link would 404. (A delete
    that fails *after* the new upload is covered in ``test_library_copies.py``.)
    """
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "name": "big", "model": "H2C", "is_active": True, "nozzle_count": 2},
                {
                    "id": 2,
                    "name": "small",
                    "model": "A1 mini",
                    "is_active": True,
                    "nozzle_count": 1,
                },
            ],
        )
    )
    configure(client, printer_id=1)
    output_id = make_output(client, model)
    upload_route()
    assert (
        client.post(
            f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
        ).json()["library_file_id"]
        == 41
    )

    delete = respx.delete(f"{API}/library/files/41").mock(return_value=httpx.Response(200, json={}))
    # Same output, but now aimed at a printer it cannot possibly fit on.
    write_bambu_3mf(
        [ColourPart(1, "Color 1", "#FF0000", trimesh.creation.box(extents=(200, 200, 4)))],
        paths.output_dir(model, output_id) / "model.3mf",
        thumbnails=None,
        model_name=model,
    )
    configure(client, printer_id=2)

    response = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    )

    assert response.status_code == 409
    assert not delete.called, "the old file was removed before the refusal"
    rows = client.get(f"/api/v1/outputs/{output_id}").json()["library_files"]
    assert [row["id"] for row in rows] == [41]


# --- #80 the Edit in ScadBuddy back-link ---------------------------------------------


def annotate_route(file_id: int = 41, notes: str | None = None) -> respx.Route:
    """The note is a read-modify-write, so both halves are mocked."""
    respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(
            200, json={"id": file_id, "filename": "demo-elan.3mf", "notes": notes}
        )
    )
    return respx.put(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(200, json={"id": file_id, "filename": "demo-elan.3mf"})
    )


@pytest.mark.requires_postgres
@respx.mock
def test_the_edit_link_is_attached_to_the_uploaded_file(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client, public_url="https://scad.test/")
    output_id = make_output(client, model)
    upload_route()
    annotate = annotate_route()

    body = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    ).json()

    edit_url = f"https://scad.test/edit/{output_id}"
    assert body["edit_url"] == edit_url
    assert annotate.called
    assert json.loads(annotate.calls.last.request.content) == {
        "notes": f"Edit in ScadBuddy: {edit_url}"
    }


@pytest.mark.requires_postgres
@respx.mock
def test_nothing_is_attached_when_no_public_url_is_configured(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    annotate = annotate_route()

    body = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    ).json()

    assert body["edit_url"] is None
    assert not annotate.called


@pytest.mark.requires_postgres
@respx.mock
def test_a_failed_annotation_still_returns_the_upload(client: TestClient, model: str) -> None:
    """The note is cosmetic; the upload is the point of the request."""
    configure(client, public_url="https://scad.test")
    output_id = make_output(client, model)
    upload = upload_route()
    annotate_route()  # the read succeeds; the write is what fails
    annotate = respx.put(f"{API}/library/files/41").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )

    response = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["library_file_id"] == 41
    # Nothing was attached, so the result does not claim a link.
    assert body["edit_url"] is None
    assert annotate.called
    assert upload.called


@pytest.mark.requires_postgres
@respx.mock
def test_the_annotation_runs_after_the_upload(client: TestClient, model: str) -> None:
    """A slow or broken annotate must not sit in front of the upload."""
    configure(client, public_url="https://scad.test")
    output_id = make_output(client, model)
    upload_route()
    annotate_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press())

    order = [(call.request.method, call.request.url.path) for call in respx.calls]
    assert order.index(("POST", "/api/v1/library/files")) < order.index(
        ("PUT", "/api/v1/library/files/41")
    )


@pytest.mark.requires_postgres
@respx.mock
def test_the_annotation_is_a_partial_update_of_notes_alone(client: TestClient, model: str) -> None:
    """Bambuddy's ``update_file`` guards every assignment with ``if data.X is not
    None``, so an omitted field is left alone — see the citation in client.py."""
    configure(client, public_url="https://scad.test")
    output_id = make_output(client, model)
    upload_route()
    annotate = annotate_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press())

    assert json.loads(annotate.calls.last.request.content) == {
        "notes": f"Edit in ScadBuddy: https://scad.test/edit/{output_id}"
    }


@pytest.mark.requires_postgres
@respx.mock
def test_a_note_someone_typed_in_bambuddy_is_not_overwritten(
    client: TestClient, model: str
) -> None:
    """``notes`` is the file's only free-text field, so it is not ScadBuddy's to clear."""
    configure(client, public_url="https://scad.test")
    output_id = make_output(client, model)
    upload_route()
    annotate = annotate_route(
        notes="PLA only — the black spool\nEdit in ScadBuddy: https://old/edit/x"
    )

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}, headers=press())

    assert json.loads(annotate.calls.last.request.content) == {
        "notes": (
            f"PLA only — the black spool\nEdit in ScadBuddy: https://scad.test/edit/{output_id}"
        )
    }
