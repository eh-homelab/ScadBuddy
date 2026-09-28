"""Spool-first printing (spec 2026-09-27 §4) — ``POST /print/outputs/{id}/run``.

The run takes the dialog's choices (spools, nozzles, quality, plate), resolves every
slicer preset from them, and always slices then queues. There is no pipeline on this
path. The GET bodies are recordings; the slice job and the queue item are built from
Bambuddy's own ``openapi.json`` (see ``tests/bambuddy/recordings/README.md``).

The helpers here are what every other run test imports, so they live in the module
that owns the route's behaviour.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import httpx
import pytest
import respx
import trimesh
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.render.bambu3mf import PlateParts, write_plates_3mf
from scadbuddy.render.split import ColourPart
from tests.api.conftest import wait_for_job
from tests.api.test_print import printers_route
from tests.api.test_print_choices import MIXED_ARCHIVES, h2c_presets
from tests.api.test_print_filaments import (
    inventory_routes,
    prepared,
    queue_route,
    slice_routes,
)
from tests.api.test_send import (
    BASE,
    _uploaded_3mf,
    _uploaded_nozzle,
    configure,
    make_output,
    upload_route,
)
from tests.bambuddy.conftest import recording
from tests.test_bambu3mf import add_plate

# A run and the dialog both read or record the output's upload copies (#316), which
# live in Postgres.
pytestmark = pytest.mark.requires_postgres

API = f"{BASE}/api/v1"


def spool_preset_routes() -> None:
    respx.route(method="GET", path__regex=r"/api/v1/inventory/spools/\d+/filament-presets").mock(
        return_value=httpx.Response(200, json=[])
    )


def hardware_routes(printer_id: int = 1) -> None:
    """The rack (a 0.2 and six 0.4s) and the archives, whose last plate is Textured PEI."""
    respx.get(f"{API}/printers/{printer_id}/status").mock(
        return_value=httpx.Response(200, json=recording("printer-status-rack.json"))
    )
    respx.get(f"{API}/archives/").mock(
        return_value=httpx.Response(200, json=recording("archives.json"))
    )


def run_routes(*, printer_id: int = 1) -> None:
    """Everything a run reads before it slices, except the upload. ``printer_id`` is
    listed as an H2C alongside the recorded printer, since the run refuses any other."""
    rows = recording("printers.json")
    if all(row["id"] != printer_id for row in rows):
        rows.append({**rows[0], "id": printer_id, "name": f"H2C {printer_id}"})
    respx.get(f"{API}/printers/").mock(return_value=httpx.Response(200, json=rows))
    inventory_routes(printer_id=printer_id)
    h2c_presets()
    spool_preset_routes()
    hardware_routes(printer_id)


def body(**choices: Any) -> dict[str, Any]:
    return {
        "printer_id": 1,
        "copies": 1,
        "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}, {"slot_id": 2, "spool_id": 5}]},
        "choices": {"nozzles": [{"size": "0.2"}], "tier": "fine", **choices},
    }


def _slot_1(spool_id: int) -> dict[str, Any]:
    """Slot 1 from ``spool_id``. The recorded requirements list a slot 2 as well, which
    the one-colour test model has no filament for, so its shelf spool is never pinned."""
    return {"slots": [{"slot_id": 1, "spool_id": spool_id}, {"slot_id": 2, "spool_id": 5}]}


def on_spool(spool_id: int, **choices: Any) -> dict[str, Any]:
    """:func:`body` printing slot 1 from ``spool_id`` (#469). Spool 9 rests on the
    right (the 0.2), spool 10 on the left (the 0.4) and spool 5 is on the shelf."""
    return {**body(**choices), "filament_plan": _slot_1(spool_id)}


def run_request(**extra: Any) -> dict[str, Any]:
    """:func:`body` with request fields (not choices) replaced or added; ``None`` drops."""
    request = {**body(), **extra}
    return {key: value for key, value in request.items() if value is not None}


@respx.mock
def test_choices_slice_with_derived_presets_and_queue_without_a_pipeline(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()
    queued = queue_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=body())

    assert response.status_code == 200, response.text
    assert response.json()["route"] == "slice_queue"
    slice_body = json.loads(sliced.calls.last.request.content)
    assert slice_body["printer_preset"] == {"source": "cloud", "id": "GM042"}
    assert slice_body["process_preset"] == {"source": "cloud", "id": "GP243"}
    assert slice_body["bed_type"] == "Textured PEI Plate"
    sent = json.loads(queued.calls.last.request.content)
    assert sent["printer_id"] == 1
    assert sent.get("ams_mapping") is None
    assert sent.get("nozzle_rack_choice") is None
    assert all("/slicer-pipelines" not in str(call.request.url) for call in respx.calls)


@respx.mock
def test_a_resolver_error_is_a_422_before_anything_is_sliced(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json=body(nozzles=[{"size": "0.2"}, {"size": "0.4"}]),
    )

    assert response.status_code == 422
    assert "different sizes" in response.json()["detail"]
    assert not sliced.called


@respx.mock
@pytest.mark.parametrize(
    ("choices", "detail"),
    [
        ({"nozzles": [{"size": "0.2"}, {"size": "0.4"}]}, "different sizes"),
        ({"process_name": "No Such Process @BBL H2C"}, "is not a process"),
    ],
    ids=["mixed-sizes", "unknown-process"],
)
def test_a_choice_the_resolver_refuses_uploads_nothing(
    client: TestClient, model: str, choices: dict[str, Any], detail: str
) -> None:
    """A refusal that the choices alone decide is made before the 3MF reaches Bambuddy's
    library, so iterating through invalid nozzle combinations leaves no uploads behind."""
    output_id = prepared(client, model)
    uploaded = upload_route()
    run_routes()
    sliced = slice_routes()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=body(**choices))

    assert response.status_code == 422
    assert detail in response.json()["detail"]
    assert not uploaded.called
    assert not sliced.called


@respx.mock
def test_every_slot_refused_is_a_422_with_nothing_sliced(client: TestClient, model: str) -> None:
    """No slot resolves, so ``filament_presets`` is empty: the run refuses rather than
    ever handing Bambuddy an empty (or ``null``-padded) preset array."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={**body(), "filament_plan": {"slots": []}},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "Slot 1 has no spool chosen." in detail
    assert "Slot 2 has no spool chosen." in detail
    assert not sliced.called


@respx.mock
def test_high_flow_slices_with_bambus_standard_preset_and_says_so(
    client: TestClient, model: str
) -> None:
    """Spec §4.1: Bambuddy refuses a ScadBuddy-made printer preset, so High Flow slices
    as Bambu's own 0.4 preset, and the result carries the warning."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json=on_spool(10, nozzles=[{"size": "0.4", "flow": "high_flow"}, {"size": "0.4"}]),
    )

    assert response.status_code == 200, response.text
    assert json.loads(sliced.calls.last.request.content)["printer_preset"] == {
        "source": "cloud",
        "id": "GM041",
    }
    assert "hf-unsupported" in {warning["kind"] for warning in response.json()["warnings"]}


@respx.mock
def test_a_plate_other_than_the_last_prints_is_warned_about(client: TestClient, model: str) -> None:
    """archives.json's last print on printer 1 used Textured PEI."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=body(bed_type="Supertack Plate")
    )

    assert response.status_code == 200, response.text
    assert json.loads(sliced.calls.last.request.content)["bed_type"] == "Supertack Plate"
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "plate-differs"]
    assert "Textured PEI Plate" in warning["message"]
    assert "Supertack Plate" in warning["message"]


