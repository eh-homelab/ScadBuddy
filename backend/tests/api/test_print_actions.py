"""Issue #311 — the print detail page's two writes: "Print again" queues the archive
again (``POST /queue/`` with ``archive_id``; Bambuddy's reprint route is a 410, plan
M6), and "Pull timelapse from printer" attaches a timelapse still on the printer
(``timelapse/select``, plan A4). Both only for an archive one of ScadBuddy's outputs
printed, like every other prints route."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_print_history import link, mock_archive
from tests.api.test_send import API, BASE, configure, make_output
from tests.bambuddy.conftest import recording
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres


def mock_enqueue(item_id: int = 9) -> respx.Route:
    return respx.post(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json={**recording("queue-item.json"), "id": item_id})
    )


# --- print again ------------------------------------------------------------------


@respx.mock
def test_print_again_queues_the_archive_on_its_printer_and_plate(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    mock_archive(35, printer_id=3, plate_id=2)
    queue = mock_enqueue(51)

    response = client.post("/api/v1/prints/35/reprint", headers=press())

    assert response.status_code == 201
    assert response.json() == {
        "queue_item_id": 51,
        "printer_id": 3,
        "bambuddy_url": f"{BASE}/queue",
    }
    sent = json.loads(queue.calls.last.request.content)
    assert sent["archive_id"] == 35
    assert sent["printer_id"] == 3
    assert sent["plate_id"] == 2
    assert "library_file_id" not in sent


@respx.mock
def test_print_again_queues_with_the_remembered_print_options(
    client: TestClient, model: str
) -> None:
    """#1329: global, then the printer's, then the model's, as a print from the dialog."""
    configure(client)
    output_id = make_output(client, model)
    slug = client.get(f"/api/v1/outputs/{output_id}").json()["slug"]
    link(client, output_id, 35)
    mock_archive(35, printer_id=3, plate_id=2)
    queue = mock_enqueue()
    options = "/api/v1/settings/print-options"
    remember = [
        {"scope": "global", "options": {"use_ams": False, "layer_inspect": True}},
        {"scope": "printer", "key": "3", "options": {"manual_start": True, "timelapse": True}},
        {"scope": "printer", "key": "4", "options": {"auto_off_after": True}},
        # A copy count and a project are the dialog's own controls, not a reprint's.
        {
            "scope": "model",
            "key": slug,
            "options": {"layer_inspect": False, "quantity": 3, "project_id": 7},
        },
    ]
    for body in remember:
        assert client.put(options, json=body).status_code == 200

    assert client.post("/api/v1/prints/35/reprint", headers=press()).status_code == 201

    sent = json.loads(queue.calls.last.request.content)
    assert sent["manual_start"] is True
    assert sent["timelapse"] is True
    assert sent["use_ams"] is False
    assert sent["layer_inspect"] is False
    assert sent["auto_off_after"] is False
    assert sent.get("quantity", 1) == 1
    assert sent.get("project_id") is None


@respx.mock
def test_print_again_falls_back_to_the_links_printer_and_plate(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35, printer_id=4, plate_id=1)
    mock_archive(35, printer_id=None, plate_id=None)
    queue = mock_enqueue()

    assert client.post("/api/v1/prints/35/reprint", headers=press()).status_code == 201

    sent = json.loads(queue.calls.last.request.content)
    assert sent["printer_id"] == 4
    assert sent["plate_id"] == 1


