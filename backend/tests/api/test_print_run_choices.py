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
import time
import zipfile
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
import trimesh
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
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


def allow_reprints(client: TestClient) -> None:
    """Let the same request print again at once, as it may once ``REPEAT_WINDOW`` has
    passed (#470): for tests that print one output twice on purpose."""
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    state.print_runs.store.repeat_window = timedelta(0)


def follow_run(client: TestClient, run_id: str, *, timeout: float = 60.0) -> dict[str, Any]:
    """Read ``GET /print/runs/{id}`` until the run has ended; its last answer (#470).
    ``timeout`` only catches a run that never ends: a loaded machine takes far longer
    than an idle one, and the test asserts the end, not how soon it came."""
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(f"/api/v1/print/runs/{run_id}")
        assert response.status_code == 200, response.text
        run: dict[str, Any] = response.json()
        if run["status"] != "running":
            return run
        assert time.monotonic() < deadline, f"run {run_id} still running after {timeout}s"
        time.sleep(0.02)


def run_print(client: TestClient, output_id: str, *, json: dict[str, Any]) -> httpx.Response:
    """``POST .../run``, followed to its end, answered as the route did before #470.

    A refusal made before the 202 comes back as it is. A run that succeeds is its
    ``result`` with a 200, and one that failed is its ``error``, with the status that
    error carries, so a test reads the outcome the same way whichever side of the 202
    decided it.
    """
    started: httpx.Response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=json)
    if started.status_code not in (200, 202):
        return started
    run = follow_run(client, started.json()["id"])
    if run["status"] == "succeeded":
        return httpx.Response(200, json=run["result"])
    return httpx.Response(run["error"]["status"], json=run["error"])