@respx.mock
def test_the_last_prints_plate_says_nothing_about_the_plate(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    slice_routes()
    queue_route()

    warnings = client.post(f"/api/v1/print/outputs/{output_id}/run", json=body()).json()["warnings"]

    assert not [w for w in warnings if w["kind"] in {"plate-differs", "not-installed"}]


@respx.mock
def test_a_nozzle_size_the_rack_lacks_is_warned_about(client: TestClient, model: str) -> None:
    """The recorded rack holds a 0.2 and 0.4s, but no 0.6. With both mounted nozzles
    reported, a 0.6 run is refused outright (#469), so this is the case where the
    printer reports its rack but not what is mounted."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    status = recording("printer-status-rack.json")
    status["nozzles"] = []
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=status))
    slice_routes()
    queue_route()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json=on_spool(5, nozzles=[{"size": "0.6"}], tier="standard"),
    )

    assert response.status_code == 200, response.text
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "not-installed"]
    assert "0.6 mm" in warning["message"]


@respx.mock
def test_an_unreadable_printer_still_prints_without_hardware_warnings(
    client: TestClient, model: str
) -> None:
    """An offline printer's status and archives are unknown, not a failure (spec §3)."""
    output_id = prepared(client, model)
    upload_route()
    printers_route()
    inventory_routes()
    h2c_presets()
    spool_preset_routes()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))
    respx.get(f"{API}/archives/").mock(return_value=httpx.Response(503))
    slice_routes()
    queue_route()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json=body(nozzles=[{"size": "0.6"}], tier="standard", bed_type="Supertack Plate"),
    )

    assert response.status_code == 200, response.text
    assert not [
        w for w in response.json()["warnings"] if w["kind"] in {"plate-differs", "not-installed"}
    ]


