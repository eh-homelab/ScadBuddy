"""Issue #24 — the client and its error mapping, against recorded 1.2.5.5 bodies."""

from __future__ import annotations

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import (
    ELIGIBILITY_PROBLEM,
    NOT_FOUND_PROBLEM,
    REJECTED_PROBLEM,
    SCOPE_PROBLEM,
    UNAVAILABLE_PROBLEM,
    Scope,
)
from scadbuddy.bambuddy.models import (
    PipelineRunRequest,
    PresetRef,
    QueueItemCreate,
    SliceRequest,
)
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"


@respx.mock
async def test_printers_use_the_trailing_slash_and_keep_integer_ids(
    bambuddy: BambuddyClient,
) -> None:
    """``/api/v1/printers`` is a 404 on 1.2.5.5; only the trailing-slash form exists."""
    route = respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=recording("printers.json"))
    )

    printers = await bambuddy.printers()

    assert route.called
    assert [(printer.id, printer.name, printer.model) for printer in printers] == [
        (1, "3DP-31B-598", "H2C")
    ]
    assert printers[0].nozzle_count == 2
    assert route.calls.last.request.headers["X-API-Key"] == "s3cret"


@respx.mock
async def test_the_slashless_printers_path_is_never_called(bambuddy: BambuddyClient) -> None:
    slashless = respx.get(f"{API}/printers").mock(
        return_value=httpx.Response(404, json=recording("printers-no-trailing-slash-404.json"))
    )
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=recording("printers.json"))
    )

    await bambuddy.printers()

    assert not slashless.called


@respx.mock
async def test_folders_and_links_are_bare_lists_and_pipelines_are_wrapped(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/library/folders").mock(
        return_value=httpx.Response(200, json=recording("library-folders.json"))
    )
    respx.get(f"{API}/slicer-pipelines/").mock(
        return_value=httpx.Response(200, json=recording("slicer-pipelines.json"))
    )
    respx.get(f"{API}/external-links/").mock(
        return_value=httpx.Response(200, json=recording("external-links.json"))
    )

    folders = await bambuddy.folders()
    assert [(folder.id, folder.name) for folder in folders] == [(1, "MakerWorld"), (2, "Raegan")]
    assert await bambuddy.pipelines() == []
    assert await bambuddy.external_links() == []


@respx.mock
async def test_presets_keep_their_tiers(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/slicer/presets").mock(
        return_value=httpx.Response(200, json=recording("slicer-presets.json"))
    )

    catalogue = await bambuddy.presets()

    assert catalogue.cloud_status == "ok"
    assert catalogue.orca_cloud_status == "not_authenticated"
    assert catalogue.cloud.printer[0].id == "GM030"
    assert catalogue.local.printer == []


@respx.mock
async def test_upload_posts_multipart_to_the_configured_folder(bambuddy: BambuddyClient) -> None:
    route = respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 41,
                "filename": "keychain-elan.3mf",
                "file_type": "3mf",
                "file_size": 4096,
                "thumbnail_path": None,
                "duplicate_of": None,
                "metadata": {},
            },
        )
    )

    uploaded = await bambuddy.upload_library_file(
        "keychain-elan.3mf", b"PK\x03\x04payload", folder_id=2
    )

    assert uploaded.id == 41
    request = route.calls.last.request
    assert request.url.params["folder_id"] == "2"
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b"keychain-elan.3mf" in request.content


@respx.mock
async def test_upload_without_a_folder_sends_no_folder_id(bambuddy: BambuddyClient) -> None:
    route = respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 7,
                "filename": "a.3mf",
                "file_type": "3mf",
                "file_size": 1,
                "thumbnail_path": None,
            },
        )
    )

    await bambuddy.upload_library_file("a.3mf", b"x")

    assert "folder_id" not in route.calls.last.request.url.params


@respx.mock
async def test_slice_sends_plate_not_plate_id(bambuddy: BambuddyClient) -> None:
    """``plate`` on the slice route, ``plate_id`` on the queue route — the two differ."""
    route = respx.post(f"{API}/library/files/41/slice").mock(
        return_value=httpx.Response(
            202, json={"job_id": 9, "status": "pending", "status_url": "/api/v1/slice-jobs/9"}
        )
    )

    accepted = await bambuddy.slice(
        41,
        SliceRequest(
            printer_preset=PresetRef(source="cloud", id="GM041"),
            process_preset=PresetRef(source="cloud", id="GP252"),
            filament_presets=[
                PresetRef(source="cloud", id="GFSA05_22"),
                PresetRef(source="cloud", id="GFSA00_22"),
            ],
            filament_colours=["#0047BB", "#F5547C"],
            bed_type="Textured PEI Plate",
        ),
    )

    assert accepted.job_id == 9
    body = route.calls.last.request.read()
    assert b'"plate":1' in body.replace(b" ", b"")
    assert b"plate_id" not in body
    assert b'"filament_colours"' in body