@respx.mock
def test_print_again_without_a_printer_is_refused(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    mock_archive(35, printer_id=None)
    queue = mock_enqueue()

    response = client.post("/api/v1/prints/35/reprint", headers=press())

    assert response.status_code == 409
    assert "printer" in response.json()["detail"]
    assert not queue.called


@respx.mock
def test_a_print_deleted_in_bambuddy_cannot_be_printed_again(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35, printer_id=1)
    respx.get(f"{API}/archives/35").mock(return_value=httpx.Response(404))
    queue = mock_enqueue()

    response = client.post("/api/v1/prints/35/reprint", headers=press())

    assert response.status_code == 409
    assert "deleted" in response.json()["detail"]
    assert not queue.called


@respx.mock
def test_a_refused_queue_names_the_scope(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    mock_archive(35)
    respx.post(f"{API}/queue/").mock(return_value=httpx.Response(403))

    response = client.post("/api/v1/prints/35/reprint", headers=press())

    assert response.status_code == 409
    assert response.json()["required_scope"] == "Manage Queue"
    assert "queue archive 35" in response.json()["detail"]


@respx.mock
def test_an_archive_no_output_printed_cannot_be_printed_again(client: TestClient) -> None:
    configure(client)
    archive = mock_archive(36)
    queue = mock_enqueue()

    assert client.post("/api/v1/prints/36/reprint", headers=press()).status_code == 404
    assert not archive.called and not queue.called


# --- pull timelapse ---------------------------------------------------------------

TIMELAPSE = "video_2026-09-27_12-22-00.mp4"


@respx.mock
def test_pull_timelapse_attaches_the_named_file(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    mock_archive(35)
    select = respx.post(f"{API}/archives/35/timelapse/select").mock(
        return_value=httpx.Response(200, json={"status": "attached", "filename": TIMELAPSE})
    )

    response = client.post(
        "/api/v1/prints/35/timelapse/pull", json={"filename": TIMELAPSE}, headers=press()
    )

    assert response.status_code == 204
    assert select.calls.last.request.url.params["filename"] == TIMELAPSE


@respx.mock
def test_pull_timelapse_drops_the_cached_archive(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    before = mock_archive(35, timelapse_path=None)
    respx.get(f"{API}/archives/35/runs").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )
    assert client.get("/api/v1/prints/35").json()["media"]["timelapse"] is None
    respx.post(f"{API}/archives/35/timelapse/select").mock(
        return_value=httpx.Response(200, json={"status": "attached", "filename": TIMELAPSE})
    )

    client.post("/api/v1/prints/35/timelapse/pull", json={"filename": TIMELAPSE}, headers=press())
    client.get("/api/v1/prints/35")

    assert before.call_count == 2, "the detail after a pull reads the archive again"


@respx.mock
def test_a_print_deleted_in_bambuddy_cannot_pull_a_timelapse(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35, printer_id=1)
    respx.get(f"{API}/archives/35").mock(return_value=httpx.Response(404))
    select = respx.post(f"{API}/archives/35/timelapse/select").mock(
        return_value=httpx.Response(200, json={})
    )

    response = client.post(
        "/api/v1/prints/35/timelapse/pull", json={"filename": TIMELAPSE}, headers=press()
    )

    assert response.status_code == 409
    assert "deleted" in response.json()["detail"]
    assert not select.called


@pytest.mark.parametrize("filename", ["", "../etc/passwd", "a/b.mp4", "x" * 300])
def test_pull_timelapse_takes_a_bare_file_name(
    client: TestClient, model: str, filename: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)

    response = client.post(
        "/api/v1/prints/35/timelapse/pull", json={"filename": filename}, headers=press()
    )

    assert response.status_code == 422


@respx.mock
def test_a_timelapse_not_on_the_printer_is_bambuddys_404(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    mock_archive(35)
    respx.post(f"{API}/archives/35/timelapse/select").mock(
        return_value=httpx.Response(404, json={"detail": "Timelapse 'x.mp4' not found on printer"})
    )

    response = client.post(
        "/api/v1/prints/35/timelapse/pull", json={"filename": "x.mp4"}, headers=press()
    )

    assert response.status_code == 404


@respx.mock
def test_an_archive_no_output_printed_cannot_pull_a_timelapse(client: TestClient) -> None:
    configure(client)
    select = respx.post(f"{API}/archives/36/timelapse/select").mock(
        return_value=httpx.Response(200, json={})
    )

    response = client.post(
        "/api/v1/prints/36/timelapse/pull", json={"filename": TIMELAPSE}, headers=press()
    )

    assert response.status_code == 404
    assert not select.called


# --- scopes: the pull reads the archive (Read Status), then attaches (Manage Archives)


@respx.mock
def test_a_pull_refused_at_the_archive_read_names_read_status(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    respx.get(f"{API}/archives/35").mock(return_value=httpx.Response(403))
    select = respx.post(f"{API}/archives/35/timelapse/select").mock(
        return_value=httpx.Response(200, json={})
    )

    response = client.post(
        "/api/v1/prints/35/timelapse/pull", json={"filename": TIMELAPSE}, headers=press()
    )

    assert response.status_code == 409
    assert response.json()["required_scope"] == "Read Status"
    assert not select.called


@respx.mock
def test_a_pull_refused_at_the_attach_names_manage_archives(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    mock_archive(35)
    respx.post(f"{API}/archives/35/timelapse/select").mock(return_value=httpx.Response(403))

    response = client.post(
        "/api/v1/prints/35/timelapse/pull", json={"filename": TIMELAPSE}, headers=press()
    )

    assert response.status_code == 409
    assert response.json()["required_scope"] == "Manage Archives"


@pytest.mark.parametrize(
    ("path", "scopes"),
    [
        ("/api/v1/prints/{archive_id}/timelapse/pull", ("Read Status", "Manage Archives")),
        ("/api/v1/prints/{archive_id}/reprint", ("Read Status", "Manage Queue")),
    ],
)
def test_each_write_documents_every_scope_it_needs(
    client: TestClient, path: str, scopes: tuple[str, ...]
) -> None:
    description = client.get("/openapi.json").json()["paths"][path]["post"]["description"]

    for scope in scopes:
        assert scope in description