@respx.mock
def test_archives_with_null_and_mixed_timestamps_still_run_and_compare_the_plate(
    client: TestClient, model: str
) -> None:
    """PR #335 review 3: the plate warning is advisory, so odd archive timestamps must
    neither 500 the run nor lose the comparison."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    respx.get(f"{API}/archives/").mock(return_value=httpx.Response(200, json=MIXED_ARCHIVES))
    slice_routes()
    queue_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=body())

    assert response.status_code == 200, response.text
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "plate-differs"]
    assert "Supertack Plate" in warning["message"]


@respx.mock
def test_an_unknown_advanced_override_is_a_422_naming_the_slot_before_slicing(
    client: TestClient, model: str
) -> None:
    """PR #335 review 2: an override is checked against the catalogue like every other
    preset, rather than failing later inside Bambuddy's slice."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json=body(filament_overrides={"2": {"source": "cloud", "id": "GONE404"}}),
    )

    assert response.status_code == 422
    assert "slot 2" in response.json()["detail"]
    assert not sliced.called


# --- the queue side: moved here from test_print_filaments.py (#87) --------------------


@respx.mock
def test_a_plan_is_sliced_and_queued_with_the_mapping_on_the_wire(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()
    queued = queue_route()

    # Spool 9 is loaded in AMS 0 tray 1 (flat tray 1); spool 5 is on the shelf.
    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=run_request(copies=3))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["route"] == "slice_queue"
    assert result["queue_item_ids"] == [51]
    assert result["slice_job_id"] == 9
    assert result["printer_id"] == 1

    sent: dict[str, Any] = json.loads(queued.calls.last.request.content)
    assert sent["printer_id"] == 1
    assert sent["library_file_id"] == 77
    assert sent["quantity"] == 3
    # No ams_mapping: Bambuddy computes it from the overrides, against the printer it
    # is actually dispatching to and the filament switcher that printer actually has.
    assert sent.get("ams_mapping") is None
    assert sent["required_filament_types"] == ["PETG", "PLA"]
    assert [override["slot_id"] for override in sent["filament_overrides"]] == [1, 2]

    # The sliced file records the colours that will actually print (Bambuddy #2977).
    slice_body: dict[str, Any] = json.loads(sliced.calls.last.request.content)
    assert slice_body["filament_colours"] == ["#688197", "#0047BB"]
    # Both slots are used, so this is a dense (unpadded) array — slot 1's preset at
    # index 0, slot 2's at index 1 (the next test covers the padded case).
    assert slice_body["filament_presets"] == [
        {"source": "cloud", "id": "GFSG00_24"},
        {"source": "cloud", "id": "GFSA05_21"},
    ]