@respx.mock
async def test_enqueue_sends_the_enum_calibration_flags(bambuddy: BambuddyClient) -> None:
    """``bed_levelling``/``flow_cali`` are ``off|on|auto`` on this API, not booleans."""
    route = respx.post(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue-item.json"))
    )

    item = await bambuddy.enqueue(
        QueueItemCreate(
            printer_id=1,
            library_file_id=52,
            quantity=3,
            plate_id=1,
            bed_levelling="off",
            flow_cali="off",
        )
    )

    assert item.id == 9
    sent = respx.calls.last.request.read()
    import json as _json

    payload = _json.loads(sent)
    assert payload["bed_levelling"] == "off"
    assert payload["flow_cali"] == "off"
    assert payload["plate_id"] == 1
    assert payload["use_ams"] is True
    assert payload["quantity"] == 3
    assert route.called


@respx.mock
async def test_run_pipeline_passes_copies_and_force(bambuddy: BambuddyClient) -> None:
    route = respx.post(f"{API}/slicer-pipelines/4/run").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": 12,
                "pipeline_id": 4,
                "source_library_file_id": 41,
                "copies": 2,
                "status": "queued",
                "slice_job_id": None,
                "sliced_library_file_id": None,
                "eligibility_overridden": False,
                "created_by": None,
                "created_at": "2026-09-23T01:00:00Z",
                "started_at": None,
                "completed_at": None,
            },
        )
    )

    run = await bambuddy.run_pipeline(4, PipelineRunRequest(source_library_file_id=41, copies=2))

    assert (run.id, run.status, run.copies) == (12, "queued", 2)
    import json as _json

    assert _json.loads(route.calls.last.request.read()) == {
        "source_library_file_id": 41,
        "copies": 2,
        "force": False,
    }


@respx.mock
async def test_await_slice_polls_until_it_completes(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/slice-jobs/9").mock(
        side_effect=[
            httpx.Response(200, json={"id": 9, "status": "pending"}),
            httpx.Response(200, json={"id": 9, "status": "running"}),
            httpx.Response(
                200,
                json={
                    "id": 9,
                    "status": "completed",
                    "result": {
                        "library_file_id": 52,
                        "name": "keychain-elan.gcode.3mf",
                        "print_time_seconds": 1597,
                        "filament_used_g": 9.06,
                    },
                },
            ),
        ]
    )

    job = await bambuddy.await_slice(9)

    assert job.status == "completed"
    assert job.result is not None and job.result.library_file_id == 52


@respx.mock
async def test_await_slice_gives_up_rather_than_polling_for_ever(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(200, json={"id": 9, "status": "running"})
    )
    bambuddy.config = type(bambuddy.config)(
        base_url=BASE_URL, api_key="k", slice_timeout=0.0, slice_poll_interval=0.0
    )

    with pytest.raises(ApiError) as caught:
        await bambuddy.await_slice(9)

    assert caught.value.status == 504


@respx.mock
async def test_external_link_upsert_uses_patch_for_an_existing_row(
    bambuddy: BambuddyClient,
) -> None:
    created = {
        "id": 3,
        "name": "Customize",
        "url": "https://scadbuddy.test",
        "icon": "shapes",
        "open_in_new_tab": False,
        "sort_order": 0,
        "created_at": "2026-09-23T01:00:00Z",
        "updated_at": "2026-09-23T01:00:00Z",
    }
    post = respx.post(f"{API}/external-links/").mock(return_value=httpx.Response(200, json=created))
    patch = respx.patch(f"{API}/external-links/3").mock(
        return_value=httpx.Response(200, json=created)
    )

    link = await bambuddy.create_external_link(
        name="Customize", url="https://scadbuddy.test", icon="shapes"
    )
    assert (link.id, link.icon, link.open_in_new_tab) == (3, "shapes", False)
    assert post.called

    await bambuddy.update_external_link(3, url="https://scadbuddy.test/other")
    import json as _json

    assert _json.loads(patch.calls.last.request.read()) == {"url": "https://scadbuddy.test/other"}