@respx.mock
def test_choices_slice_with_derived_presets_and_queue_without_a_pipeline(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()
    queued = queue_route()

    response = run_print(client, output_id, json=body())

    assert response.status_code == 200, response.text
    assert response.json()["route"] == "slice_queue"
    slice_body = json.loads(sliced.calls.last.request.content)
    assert slice_body["printer_preset"] == {"source": "cloud", "id": "GM042"}
    assert slice_body["process_preset"] == {"source": "cloud", "id": "GP243"}
    assert slice_body["bed_type"] == "Textured PEI Plate"
    sent = json.loads(queued.calls.last.request.content)
    assert sent["printer_id"] == 1
    assert sent.get("ams_mapping") is None
    # The recorded requirements carry no group, so nothing prints from the rack (#836).
    assert sent.get("nozzle_rack_choice") is None
    assert all("/slicer-pipelines" not in str(call.request.url) for call in respx.calls)


def declare_print_settings(paths: DataPaths, model: str, settings: dict[str, str]) -> None:
    """Give ``model``'s model.json the ``print_settings`` (#770)."""
    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    meta["print_settings"] = settings
    paths.model_meta(model).write_text(json.dumps(meta), encoding="utf-8")


@respx.mock
def test_a_templates_print_settings_reach_the_slice(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#770: the template's defaults are the slice's process overrides."""
    output_id = prepared(client, model)
    declare_print_settings(paths, model, {"enable_support": "0", "enable_prime_tower": "1"})
    upload_route()
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = run_print(client, output_id, json=body())

    assert response.status_code == 200, response.text
    slice_body = json.loads(sliced.calls.last.request.content)
    assert slice_body["process_overrides"] == {"enable_prime_tower": "1", "enable_support": "0"}
    # The same presets as a template without them: only the overrides differ.
    assert slice_body["process_preset"] == {"source": "cloud", "id": "GP243"}


@respx.mock
def test_the_dialogs_print_sequence_goes_over_the_templates(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#907: the print's own sequence is a process override, and wins over the
    template's default for the same key; the template's other settings stay."""
    output_id = prepared(client, model)
    declare_print_settings(paths, model, {"enable_prime_tower": "1", "print_sequence": "by layer"})
    upload_route()
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = run_print(client, output_id, json={**body(), "print_sequence": "by object"})

    assert response.status_code == 200, response.text
    slice_body = json.loads(sliced.calls.last.request.content)
    assert slice_body["process_overrides"] == {
        "enable_prime_tower": "1",
        "print_sequence": "by object",
    }


@respx.mock
def test_a_print_sequence_without_template_settings_is_the_only_override(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = run_print(client, output_id, json={**body(), "print_sequence": "by object"})

    assert response.status_code == 200, response.text
    assert json.loads(sliced.calls.last.request.content)["process_overrides"] == {
        "print_sequence": "by object"
    }


def test_an_unknown_print_sequence_is_refused(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={**body(), "print_sequence": "by plate"}
    )

    assert response.status_code == 422


@respx.mock
def test_a_template_without_print_settings_sends_no_overrides(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()
    queue_route()

    assert run_print(client, output_id, json=body()).status_code == 200
    assert "process_overrides" not in json.loads(sliced.calls.last.request.content)


@respx.mock
def test_an_unknown_print_setting_is_refused_before_anything_is_sliced(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = prepared(client, model)
    declare_print_settings(paths, model, {"infill": "15%"})
    upload_route()
    run_routes()
    sliced = slice_routes()

    response = run_print(client, output_id, json=body())

    assert response.status_code == 409
    assert "'infill' is not a print setting" in response.json()["detail"]
    assert not sliced.called


@respx.mock
def test_a_resolver_error_is_a_422_before_anything_is_sliced(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()

    response = run_print(
        client,
        output_id,
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

    response = run_print(client, output_id, json=body(**choices))

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

    response = run_print(
        client,
        output_id,
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

    response = run_print(
        client,
        output_id,
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

    response = run_print(client, output_id, json=body(bed_type="Supertack Plate"))

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

    warnings = run_print(client, output_id, json=body()).json()["warnings"]

    assert not [w for w in warnings if w["kind"] in {"plate-differs", "not-installed"}]


@respx.mock
def test_a_nozzle_size_the_rack_lacks_is_warned_about(client: TestClient, model: str) -> None:
    """The recorded rack holds a 0.2 and 0.4s, but no 0.6: the run warns, never refuses."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    status = recording("printer-status-rack.json")
    status["nozzles"] = []
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=status))
    slice_routes()
    queue_route()

    response = run_print(
        client,
        output_id,
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

    response = run_print(
        client,
        output_id,
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

    response = run_print(client, output_id, json=body())

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

    response = run_print(
        client,
        output_id,
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
    response = run_print(client, output_id, json=run_request(copies=3))
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
    # case this fix covers. `execute_run` reads plate 1 then plate 2, in order.
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
            # Then each sliced file's own read for its rack groups (#836), none here.
            httpx.Response(200, json=recording("filament-requirements.json")),
            httpx.Response(200, json=recording("filament-requirements.json")),
        ]
    )
    sliced = slice_routes()
    queue_route()

    response = run_print(client, output_id, json=run_request(all_plates=True))

    assert response.status_code == 200, response.text
    assert all(w["kind"] != "rack-left-to-bambuddy" for w in response.json()["warnings"])
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

    result = run_print(
        client,
        output_id,
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

    response = run_print(client, output_id, json=body())
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

    response = run_print(client, output_id, json=run_request(printer_id=None))

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

    result = run_print(client, output_id, json=body()).json()

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
    run_print(client, output_id, json=on_spool(5))
    assert upload.call_count == 1
    assert _uploaded_nozzle(upload) == ["0.2"]

    run_print(
        client,
        output_id,
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

    run_print(client, output_id, json=body())
    # The test model has one filament, printed from slot 1's spool: spool 9 is 688197FF
    # (inventory-spools.json), where the model's own colour is #FF0000.
    assert _uploaded_colours(upload) == ["#688197"]

    swapped = run_request(
        filament_plan={"slots": [{"slot_id": 1, "spool_id": 5}, {"slot_id": 2, "spool_id": 9}]}
    )
    run_print(client, output_id, json=swapped)

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
def test_the_all_plates_filament_read_reads_the_spools_once(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Two plates, one spool inventory: the spools are the same for every plate, so an
    all-plates read asks for them once (#480)."""
    output_id = two_plate_output(client, model, paths)
    upload_route()
    split_plates_routes()
    hardware_routes()
    before = sum(1 for call in respx.calls if call.request.url.path.endswith("/inventory/spools"))
    response = client.get(
        f"/api/v1/print/outputs/{output_id}/filaments?printer_id=1&all_plates=true"
    )
    assert response.status_code == 200, response.text
    assert [slot["slot_id"] for slot in response.json()["slots"]] == [1, 2]
    reads = sum(1 for call in respx.calls if call.request.url.path.endswith("/inventory/spools"))
    assert reads - before == 1


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

    response = run_print(
        client,
        output_id,
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

    response = run_print(client, output_id, json=run_request(all_plates=True))

    assert response.status_code == 200, response.text
    bodies = [json.loads(call.request.content) for call in sliced.calls]
    assert [slice_body["plate"] for slice_body in bodies] == [1, 2]


@respx.mock
def test_an_all_plates_run_reads_the_assignments_and_printer_once(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The run's filament read shares what every plate has in common, as the dialog's
    does: only each plate's slots are read per plate (#480)."""
    output_id = two_plate_output(client, model, paths)
    upload_route()
    printers_route()
    h2c_presets()
    spool_preset_routes()
    hardware_routes()
    split_plates_routes()
    slice_routes()
    queue_route()

    def reads(suffix: str) -> int:
        return sum(1 for call in respx.calls if call.request.url.path.endswith(suffix))

    response = run_print(client, output_id, json=run_request(all_plates=True))

    assert response.status_code == 200, response.text
    assert reads("/inventory/assignments") == 1
    assert reads("/printers/1/inventory-remain") == 1
    # Each plate's slots once, then each sliced file once more for its rack groups
    # (``choose_rack``, #836): that read is of the slice, which only exists per plate.
    assert reads("/filament-requirements") == 4


# --- final review 2: the run refuses a printer it cannot resolve presets for ----------


@respx.mock
def test_a_printer_bambuddy_does_not_know_is_a_422_before_anything_is_uploaded(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload = upload_route()
    run_routes()
    sliced = slice_routes()

    response = run_print(client, output_id, json=run_request(printer_id=7))

    assert response.status_code == 422, response.text
    assert "printer 7" in response.json()["detail"]
    assert not upload.called
    assert not sliced.called


@respx.mock
def test_a_deactivated_printer_is_a_422_before_anything_is_uploaded(
    client: TestClient, model: str
) -> None:
    """#479: Bambuddy still lists a deactivated printer, and the dialog never offers
    one, so a run naming it is refused before the upload rather than failing after."""
    output_id = prepared(client, model)
    upload = upload_route()
    run_routes()
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(
            200, json=[{"id": 1, "name": "Workshop", "model": "H2C", "is_active": False}]
        )
    )
    sliced = slice_routes()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=body())

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "Workshop is deactivated in Bambuddy. Activate it there, or pick another printer."
    )
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

    response = run_print(client, output_id, json=body())

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "ScadBuddy can only choose slicer presets for a Bambu Lab H2C so far, and "
        "Workshop's model is X1C."
    )
    assert not sliced.called


# --- #768: the run does not check the mounted nozzles ------------------------------------
#
# Measured by the maintainer's test print, 2026-09-29: a two-colour print sliced for
# 0.2 mm printed through the one 0.2 mm nozzle while the other extruder had a different
# size fitted. #538's refusals (#469) are gone.
#
# printer-status-rack.json is printer 1 with the Filament Track Switch: the right 0.2
# HS00, the left 0.4 HH01, AMS 0/1 resting on inlet B (right) and AMS 2 and the HT on
# inlet A (left). Spools 9 and 14 are in AMS 0 (right), spool 10 in AMS 2 (left), and
# spool 5 is on the shelf (inventory-assignments.json).


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


#: Both slots on spools in AMS 0, which feeds (or rests on) the right, the 0.2.
BOTH_ON_RIGHT = {"slots": [{"slot_id": 1, "spool_id": 9}, {"slot_id": 2, "spool_id": 14}]}
#: Slot 2 on spool 10, in AMS 2, which feeds (or rests on) the left, the 0.4.
ONE_ON_LEFT = {"slots": [{"slot_id": 1, "spool_id": 9}, {"slot_id": 2, "spool_id": 10}]}
#: Printer 1 as if it had no switch: each AMS wired to one side.
WIRED = {
    "ams_extruder_map": {"0": 0, "1": 0, "2": 1, "128": 1},
    "ams_switch_inlet": {},
    "fila_switch": {"installed": False},
}
#: Printer 1 as a single-nozzle printer (X1C, P1S, A1): no left nozzle, no switch, and
#: every AMS wired to the one (right) extruder.
NO_LEFT = {
    "nozzles": [{"nozzle_type": "HS00", "nozzle_diameter": "0.4"}],
    "ams_extruder_map": {"0": 0, "1": 0, "2": 0, "128": 0},
    "ams_switch_inlet": {},
    "fila_switch": {"installed": False},
}
#: Printer 1 with a 0.4 on each side, the left High Flow (queue item 149's printer).
BOTH_04_LEFT_HF = {
    "nozzles": [
        {"nozzle_type": "HS01", "nozzle_diameter": "0.4"},
        {"nozzle_type": "HH01", "nozzle_diameter": "0.4"},
    ]
}
#: The kinds the removed nozzle checks warned with; none may come back.
NOZZLE_KINDS = {"side-unknown", "hf-unsupported", "hf-mounted"}


def _two_colour_run(
    client: TestClient,
    model: str,
    paths: DataPaths,
    plan: dict[str, Any],
    **choices: Any,
) -> tuple[httpx.Response, respx.Route]:
    output_id = two_colour_output(client, model, paths)
    upload = upload_route()
    run_routes()
    slice_routes()
    queue_route()
    return run_print(client, output_id, json={**body(**choices), "filament_plan": plan}), upload


def _kinds(response: httpx.Response) -> set[str]:
    return {warning["kind"] for warning in response.json()["warnings"]}


@respx.mock
def test_one_matching_nozzle_prints_two_colours_whose_spools_are_on_its_side(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The maintainer's test print: two colours at 0.2, only the right nozzle 0.2, both
    spools on the right. It prints, with no nozzle warning."""
    _status(**WIRED)
    response, upload = _two_colour_run(client, model, paths, BOTH_ON_RIGHT)

    assert response.status_code == 200, response.text
    assert upload.called
    # Every filament on one extruder, as Bambu Studio saved the test print (#768).
    assert _uploaded_settings(upload)["filament_map"] == ["1", "1"]
    assert not NOZZLE_KINDS & _kinds(response)


@respx.mock
def test_the_file_offers_the_slicer_only_the_side_with_the_nozzle(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#834, queue item 159: the right 0.2 and the left 0.4 High Flow (the recorded
    status). Without this the slicer put a filament on the left as if it were a 0.2,
    and the printer paused."""
    response, upload = _two_colour_run(client, model, paths, ONE_ON_LEFT)

    assert response.status_code == 200, response.text
    settings = _uploaded_settings(upload)
    # The slicer's order: its first extruder is the left, its second the right.
    assert settings["extruder_nozzle_stats"] == ["Standard#0", "Standard#1"]
    assert settings["extruder_nozzle_stats_new"] == ["Standard#0", "Standard#1"]


@respx.mock
def test_an_unreadable_status_leaves_the_slicer_to_choose(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = two_colour_output(client, model, paths)
    upload = upload_route()
    run_routes()
    # After run_routes, whose recorded status it replaces.
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))
    slice_routes()
    queue_route()

    response = run_print(client, output_id, json={**body(), "filament_plan": BOTH_ON_RIGHT})

    assert response.status_code == 200, response.text
    assert "extruder_nozzle_stats" not in _uploaded_settings(upload)


@respx.mock
def test_without_the_switch_a_spool_on_the_other_nozzle_is_not_refused(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Refused before #768 ("Slot 2's spool (AMS 2, left) is on the 0.4 mm nozzle"). The
    run no longer judges a spool by the nozzle mounted on its side."""
    _status(**WIRED)
    response, upload = _two_colour_run(client, model, paths, ONE_ON_LEFT)

    assert response.status_code == 200, response.text
    assert upload.called
    assert not NOZZLE_KINDS & _kinds(response)


@respx.mock
def test_with_the_switch_a_spool_on_the_other_nozzle_is_not_refused(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Queue item 108's plan, which #538 refused as a multi-colour print on differing
    nozzles; with the switch any AMS reaches either nozzle."""
    response, upload = _two_colour_run(client, model, paths, ONE_ON_LEFT)

    assert response.status_code == 200, response.text
    assert upload.called
    assert not NOZZLE_KINDS & _kinds(response)


@respx.mock
def test_a_size_neither_mounted_nozzle_has_is_not_refused(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Refused before #768 ("Neither nozzle is 0.6 mm"). The printer swaps what it needs
    from its rack; the rack-wide ``not-installed`` warning still says when it has none."""
    _status()
    response, _ = _two_colour_run(
        client, model, paths, BOTH_ON_RIGHT, nozzles=[{"size": "0.6"}], tier="standard"
    )

    assert response.status_code == 200, response.text
    assert not NOZZLE_KINDS & _kinds(response)


@respx.mock
def test_a_single_nozzle_printers_other_size_is_not_refused(client: TestClient, model: str) -> None:
    """Refused before #768 ("The nozzle is 0.4 mm, not 0.2 mm")."""
    output_id = prepared(client, model)
    upload = upload_route()
    run_routes()
    _status(**NO_LEFT)
    slice_routes()
    queue_route()

    response = run_print(client, output_id, json=on_spool(9))

    assert response.status_code == 200, response.text
    assert upload.called


HF_LEFT = (
    "The left nozzle is High Flow and this print is sliced for Standard flow (High Flow "
    "slicing isn't supported yet, #484), so if it prints on the left, the printer pauses "
    'at the first layer ("the left nozzle is not matched with slicing file"). Fit a '
    "standard nozzle there before it starts."
)


@respx.mock
def test_a_mounted_high_flow_nozzle_of_the_size_is_warned_about_not_refused(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#723, kept by the owner's ruling on #772: queue item 149 paused on a High Flow
    left sliced as standard. A print may be set up before its nozzle is fitted, so it is
    a warning, and the only mounted-nozzle one left."""
    _status(**BOTH_04_LEFT_HF)
    response, _ = _two_colour_run(
        client, model, paths, BOTH_ON_RIGHT, nozzles=[{"size": "0.4"}], tier="standard"
    )

    assert response.status_code == 200, response.text
    warnings = [(w["kind"], w["message"]) for w in response.json()["warnings"]]
    assert ("hf-mounted", HF_LEFT) in warnings
    assert "side-unknown" not in _kinds(response)


@respx.mock
def test_the_high_flow_warning_changes_nothing_the_run_sends(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#797: the advisory never refuses, and the upload is the one a standard left gets."""
    _status(**{**BOTH_04_LEFT_HF, "nozzles": [BOTH_04_LEFT_HF["nozzles"][0]] * 2})
    _, upload = _two_colour_run(
        client, model, paths, BOTH_ON_RIGHT, nozzles=[{"size": "0.4"}], tier="standard"
    )
    standard = _uploaded_settings(upload)
    _status(**BOTH_04_LEFT_HF)
    response, upload = _two_colour_run(
        client, model, paths, BOTH_ON_RIGHT, nozzles=[{"size": "0.4"}], tier="standard"
    )

    assert response.status_code == 200, response.text
    assert "hf-mounted" in _kinds(response)
    assert _uploaded_settings(upload) == standard


@respx.mock
def test_a_mounted_high_flow_nozzle_of_another_size_says_nothing(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The recorded left is a 0.4 High Flow; a 0.2 print raises no type warning."""
    _status()
    response, _ = _two_colour_run(client, model, paths, BOTH_ON_RIGHT)

    assert response.status_code == 200, response.text
    assert not NOZZLE_KINDS & _kinds(response)


@respx.mock
def test_the_run_reads_the_printer_status_once(client: TestClient, model: str) -> None:
    """Review #8: the hardware warnings come from one snapshot."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    status = respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=recording("printer-status-rack.json"))
    )
    slice_routes()
    queue_route()

    response = run_print(client, output_id, json=on_spool(9))

    assert response.status_code == 200, response.text
    assert status.call_count == 1


@respx.mock
def test_the_spool_assignments_are_read_only_for_the_plates_filaments(
    client: TestClient, model: str
) -> None:
    """Nothing reads a spool's side before the upload any more (#768)."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    assignments = respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
    )
    slice_routes()
    queue_route()

    response = run_print(client, output_id, json=on_spool(10))

    assert response.status_code == 200, response.text
    assert assignments.call_count == 1


@respx.mock
@pytest.mark.parametrize(
    "status", [WIRED, NO_LEFT, BOTH_04_LEFT_HF], ids=["wired", "single", "high-flow"]
)
def test_the_check_refuses_and_warns_about_no_mounted_nozzle(
    client: TestClient, model: str, paths: DataPaths, status: dict[str, Any]
) -> None:
    """#755's check repeated the nozzle verdict; with no verdict it says nothing, and
    uploads nothing."""
    output_id = two_colour_output(client, model, paths)
    upload = upload_route()
    run_routes()
    _status(**status)

    check = client.post(
        f"/api/v1/print/outputs/{output_id}/check",
        json={**body(), "filament_plan": ONE_ON_LEFT},
    )

    assert check.status_code == 200, check.text
    assert (check.json()["errors"], check.json()["warnings"]) == ([], [])
    assert not upload.called


def _check(client: TestClient, output_id: str, **choices: Any) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/api/v1/print/outputs/{output_id}/check",
        json={**body(tier="standard", **choices), "filament_plan": ONE_ON_LEFT},
    )
    return response


@respx.mock
def test_the_check_carries_the_high_flow_warning(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#723, #797: a mounted High Flow nozzle of the chosen size is said before Print,
    as a warning that holds nothing."""
    output_id = two_colour_output(client, model, paths)
    upload = upload_route()
    run_routes()
    _status(**BOTH_04_LEFT_HF)

    check = _check(client, output_id, nozzles=[{"size": "0.4", "flow": "standard"}])

    assert check.status_code == 200, check.text
    assert (check.json()["errors"], check.json()["warnings"]) == (
        [],
        [{"kind": "hf-mounted", "slot_id": None, "message": HF_LEFT}],
    )
    assert not upload.called


@respx.mock
def test_the_check_says_nothing_of_standard_mounted_nozzles(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = two_colour_output(client, model, paths)
    run_routes()
    _status(nozzles=[BOTH_04_LEFT_HF["nozzles"][0]] * 2)

    check = _check(client, output_id, nozzles=[{"size": "0.4"}])

    assert check.status_code == 200, check.text
    assert (check.json()["errors"], check.json()["warnings"]) == ([], [])


@respx.mock
def test_the_check_carries_the_high_flow_warning_when_high_flow_is_chosen(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#797: the slice is always Standard flow (#484), so a High Flow choice still warns
    of a mounted High Flow nozzle of the chosen size."""
    output_id = two_colour_output(client, model, paths)
    run_routes()
    _status(**BOTH_04_LEFT_HF)

    check = _check(client, output_id, nozzles=[{"size": "0.4", "flow": "high_flow"}])

    assert check.status_code == 200, check.text
    assert "hf-mounted" in {warning["kind"] for warning in check.json()["warnings"]}


@respx.mock
def test_the_check_does_not_carry_the_resolvers_high_flow_note(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#862: the resolver's own ``hf-unsupported`` note (Bambuddy always slices High
    Flow as Standard) is left to the run and the nozzle step, not the pre-Print check —
    with Standard nozzles mounted, the check says nothing even when High Flow is
    chosen."""
    output_id = two_colour_output(client, model, paths)
    run_routes()
    _status(nozzles=[BOTH_04_LEFT_HF["nozzles"][0]] * 2)

    check = _check(client, output_id, nozzles=[{"size": "0.4", "flow": "high_flow"}])

    assert check.status_code == 200, check.text
    assert (check.json()["errors"], check.json()["warnings"]) == ([], [])


@respx.mock
def test_the_runs_result_still_carries_the_resolvers_high_flow_note(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#862: unlike the check above, the run itself still says it, from the resolver."""
    _status(nozzles=[BOTH_04_LEFT_HF["nozzles"][0]] * 2)
    response, _ = _two_colour_run(
        client,
        model,
        paths,
        BOTH_ON_RIGHT,
        nozzles=[{"size": "0.4", "flow": "high_flow"}],
        tier="standard",
    )

    assert response.status_code == 200, response.text
    assert "hf-unsupported" in _kinds(response)


@respx.mock
def test_the_check_gives_no_high_flow_warning_when_the_status_is_unreadable(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """No side is assumed: an unreadable status is no nozzle known, so no warning."""
    output_id = two_colour_output(client, model, paths)
    run_routes()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))

    check = _check(client, output_id, nozzles=[{"size": "0.4"}])

    assert check.status_code == 200, check.text
    assert check.json()["warnings"] == []


@respx.mock
@pytest.mark.parametrize(
    "choices",
    [
        {"nozzles": [{"size": "0.2"}, {"size": "0.4"}]},
        {"process_name": "No Such Process @BBL H2C"},
    ],
    ids=["mixed-sizes", "unknown-process"],
)
def test_the_check_gives_the_runs_choice_refusal_word_for_word(
    client: TestClient, model: str, choices: dict[str, Any]
) -> None:
    """#760: what the catalogue refuses for the choices is said before Print too, as the
    run's own 422 detail, with nothing uploaded or sliced."""
    output_id = prepared(client, model)
    uploaded = upload_route()
    run_routes()
    sliced = slice_routes()

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=body(**choices))
    run = run_print(client, output_id, json=body(**choices))

    assert check.status_code == 200, check.text
    assert run.status_code == 422, run.text
    assert check.json() == {"errors": [run.json()["detail"]], "warnings": [], "rack": None}
    assert not uploaded.called
    assert not sliced.called


@respx.mock
def test_the_check_gives_the_runs_printer_refusal_word_for_word(
    client: TestClient, model: str
) -> None:
    """#760: a printer the resolver cannot serve is said before Print, as the run says it."""
    output_id = prepared(client, model)
    uploaded = upload_route()
    run_routes()
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(
            200, json=[{"id": 1, "name": "Workshop", "model": "H2C", "is_active": False}]
        )
    )

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=body())

    assert check.status_code == 200, check.text
    assert check.json()["errors"] == [
        "Workshop is deactivated in Bambuddy. Activate it there, or pick another printer."
    ]
    assert not uploaded.called


@respx.mock
def test_a_422_bambuddy_answers_on_a_read_fails_the_check_rather_than_refusing(
    client: TestClient, model: str
) -> None:
    """#765 review: only the run's own refusals become the check's ``errors``. A 422 that
    Bambuddy answers on a plain read is a failed check, not a verdict to show the user."""
    output_id = prepared(client, model)
    run_routes()
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(422, json={"detail": "model is required"})
    )

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=body())

    # Bambuddy's own 422, passed through by errors.py, not a 200 carrying a verdict.
    assert check.status_code == 422, check.text
    assert "errors" not in check.json()


# --- #836: the rack pick, ranked per plate after the slice ------------------------------


def grouped_requirements_route(
    *,
    sliced_id: int = 77,
    filament_type: str = "PLA",
    color: str = "#FF6A13",
    diameter: str = "0.40",
) -> None:
    """The sliced file's requirements grouped as on library file 228 (spec §8 unknown 2);
    every other file answers the recording. Registered with ``inventory_routes``' pattern,
    so respx re-uses that route and this answer replaces its own."""
    base = recording("filament-requirements.json")

    def answer(request: httpx.Request) -> httpx.Response:
        if f"/library/files/{sliced_id}/" not in request.url.path:
            return httpx.Response(200, json=base)
        filament = {
            "slot_id": 1,
            "type": filament_type,
            "color": color,
            "used_grams": 3.2,
            "used_meters": 1.1,
            "used_in_plate": True,
            "group_id": 0,
            "group": {
                "on_rack": True,
                "nozzle_diameter": diameter,
                "volume_type": "Standard",
                "filament_color": color,
            },
        }
        return httpx.Response(200, json={"file_id": sliced_id, "filaments": [filament]})

    respx.route(method="GET", path__regex=r"/api/v1/library/files/\d+/filament-requirements").mock(
        side_effect=answer
    )


@respx.mock
def test_the_ranked_rack_choice_is_sent(client: TestClient, model: str) -> None:
    """Spec §1: with a readable rack and a sliced group, the ranked pick is sent. The
    recorded rack's 0.4 Standard positions are 2, 4 and 6; position 4 holds FF6A13."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    grouped_requirements_route()
    slice_routes()
    queued = queue_route()

    response = run_print(client, output_id, json=body(nozzles=[{"size": "0.4"}], tier="standard"))

    assert response.status_code == 200, response.text
    assert json.loads(queued.calls.last.request.content)["nozzle_rack_choice"] == {"0": 4}


@respx.mock
def test_a_rack_position_chosen_by_hand_is_sent(client: TestClient, model: str) -> None:
    """Spec §5: the dialog's manual position wins over the ranking where it fits."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    grouped_requirements_route()
    slice_routes()
    queued = queue_route()

    response = run_print(
        client,
        output_id,
        json={**body(nozzles=[{"size": "0.4"}], tier="standard"), "rack_position": 6},
    )

    assert response.status_code == 200, response.text
    assert json.loads(queued.calls.last.request.content)["nozzle_rack_choice"] == {"0": 6}


@respx.mock
def test_let_bambuddy_pick_sends_no_choice(client: TestClient, model: str) -> None:
    """The request's algorithm wins over the printer's remembered one (Least used)."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    grouped_requirements_route()
    slice_routes()
    queued = queue_route()

    response = run_print(
        client,
        output_id,
        json={**body(nozzles=[{"size": "0.4"}], tier="standard"), "rack_algorithm": "bambuddy"},
    )

    assert response.status_code == 200, response.text
    assert json.loads(queued.calls.last.request.content).get("nozzle_rack_choice") is None
    assert all(w["kind"] != "rack-left-to-bambuddy" for w in response.json()["warnings"])


@respx.mock
def test_an_unreadable_rack_sends_no_choice_and_says_so(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    grouped_requirements_route()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))
    slice_routes()
    queued = queue_route()

    response = run_print(client, output_id, json=body(nozzles=[{"size": "0.4"}], tier="standard"))

    assert response.status_code == 200, response.text
    assert json.loads(queued.calls.last.request.content).get("nozzle_rack_choice") is None
    assert {(w["kind"], w["message"]) for w in response.json()["warnings"]} >= {
        ("rack-left-to-bambuddy", "Rack pick left to Bambuddy: status unreadable.")
    }


@respx.mock
def test_a_rack_warning_repeated_on_every_plate_is_shown_once(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Spec §6: rack warnings join the plate loop's de-duplication."""
    output_id = two_plate_output(client, model, paths)
    upload_route()
    printers_route()
    h2c_presets()
    spool_preset_routes()
    hardware_routes()
    split_plates_routes()
    grouped_requirements_route(filament_type="PLA-CF")
    slice_routes()
    queue_route()

    response = run_print(
        client,
        output_id,
        json=run_request(
            all_plates=True, choices={"nozzles": [{"size": "0.4"}], "tier": "standard"}
        ),
    )

    assert response.status_code == 200, response.text
    kinds = [w["kind"] for w in response.json()["warnings"]]
    assert kinds.count("rack-unsafe-material") == 1
    assert "rack-left-to-bambuddy" not in kinds