@respx.mock
def test_all_plates_pads_a_plate_that_uses_only_slot_2(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Plate 2 of an all_plates run using only slot 2 must still slice a
    length-2 ``filament_presets`` array with slot 2's spool at index 1 — not a length-1
    array that slices the wrong preset onto the wrong filament."""
    configure(client)
    output_id = make_output(client, model)
    [output_3mf] = paths.outputs.glob(f"*/{output_id}/model.3mf")
    add_plate(output_3mf, 2)
    upload_route()
    printers_route()
    h2c_presets()
    spool_preset_routes()
    hardware_routes()
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=recording("inventory-spools.json"))
    )
    respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
    )
    respx.get(f"{API}/printers/1").mock(
        return_value=httpx.Response(200, json=recording("printer.json"))
    )
    respx.get(f"{API}/printers/1/inventory-remain").mock(
        return_value=httpx.Response(200, json=recording("inventory-remain.json"))
    )
    # Plate 1 uses both slots (the recording); plate 2 uses only slot 2, the padding
    # case this fix covers. `run_for_output` reads plate 1 then plate 2, in order.
    respx.route(method="GET", path__regex=r"/api/v1/library/files/\d+/filament-requirements").mock(
        side_effect=[
            httpx.Response(200, json=recording("filament-requirements.json")),
            httpx.Response(
                200,
                json={
                    "file_id": 41,
                    "filename": "plate.3mf",
                    "plate_id": 2,
                    "filaments": [
                        {
                            "slot_id": 1,
                            "type": "",
                            "color": "#0047BB",
                            "used_grams": 0,
                            "used_meters": 0,
                            "used_in_plate": False,
                        },
                        {
                            "slot_id": 2,
                            "type": "",
                            "color": "#FF1493",
                            "used_grams": 0,
                            "used_meters": 0,
                            "used_in_plate": True,
                        },
                    ],
                },
            ),
        ]
    )
    sliced = slice_routes()
    queue_route()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(all_plates=True)
    )

    assert response.status_code == 200, response.text
    bodies = [json.loads(call.request.content) for call in sliced.calls]
    assert [slice_body["plate"] for slice_body in bodies] == [1, 2]
    # Plate 1 is dense (both slots used).
    assert bodies[0]["filament_presets"] == [
        {"source": "cloud", "id": "GFSG00_24"},
        {"source": "cloud", "id": "GFSA05_21"},
    ]
    # Plate 2 uses only slot 2: still a length-2 array, spool 5's preset at index 1,
    # and the unused slot 1 padded with it rather than compacted away.
    assert bodies[1]["filament_presets"] == [
        {"source": "cloud", "id": "GFSA05_21"},
        {"source": "cloud", "id": "GFSA05_21"},
    ]
    assert bodies[1]["filament_colours"] == ["#FFFFFF", "#0047BB"]


@respx.mock
def test_the_plan_carries_scadbuddys_own_warnings_back(client: TestClient, model: str) -> None:
    """A spool on the shelf is a legitimate choice; the answer says to load it rather
    than refusing."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    slice_routes()
    queue_route()

    result = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json=run_request(
            filament_plan={"slots": [{"slot_id": 1, "spool_id": 5}, {"slot_id": 2, "spool_id": 9}]}
        ),
    ).json()
    assert any(warning["kind"] == "not-loaded" for warning in result["warnings"])


@respx.mock
def test_a_failed_slice_reports_bambuddys_own_words(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    respx.route(method="POST", path__regex=r"/api/v1/library/files/\d+/slice").mock(
        return_value=httpx.Response(200, json={"job_id": 9, "status": "pending"})
    )
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(
            200, json={"id": 9, "status": "failed", "error": "object outside the build plate"}
        )
    )
    queued = queue_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=body())
    assert response.status_code == 502
    assert "object outside the build plate" in response.json()["detail"]
    assert not queued.called


@pytest.mark.parametrize("plate_id", [0, -1])
def test_a_plate_below_one_is_rejected(client: TestClient, model: str, plate_id: int) -> None:
    """Bambuddy's plates are 1-based; a 0 would slice the wrong plate silently."""
    configure(client)
    response = client.post(
        "/api/v1/print/outputs/0123456789abcdef0123456789abcdef/run",
        json=run_request(plate_id=plate_id),
    )
    assert response.status_code == 422


# --- the printer and the upload: moved here from test_print.py (#86, #126) ----------


@respx.mock
def test_run_with_no_printer_anywhere_says_so_rather_than_guessing(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload = upload_route()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(printer_id=None)
    )

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert "printer" in response.json()["detail"]
    assert not upload.called


@respx.mock
def test_the_run_route_uploads_the_3mf_when_the_output_was_never_sent(
    client: TestClient, model: str
) -> None:
    """Printing straight from the picker, without pressing Send first."""
    configure(client)
    run_routes()
    job_id = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}}).json()[
        "job_id"
    ]
    wait_for_job(client, job_id)
    output_id = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id}).json()["id"]
    upload = upload_route()
    slice_routes()
    queue_route()

    result = client.post(f"/api/v1/print/outputs/{output_id}/run", json=body()).json()

    assert upload.called
    assert result["library_file_id"] == 41