# --- error mapping -----------------------------------------------------------------


@pytest.mark.parametrize("code", [401, 403])
@respx.mock
async def test_a_refused_key_names_the_scope_it_needs(bambuddy: BambuddyClient, code: int) -> None:
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(code, json={"detail": "Not authenticated"})
    )

    with pytest.raises(ApiError) as caught:
        await bambuddy.printers()

    error = caught.value
    assert error.status == 409
    assert error.type == SCOPE_PROBLEM
    assert Scope.READ_STATUS.value in error.detail
    assert error.extensions["required_scope"] == Scope.READ_STATUS.value
    assert error.extensions["bambuddy_status"] == code
    assert "Not authenticated" in error.detail


@respx.mock
async def test_the_library_calls_name_the_manage_library_scope(
    bambuddy: BambuddyClient,
) -> None:
    respx.post(f"{API}/library/files").mock(return_value=httpx.Response(403, json={}))

    with pytest.raises(ApiError) as caught:
        await bambuddy.upload_library_file("a.3mf", b"x")

    assert Scope.MANAGE_LIBRARY.value in caught.value.detail


@respx.mock
async def test_the_queue_calls_name_the_manage_queue_scope(bambuddy: BambuddyClient) -> None:
    respx.post(f"{API}/queue/").mock(return_value=httpx.Response(401, json={}))

    with pytest.raises(ApiError) as caught:
        await bambuddy.enqueue(QueueItemCreate(printer_id=1, library_file_id=2))

    assert Scope.MANAGE_QUEUE.value in caught.value.detail


@respx.mock
async def test_a_404_stays_a_404(bambuddy: BambuddyClient) -> None:
    respx.delete(f"{API}/library/files/41").mock(
        return_value=httpx.Response(404, json={"detail": "Not found"})
    )

    with pytest.raises(ApiError) as caught:
        await bambuddy.delete_library_file(41)

    assert caught.value.status == 404
    assert caught.value.type == NOT_FOUND_PROBLEM


@respx.mock
async def test_a_409_carries_the_eligibility_report_verbatim(bambuddy: BambuddyClient) -> None:
    report = {
        "ok": False,
        "target_kind": "specific_printer",
        "target_printer_id": 1,
        "target_printer_name": "3DP-31B-598",
        "issues": [
            {"kind": "filament_type_mismatch", "slot_index": 0, "expected": "PLA", "actual": "PETG"}
        ],
    }
    respx.post(f"{API}/slicer-pipelines/4/run").mock(return_value=httpx.Response(409, json=report))

    with pytest.raises(ApiError) as caught:
        await bambuddy.run_pipeline(4, PipelineRunRequest(source_library_file_id=41))

    assert caught.value.status == 409
    assert caught.value.type == ELIGIBILITY_PROBLEM
    assert caught.value.extensions["bambuddy_body"] == report


@respx.mock
async def test_a_422_is_passed_through_with_its_body(bambuddy: BambuddyClient) -> None:
    detail = {"detail": [{"loc": ["body", "plate"], "msg": "input should be >= 0"}]}
    respx.post(f"{API}/queue/").mock(return_value=httpx.Response(422, json=detail))

    with pytest.raises(ApiError) as caught:
        await bambuddy.enqueue(QueueItemCreate(printer_id=1, library_file_id=2))

    assert caught.value.status == 422
    assert caught.value.type == REJECTED_PROBLEM
    assert caught.value.extensions["bambuddy_body"] == detail


@respx.mock
async def test_a_500_becomes_a_bad_gateway(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/printers/").mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(ApiError) as caught:
        await bambuddy.printers()

    assert caught.value.status == 502
    assert caught.value.type == UNAVAILABLE_PROBLEM


@respx.mock
async def test_an_unreachable_bambuddy_is_a_bad_gateway(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/printers/").mock(side_effect=httpx.ConnectError("no route to host"))

    with pytest.raises(ApiError) as caught:
        await bambuddy.printers()

    assert caught.value.status == 502


@respx.mock
async def test_a_timeout_is_a_gateway_timeout(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/printers/").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(ApiError) as caught:
        await bambuddy.printers()

    assert caught.value.status == 504


@respx.mock
async def test_a_wrapped_list_where_a_bare_one_belongs_is_a_bad_gateway(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/printers/").mock(return_value=httpx.Response(200, json={"printers": []}))

    with pytest.raises(ApiError) as caught:
        await bambuddy.printers()

    assert caught.value.status == 502
