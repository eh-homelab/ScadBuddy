"""Issue #308 — the prints API: a list of ScadBuddy's own prints with filters and a
cursor, and a detail joining the output's provenance and files to the archive's media
and outcome (print-history plan §2.4). Bambuddy is mocked with respx, from the
recordings of the live 1.2.5.6; the links are in Postgres."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.print_links import PrintLink
from scadbuddy.core.paths import DataPaths
from tests.api.test_send import API, BASE, configure, make_output
from tests.bambuddy.conftest import recording

pytestmark = pytest.mark.requires_postgres

OTHER_SLUG = "other"


def state(client: TestClient) -> AppState:
    app_state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return app_state


def link(client: TestClient, output_id: str, archive_id: int, **fields: Any) -> None:
    asyncio.run(
        state(client).print_links.record(
            output_id, PrintLink(archive_id=archive_id, matched_by="queue_item", **fields)
        )
    )


def archive(archive_id: int, **fields: Any) -> dict[str, Any]:
    """The recorded archive 35 (completed, with a finish photo and a timelapse), as
    ``archive_id`` and with ``fields`` changed."""
    return {**recording("archive-detail.json"), "id": archive_id, **fields}


def mock_archive(archive_id: int, **fields: Any) -> respx.Route:
    return respx.get(f"{API}/archives/{archive_id}").mock(
        return_value=httpx.Response(200, json=archive(archive_id, **fields))
    )


def other_model(paths: DataPaths) -> str:
    paths.model_dir(OTHER_SLUG).mkdir(parents=True, exist_ok=True)
    paths.model_source(OTHER_SLUG).write_text('width = 10;\nlabel = "hi";\n', encoding="utf-8")
    paths.model_meta(OTHER_SLUG).write_text(json.dumps({"name": "Other"}) + "\n", encoding="utf-8")
    return OTHER_SLUG


# --- the list -------------------------------------------------------------------


@respx.mock
def test_a_linked_print_is_listed_with_its_summary(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35, printer_id=1)
    mock_archive(35)

    response = client.get("/api/v1/prints")

    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    [summary] = body["items"]
    assert summary == {
        "archive_id": 35,
        "output_id": output_id,
        "slug": model,
        "output_name": "Elan",
        "status": "completed",
        "printer_id": 1,
        "started_at": "2026-09-27T04:09:36.529201",
        "completed_at": "2026-09-27T05:56:53.660315",
        "actual_time_seconds": 6437,
        "filament_used_grams": 16.36,
        "cover": {
            "kind": "photo",
            "url": "/api/v1/prints/35/photos/finish_20260927_015703_93372185.jpg",
        },
        "has_timelapse": True,
        "attachment_count": 0,
        # make_output renders width 12; the template's default is 10.
        "params_diff": {"width": 12},
        "run_count": 1,
    }


@respx.mock
def test_without_photos_the_cover_is_the_thumbnail(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    mock_archive(35, photos=None, timelapse_path=None)

    [summary] = client.get("/api/v1/prints").json()["items"]

    assert summary["cover"] == {"kind": "thumbnail", "url": "/api/v1/prints/35/thumbnail"}
    assert summary["has_timelapse"] is False


@respx.mock
def test_a_deleted_archive_is_listed_not_dropped(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35, printer_id=1)
    respx.get(f"{API}/archives/35").mock(
        return_value=httpx.Response(404, json={"detail": "Archive not found"})
    )

    [summary] = client.get("/api/v1/prints").json()["items"]

    assert summary["archive_id"] == 35
    assert summary["status"] == "deleted_in_bambuddy"
    assert summary["printer_id"] == 1, "what the link knew"
    assert summary["cover"] is None
    assert summary["has_timelapse"] is False
    assert summary["run_count"] == 0


def test_with_nothing_linked_the_list_is_empty(client: TestClient) -> None:
    configure(client)
    assert client.get("/api/v1/prints").json() == {"items": [], "next_cursor": None}


@respx.mock
def test_bambuddy_unreachable_is_a_problem_not_an_empty_list(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    respx.get(f"{API}/archives/35").mock(side_effect=httpx.ConnectError("refused"))

    assert client.get("/api/v1/prints").status_code == 502


@respx.mock
def test_the_list_pages_by_cursor_newest_archive_first(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    for archive_id in (16, 17, 23):
        link(client, output_id, archive_id)
        mock_archive(archive_id)

    first = client.get("/api/v1/prints", params={"limit": 2}).json()
    second = client.get("/api/v1/prints", params={"limit": 2, "cursor": first["next_cursor"]})

    assert [item["archive_id"] for item in first["items"]] == [23, 17]
    assert first["next_cursor"] is not None
    assert [item["archive_id"] for item in second.json()["items"]] == [16]
    assert second.json()["next_cursor"] is None


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"cursor": "nope"}])
def test_a_bad_limit_or_cursor_is_rejected(client: TestClient, params: dict[str, Any]) -> None:
    configure(client)
    assert client.get("/api/v1/prints", params=params).status_code == 422


@respx.mock
def test_the_list_filters_by_slug(client: TestClient, model: str, paths: DataPaths) -> None:
    configure(client)
    demo = make_output(client, model)
    other = make_output(client, other_model(paths))
    link(client, demo, 35)
    link(client, other, 36)
    mock_archive(35)
    unasked = mock_archive(36)

    items = client.get("/api/v1/prints", params={"slug": model}).json()["items"]

    assert [item["archive_id"] for item in items] == [35]
    assert not unasked.called, "another template's prints are not read from Bambuddy"


@respx.mock
def test_the_list_filters_by_status_printer_and_date(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    link(client, output_id, 36)
    link(client, output_id, 37)
    mock_archive(35)
    mock_archive(36, status="failed", failure_reason="Spaghetti", printer_id=2)
    mock_archive(
        37, status="printing", started_at="2026-09-20T10:00:00", completed_at=None, printer_id=2
    )

    def ids(**params: Any) -> list[int]:
        items = client.get("/api/v1/prints", params=params).json()["items"]
        return [item["archive_id"] for item in items]

    assert ids(status="failed") == [36]
    assert ids(printer_id=2) == [37, 36]
    assert ids(**{"from": "2026-09-21"}) == [36, 35]
    assert ids(to="2026-09-20") == [37]
    assert ids(**{"from": "2026-09-27", "to": "2026-09-27"}) == [36, 35]


@respx.mock
def test_the_list_filters_by_text(client: TestClient, model: str) -> None:
    configure(client)
    named = make_output(client, model, name="Reagan")
    other = make_output(client, model, name="Elan")
    link(client, named, 35)
    link(client, other, 36)
    mock_archive(35)
    mock_archive(36, print_name="gift tag")

    def ids(q: str) -> list[int]:
        return [
            item["archive_id"]
            for item in client.get("/api/v1/prints", params={"q": q}).json()["items"]
        ]

    assert ids("reagan") == [35], "the output's name, any case"
    assert ids("GIFT") == [36], "the print's name in Bambuddy"
    assert ids('"width": 12') == [36, 35], "the params text"
    assert ids("nothing like it") == []


@respx.mock
def test_archive_reads_are_cached_briefly(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    route = mock_archive(35)
    runs = mock_detail_reads()["runs"]

    client.get("/api/v1/prints")
    client.get("/api/v1/prints")
    client.get("/api/v1/prints/35")
    client.get("/api/v1/prints/35")

    assert route.call_count == 1
    assert runs.call_count == 1


# --- the detail -----------------------------------------------------------------


def mock_detail_reads(archive_id: int = 35) -> dict[str, respx.Route]:
    return {
        "runs": respx.get(f"{API}/archives/{archive_id}/runs").mock(
            return_value=httpx.Response(200, json=recording("archive-runs.json"))
        ),
        "info": respx.get(f"{API}/archives/{archive_id}/timelapse/info").mock(
            return_value=httpx.Response(200, json=recording("timelapse-info.json"))
        ),
        "thumbnails": respx.get(f"{API}/archives/{archive_id}/timelapse/thumbnails").mock(
            return_value=httpx.Response(200, json=recording("timelapse-thumbnails.json"))
        ),
        "printer_media": respx.get(f"{API}/archives/{archive_id}/printer-media").mock(
            return_value=httpx.Response(200, json=recording("printer-media.json"))
        ),
    }


@respx.mock
def test_the_detail_joins_provenance_files_media_and_outcome(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35, printer_id=1, plate_id=1)
    mock_archive(35)
    routes = mock_detail_reads()

    response = client.get("/api/v1/prints/35")

    assert response.status_code == 200
    body = response.json()
    # Everything the summary has, too.
    assert body["archive_id"] == 35
    assert body["params_diff"] == {"width": 12}
    meta = state(client).outputs.get(output_id)
    assert body["provenance"] == {
        "slug": model,
        "model_version": meta.model_version,
        "params": {"width": 12},
        "output_id": output_id,
        "edit_url": f"/edit/{output_id}",
    }
    files = {file["kind"]: file for file in body["files"]}
    assert set(files) == {"output_3mf", "sliced", "source", "preview_glb"}
    assert files["output_3mf"]["url"] == f"/api/v1/outputs/{output_id}/model.3mf"
    assert files["output_3mf"]["name"] == "demo-elan.3mf"
    assert files["output_3mf"]["size"] > 0
    assert files["preview_glb"]["url"] == f"/api/v1/outputs/{output_id}/preview.glb"
    assert files["sliced"] == {
        "kind": "sliced",
        "name": "name-keychain-9427184559df41f085d4737a2aafa514.gcode.3mf",
        "size": 2091667,
        "url": "/api/v1/prints/35/files/sliced",
    }
    assert files["source"]["url"] == "/api/v1/prints/35/files/source"
    assert files["source"]["name"] == "name-keychain-9427184559df41f085d4737a2aafa514.3mf"

    media = body["media"]
    # Unverified which photo is the finish photo (plan §2.4): all of them are photos.
    assert media["finish_photo"] is None
    assert media["photos"] == [
        {
            "name": "finish_20260927_015703_93372185.jpg",
            "url": "/api/v1/prints/35/photos/finish_20260927_015703_93372185.jpg",
        }
    ]
    timelapse = media["timelapse"]
    assert timelapse["url"] == "/api/v1/prints/35/timelapse"
    assert timelapse["info"]["duration"] == pytest.approx(5.208256)
    frames = recording("timelapse-thumbnails.json")
    assert timelapse["poster_frames"][0] == {
        "timestamp": frames["timestamps"][0],
        "data_url": "data:image/jpeg;base64," + frames["thumbnails"][0],
    }
    assert media["plate_thumbnails"] == [
        {"index": 1, "url": "/api/v1/prints/35/plates/1/thumbnail"}
    ]
    assert media["attachments"] == []

    outcome = body["outcome"]
    assert outcome["status"] == "completed"
    assert outcome["failure_reason"] is None
    assert outcome["estimated_time_seconds"] == 5647
    assert outcome["actual_time_seconds"] == 6437
    assert outcome["filament_used_grams"] == 16.36
    assert outcome["filament_type"] == "PLA"
    assert outcome["cost"] == 0.43
    assert outcome["printer_id"] == 1
    assert outcome["printer_name"] == "3DP-31B-598"
    assert [run["id"] for run in outcome["runs"]] == [27]

    assert body["printer_media"] is None
    assert not routes["printer_media"].called, "the printer is not asked by default"
    assert body["links"] == {"bambuddy_url": f"{BASE}/archives", "customize_url": f"/m/{model}"}


@respx.mock
def test_the_printers_media_is_read_only_on_request(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    mock_archive(35)
    routes = mock_detail_reads()

    body = client.get("/api/v1/prints/35", params={"printer_media": 1}).json()

    assert routes["printer_media"].called
    assert body["printer_media"]["remote_files"][0]["kind"] == "ipcam"


@respx.mock
def test_a_failed_print_without_a_timelapse(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 36)
    mock_archive(
        36,
        status="failed",
        failure_reason="Spaghetti detected",
        timelapse_path=None,
        source_3mf_path=None,
        photos=["a1b2c3d4.jpg", "finish_20260927_015703_93372185.jpg"],
    )
    routes = mock_detail_reads(36)

    body = client.get("/api/v1/prints/36").json()

    assert body["status"] == "failed"
    assert body["outcome"]["failure_reason"] == "Spaghetti detected"
    assert body["media"]["timelapse"] is None
    assert not routes["info"].called and not routes["thumbnails"].called
    assert [photo["name"] for photo in body["media"]["photos"]] == [
        "a1b2c3d4.jpg",
        "finish_20260927_015703_93372185.jpg",
    ]
    assert "source" not in {file["kind"] for file in body["files"]}


@respx.mock
def test_a_timelapse_bambuddy_cannot_describe_still_plays(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35)
    mock_archive(35)
    routes = mock_detail_reads()
    routes["info"].mock(return_value=httpx.Response(500, json={"detail": "ffprobe failed"}))
    routes["thumbnails"].mock(return_value=httpx.Response(500, json={"detail": "ffmpeg failed"}))

    response = client.get("/api/v1/prints/35")

    assert response.status_code == 200
    assert response.json()["media"]["timelapse"] == {
        "url": "/api/v1/prints/35/timelapse",
        "info": None,
        "poster_frames": [],
    }


@respx.mock
def test_the_detail_of_a_deleted_archive_keeps_scadbuddys_half(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    link(client, output_id, 35, printer_id=1)
    respx.get(f"{API}/archives/35").mock(return_value=httpx.Response(404))
    runs = respx.get(f"{API}/archives/35/runs").mock(return_value=httpx.Response(404))

    response = client.get("/api/v1/prints/35")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "deleted_in_bambuddy"
    assert body["outcome"]["status"] == "deleted_in_bambuddy"
    assert body["outcome"]["runs"] == []
    assert body["provenance"]["output_id"] == output_id
    assert {file["kind"] for file in body["files"]} == {"output_3mf", "preview_glb"}
    assert body["media"]["photos"] == [] and body["media"]["timelapse"] is None
    assert not runs.called


@respx.mock
def test_an_archive_no_output_printed_has_no_detail(client: TestClient) -> None:
    configure(client)
    route = mock_archive(36)

    assert client.get("/api/v1/prints/36").status_code == 404
    assert not route.called


def test_the_output_preview_mesh_is_served(client: TestClient, model: str) -> None:
    output_id = make_output(client, model)

    response = client.get(f"/api/v1/outputs/{output_id}/preview.glb")

    assert response.status_code == 200
    assert response.headers["content-type"] == "model/gltf-binary"
    assert response.content.startswith(b"glTF")