@respx.mock
def test_the_chosen_nozzle_is_stated_in_the_upload_and_a_change_re_uploads(
    client: TestClient, model: str
) -> None:
    """#126: the 3MF states the nozzle it is printed with — now the dialog's choice —
    and since the plate is the same, it is the nozzle alone that forces the re-upload."""
    output_id = prepared(client, model)
    upload = upload_route()
    respx.delete(f"{API}/library/files/41").mock(return_value=httpx.Response(200, json={}))
    run_routes()
    slice_routes()
    queue_route()

    # The shelf spool has no side, so neither run is refused or pinned and the nozzle
    # really is the only thing that differs (#469).
    client.post(f"/api/v1/print/outputs/{output_id}/run", json=on_spool(5))
    assert upload.call_count == 1
    assert _uploaded_nozzle(upload) == ["0.2"]

    client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json=on_spool(5, nozzles=[{"size": "0.4"}], tier="standard"),
    )

    assert upload.call_count == 2, "the 0.4 run reused a file stating the 0.2 nozzle"
    assert _uploaded_nozzle(upload) == ["0.4"]


def _uploaded_colours(route: respx.Route) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(route))) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    colours: list[str] = settings["filament_colour"]
    return colours


@respx.mock
def test_the_upload_is_in_the_chosen_spools_colours_and_a_swap_re_uploads(
    client: TestClient, model: str
) -> None:
    """#476: the slicer keeps the file's cover image, so the file has to be in the spools'
    colours, not the model's, or the queue shows a print that will not come out."""
    output_id = prepared(client, model)
    upload = upload_route()
    respx.delete(f"{API}/library/files/41").mock(return_value=httpx.Response(200, json={}))
    run_routes()
    slice_routes()
    queue_route()

    client.post(f"/api/v1/print/outputs/{output_id}/run", json=body())
    # The test model has one filament, printed from slot 1's spool: spool 9 is 688197FF
    # (inventory-spools.json), where the model's own colour is #FF0000.
    assert _uploaded_colours(upload) == ["#688197"]

    swapped = run_request(
        filament_plan={"slots": [{"slot_id": 1, "spool_id": 5}, {"slot_id": 2, "spool_id": 9}]}
    )
    client.post(f"/api/v1/print/outputs/{output_id}/run", json=swapped)

    assert upload.call_count == 2, "the swapped run reused a file in the other colours"
    assert _uploaded_colours(upload) == ["#0047BB"]


# --- final review 1: a slot only a later plate uses ------------------------------------


def split_plates_routes() -> None:
    """Plate 1 uses only slot 1 and plate 2 only slot 2, answered by the ``plate_id``
    each read asks for rather than by call order."""

    def answer(request: httpx.Request) -> httpx.Response:
        plate = int(request.url.params.get("plate_id", "1"))
        base = recording("filament-requirements.json")
        for filament in base["filaments"]:
            filament["used_in_plate"] = filament["slot_id"] == plate
        return httpx.Response(200, json={**base, "plate_id": plate})

    respx.route(method="GET", path__regex=r"/api/v1/library/files/\d+/filament-requirements").mock(
        side_effect=answer
    )
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=recording("inventory-spools.json"))
    )
    respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
    )
    respx.get(f"{API}/printers/1").mock(
        return_value=httpx.Response(200, json=recording("printer.json"))
    )
    respx.get(f"{API}/printers/1/inventory-remain").mock(
        return_value=httpx.Response(200, json=recording("inventory-remain.json"))
    )


def two_plate_output(client: TestClient, model: str, paths: DataPaths) -> str:
    configure(client)
    output_id = make_output(client, model)
    [output_3mf] = paths.outputs.glob(f"*/{output_id}/model.3mf")
    add_plate(output_3mf, 2)
    return output_id


@respx.mock
def test_the_all_plates_filament_read_offers_every_plates_slots(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Spec §2 step 1: one row per colour in the model. Plate 1 alone would never show
    slot 2, which only plate 2 uses."""
    output_id = two_plate_output(client, model, paths)
    upload_route()
    split_plates_routes()
    hardware_routes()

    one = client.get(f"/api/v1/print/outputs/{output_id}/filaments?printer_id=1").json()
    every = client.get(
        f"/api/v1/print/outputs/{output_id}/filaments?printer_id=1&all_plates=true"
    ).json()

    assert [slot["slot_id"] for slot in one["slots"]] == [1]
    assert [slot["slot_id"] for slot in every["slots"]] == [1, 2]
    assert {choice["slot_id"] for choice in every["suggested"]} == {1, 2}


@respx.mock
def test_all_plates_with_no_spool_for_a_later_plates_slot_is_a_422_naming_it(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = two_plate_output(client, model, paths)
    upload_route()
    printers_route()
    h2c_presets()
    spool_preset_routes()
    hardware_routes()
    split_plates_routes()
    sliced = slice_routes()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json=run_request(
            all_plates=True,
            filament_plan={"slots": [{"slot_id": 1, "spool_id": 9}]},
        ),
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "Plate 2: Slot 2 has no spool chosen."
    assert not sliced.called


@respx.mock
def test_all_plates_with_every_slot_chosen_slices_each_plate(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = two_plate_output(client, model, paths)
    upload_route()
    printers_route()
    h2c_presets()
    spool_preset_routes()
    hardware_routes()
    split_plates_routes()
    sliced = slice_routes()
    queue_route()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(all_plates=True)
    )

    assert response.status_code == 200, response.text
    bodies = [json.loads(call.request.content) for call in sliced.calls]
    assert [slice_body["plate"] for slice_body in bodies] == [1, 2]


# --- final review 2: the run refuses a printer it cannot resolve presets for ----------


@respx.mock
def test_a_printer_bambuddy_does_not_know_is_a_422_before_anything_is_uploaded(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload = upload_route()
    run_routes()
    sliced = slice_routes()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=run_request(printer_id=7))

    assert response.status_code == 422, response.text
    assert "printer 7" in response.json()["detail"]
    assert not upload.called
    assert not sliced.called


@respx.mock
def test_a_printer_that_is_not_an_h2c_is_a_422_before_anything_is_sliced(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(
            200, json=[{"id": 1, "name": "Workshop", "model": "X1C", "is_active": True}]
        )
    )
    sliced = slice_routes()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=body())

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "ScadBuddy can only choose slicer presets for a Bambu Lab H2C so far, and "
        "Workshop's model is X1C."
    )
    assert not sliced.called


# --- #469: a print the mounted nozzles would pause is refused before upload -----------
#
# printer-status-rack.json is printer 1 with the Filament Track Switch: the right 0.2
# HS00, the left 0.4 HH01, AMS 0/1 resting on inlet B (right) and AMS 2 and the HT on
# inlet A (left). With the switch any AMS reaches either nozzle. Spool 9 is in AMS 0
# tray 1, spool 10 in AMS 2 tray 0, and spool 5 is on the shelf
# (inventory-assignments.json).


def _uploaded_settings(route: respx.Route) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(route))) as archive:
        settings: dict[str, Any] = json.loads(archive.read("Metadata/project_settings.config"))
    return settings


def _status(**changes: Any) -> None:
    body = recording("printer-status-rack.json")
    body.update(changes)
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=body))


def two_colour_output(
    client: TestClient, model: str, paths: DataPaths, *plates: tuple[int, ...]
) -> str:
    """The test model rewritten in two colours, one plate per entry of ``plates`` naming
    the filaments its parts print with (by default one plate using both)."""
    output_id = prepared(client, model)
    directory = paths.output_dir(model, output_id)
    colours = ["#FF0000", "#0000FF"]
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    meta["colors"] = colours
    (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    box = trimesh.creation.box(extents=(10, 10, 5))
    write_plates_3mf(
        [
            PlateParts(
                tuple(ColourPart(n, f"Color {n}", colours[n - 1], box) for n in extruders),
                extruders,
            )
            for extruders in plates or [(1, 2)]
        ],
        colours,
        directory / "model.3mf",
        thumbnails=None,
        model_name=model,
    )
    return output_id


TWO_SPOOLS = {"slots": [{"slot_id": 1, "spool_id": 9}, {"slot_id": 2, "spool_id": 10}]}
BOTH_02 = [{"nozzle_type": "HS00", "nozzle_diameter": "0.2"}] * 2
#: Printer 1 as if it had no switch: each AMS wired to one side.
WIRED = {
    "ams_extruder_map": {"0": 0, "1": 0, "2": 1, "128": 1},
    "ams_switch_inlet": {},
    "fila_switch": {"installed": False},
}


@respx.mock
def test_with_the_switch_a_spool_resting_on_the_left_prints_in_one_color(
    client: TestClient, model: str
) -> None:
    """Queue item 108's spool under the user's ruling: AMS 2 rests on the left's 0.4, but
    the switch can feed it to the right, so a one-color 0.2 run is not refused. The 3MF
    is not pinned (the slicer ignores that), so the run says the slicer picks the side."""
    output_id = prepared(client, model)
    upload = upload_route()
    run_routes()
    slice_routes()
    queue_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=on_spool(10))

    assert response.status_code == 200, response.text
    assert "filament_map" not in _uploaded_settings(upload)
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "side-unknown"]
    assert warning["message"].startswith("Only the right nozzle is 0.2 mm")


@respx.mock
def test_a_multi_color_print_on_differing_nozzles_is_a_422_before_upload(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Queue item 108 itself: two colors, a 0.2 on the right and a 0.4 on the left. The
    slicer spreads the colors across both nozzles and nothing ScadBuddy writes stops it."""
    output_id = two_colour_output(client, model, paths)
    upload = upload_route()
    run_routes()
    sliced = slice_routes()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={
            **body(),
            "filament_plan": {
                "slots": [{"slot_id": 1, "spool_id": 9}, {"slot_id": 2, "spool_id": 10}]
            },
        },
    )

    assert response.status_code == 422, response.text
    assert "The slicer spreads a multi-color print across both" in response.json()["detail"]
    assert not upload.called
    assert not sliced.called


@respx.mock
def test_a_size_neither_nozzle_has_is_a_422_before_anything_is_uploaded(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload = upload_route()
    run_routes()
    sliced = slice_routes()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json=on_spool(9, nozzles=[{"size": "0.6"}], tier="standard"),
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "Neither nozzle is 0.6 mm: the right has 0.2 mm and the left 0.4 mm. Choose 0.2 or "
        "0.4, or fit a 0.6 mm nozzle."
    )
    assert not upload.called
    assert not sliced.called


@respx.mock
def test_with_both_sides_matching_nothing_is_refused_or_warned(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    _status(nozzles=BOTH_02)
    slice_routes()
    queue_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=on_spool(10))

    assert response.status_code == 200, response.text
    assert "side-unknown" not in {warning["kind"] for warning in response.json()["warnings"]}


@respx.mock
def test_without_the_switch_a_spool_on_the_other_nozzle_is_a_422_before_upload(
    client: TestClient, model: str
) -> None:
    """Without the switch AMS 2 really is wired to the left, where the 0.4 is."""
    output_id = prepared(client, model)
    upload = upload_route()
    run_routes()
    _status(**WIRED)
    sliced = slice_routes()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=on_spool(10))

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "Slot 1's spool (AMS 2, left) is on the 0.4 mm nozzle; this print is sliced for "
        "0.2 mm. Pick a spool on the right, or choose 0.4."
    )
    assert not upload.called
    assert not sliced.called


@respx.mock
def test_without_the_switch_a_shelf_spool_prints_with_the_slicers_side_warned(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload = upload_route()
    run_routes()
    _status(**WIRED)
    slice_routes()
    queue_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=on_spool(5))

    assert response.status_code == 200, response.text
    assert "filament_map" not in _uploaded_settings(upload)
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "side-unknown"]
    assert warning["message"].startswith("Only the right nozzle is 0.2 mm")


@respx.mock
def test_an_unreadable_status_refuses_nothing_and_leaves_the_slicer_to_choose(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload = upload_route()
    run_routes()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))
    slice_routes()
    queue_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=on_spool(10))

    assert response.status_code == 200, response.text
    assert "filament_map" not in _uploaded_settings(upload)
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "side-unknown"]
    assert "couldn't read which nozzles" in warning["message"]


@respx.mock
def test_the_run_reads_the_printer_status_once(client: TestClient, model: str) -> None:
    """Review #8: the refusals and the nozzle warnings come from one snapshot."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    status = respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=recording("printer-status-rack.json"))
    )
    slice_routes()
    queue_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=on_spool(9))

    assert response.status_code == 200, response.text
    assert status.call_count == 1


@respx.mock
def test_a_single_nozzle_printer_prints_a_multi_color_model(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Review of #538: an X1C, P1S or A1 with an AMS prints every colour through its one
    nozzle; Bambuddy reports an empty second nozzle for it."""
    output_id = two_colour_output(client, model, paths)
    upload = upload_route()
    run_routes()
    _status(
        nozzles=[
            {"nozzle_type": "HS00", "nozzle_diameter": "0.2"},
            {"nozzle_type": "", "nozzle_diameter": ""},
        ],
        ams_extruder_map={"0": 0, "1": 0},
        ams_switch_inlet={},
        fila_switch=None,
    )
    slice_routes()
    queue_route()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={**body(), "filament_plan": TWO_SPOOLS}
    )

    assert response.status_code == 200, response.text
    assert upload.called
    assert "side-unknown" not in {warning["kind"] for warning in response.json()["warnings"]}


@respx.mock
def test_a_dual_printer_with_one_nozzle_unreported_warns_rather_than_refuses(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = two_colour_output(client, model, paths)
    upload_route()
    run_routes()
    _status(
        nozzles=[
            {"nozzle_type": "HS00", "nozzle_diameter": "0.2"},
            {"nozzle_type": "HH01", "nozzle_diameter": ""},
        ]
    )
    slice_routes()
    queue_route()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={**body(), "filament_plan": TWO_SPOOLS}
    )

    assert response.status_code == 200, response.text
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "side-unknown"]
    assert "didn't report the left one" in warning["message"]


@respx.mock
def test_a_one_color_plate_of_a_multi_color_model_prints_on_differing_nozzles(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Review of #538: the plate printed decides, not the model. Its one filament goes
    to either nozzle, so the run warns that the slicer picks rather than refusing."""
    output_id = two_colour_output(client, model, paths, (1,))
    upload_route()
    run_routes()
    slice_routes()
    queue_route()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={**body(), "filament_plan": TWO_SPOOLS}
    )

    assert response.status_code == 200, response.text
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "side-unknown"]
    assert warning["message"].startswith("Only the right nozzle is 0.2 mm")


@respx.mock
def test_unreadable_spool_assignments_leave_the_sides_unknown_rather_than_fail(
    client: TestClient, model: str
) -> None:
    """Review of #538: the pre-upload read tolerates ``/inventory/assignments`` failing
    as it does the status; the filament read after the upload still gets it."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    _status(**WIRED)
    assignments = respx.get(f"{API}/inventory/assignments").mock(
        side_effect=[
            httpx.Response(503),
            *[httpx.Response(200, json=recording("inventory-assignments.json"))] * 4,
        ]
    )
    slice_routes()
    queue_route()

    # Spool 10 is wired to the left's 0.4, which a readable assignment would refuse.
    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=on_spool(10))

    assert response.status_code == 200, response.text
    assert assignments.call_count >= 2


@respx.mock
def test_an_unreadable_status_skips_the_spool_assignments(client: TestClient, model: str) -> None:
    """No status, no side can be told, so the pre-upload read isn't made."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))
    assignments = respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
    )
    slice_routes()
    queue_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=on_spool(10))

    assert response.status_code == 200, response.text
    # Only the plate's filament read after the upload.
    assert assignments.call_count == 1


@respx.mock
def test_all_plates_with_a_later_plates_spool_on_the_wrong_side_is_a_422_before_upload(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """claude-review on #538: slot 2 is used only by plate 2, and its spool is wired to
    the left's 0.4. Printing every plate checks it before anything is uploaded."""
    output_id = two_colour_output(client, model, paths, (1,), (2,))
    upload = upload_route()
    printers_route()
    h2c_presets()
    spool_preset_routes()
    hardware_routes()
    split_plates_routes()
    _status(**WIRED)
    sliced = slice_routes()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json=run_request(all_plates=True, filament_plan=TWO_SPOOLS),
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("Slot 2's spool (AMS 2, left) is on the 0.4 mm")
    assert not upload.called
    assert not sliced.called
