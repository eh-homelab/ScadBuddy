"""Issue #86 — /api/v1/print/…, through the real app.

Bodies for the GET routes are the recordings (see ``tests/bambuddy/recordings/README.md``);
the eligibility and run responses are built from Bambuddy's own ``openapi.json``, because
posting to the live instance was out of bounds.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy.pipelines import BED_TYPES
from tests.api.conftest import wait_for_job
from tests.api.test_send import BASE, configure, make_output, upload_route
from tests.bambuddy.conftest import recording

API = f"{BASE}/api/v1"


def pipelines_route(body: Any | None = None) -> respx.Route:
    return respx.get(f"{API}/slicer-pipelines/").mock(
        return_value=httpx.Response(200, json=body or recording("slicer-pipelines-configured.json"))
    )


def printers_route() -> respx.Route:
    return respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=recording("printers.json"))
    )


def presets_routes() -> None:
    respx.get(f"{API}/slicer/presets").mock(
        return_value=httpx.Response(200, json=recording("slicer-presets.json"))
    )
    respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=recording("local-presets.json"))
    )


def report(
    *,
    ok: bool = True,
    target_kind: str = "specific_printer",
    issues: list[dict[str, Any]] | None = None,
    printer_reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A ``PipelineEligibilityReport`` as Bambuddy's openapi.json declares it."""
    return {
        "ok": ok,
        "target_kind": target_kind,
        "target_printer_id": 1,
        "target_printer_name": "3DP-31B-598",
        "target_model_class": None,
        "issues": issues or [],
        "printer_reports": printer_reports or [],
    }


def run_body(run_id: int = 12, *, overridden: bool = False) -> dict[str, Any]:
    return {
        "id": run_id,
        "pipeline_id": 1,
        "pipeline_name": "Default",
        "source_library_file_id": 41,
        "copies": 2,
        "copies_completed": 0,
        "copies_failed": 0,
        "copies_cancelled": 0,
        "copies_in_progress": 2,
        "status": "queued",
        "slice_job_id": 9,
        "sliced_library_file_id": 52,
        "eligibility_overridden": overridden,
        "target_kind": "specific_printer",
        "target_printer_id": 1,
        "fanout_strategy": "max_parallel",
        "jobs": [
            {
                "id": 31,
                "pipeline_run_id": run_id,
                "copy_index": 0,
                "assigned_printer_id": 1,
                "assigned_printer_name": "3DP-31B-598",
                "queue_entry_id": 77,
                "status": "queued",
            }
        ],
    }


# --- listing ------------------------------------------------------------------------


@respx.mock
def test_the_pipeline_list_names_the_presets_and_resolves_the_target(
    client: TestClient, model: str
) -> None:
    configure(client)
    pipelines_route()
    printers_route()
    presets_routes()

    body = client.get(f"/api/v1/print/models/{model}/pipelines").json()

    assert len(body["pipelines"]) == 1
    pipeline = body["pipelines"][0]
    assert pipeline["name"] == "Default"
    assert pipeline["bed_type"] == "Textured PEI Plate"
    # Bambuddy gives refs; the picker needs names, and there is no preset-by-id route.
    assert pipeline["process_preset_name"] == "0.08mm High Quality @BBL H2C 0.2 nozzle"
    assert pipeline["printer_preset"] == {"source": "cloud", "id": "GM041"}
    # A specific_printer target is one printer, named.
    assert pipeline["target_kind"] == "specific_printer"
    assert pipeline["printer_ids"] == [1]
    assert pipeline["target_printer_name"] == "3DP-31B-598"


@respx.mock
def test_an_unresolvable_preset_ref_leaves_the_name_null_rather_than_failing(
    client: TestClient, model: str
) -> None:
    """The recorded catalogue is truncated, so GM041's printer name is not in it — which
    is exactly what a preset deleted in Bambuddy looks like."""
    configure(client)
    pipelines_route()
    printers_route()
    presets_routes()

    pipeline = client.get(f"/api/v1/print/models/{model}/pipelines").json()["pipelines"][0]

    assert pipeline["printer_preset_name"] is None
    assert pipeline["printer_preset"] == {"source": "cloud", "id": "GM041"}


@respx.mock
def test_a_printer_class_target_resolves_to_every_active_printer_of_that_model(
    client: TestClient, model: str
) -> None:
    configure(client)
    pipelines_route(
        {
            "pipelines": [
                {
                    "id": 7,
                    "name": "Any H2C",
                    "printer_preset": {"source": "cloud", "id": "GM041"},
                    "process_preset": {"source": "cloud", "id": "GP243"},
                    "filament_presets": [{"source": "cloud", "id": "GFSG00_23"}],
                    "bed_type": "Textured PEI Plate",
                    "target_kind": "printer_class",
                    "target_printer_id": None,
                    "target_model_class": "H2C",
                    "fanout_strategy": "round_robin",
                }
            ]
        }
    )
    printers_route()
    presets_routes()

    pipeline = client.get(f"/api/v1/print/models/{model}/pipelines").json()["pipelines"][0]

    assert pipeline["target_model_class"] == "H2C"
    assert pipeline["target_printer_name"] is None
    assert pipeline["printer_ids"] == [1]


@respx.mock
def test_the_model_default_wins_over_the_global_fallback(client: TestClient, model: str) -> None:
    configure(client, pipeline_id=1)
    pipelines_route()
    printers_route()
    presets_routes()

    assert client.put(f"/api/v1/print/models/{model}/pipeline", json={"pipeline_id": 9}).json() == {
        "slug": model,
        "pipeline_id": 9,
        "global_pipeline_id": 1,
    }

    body = client.get(f"/api/v1/print/models/{model}/pipelines").json()
    assert (body["model_pipeline_id"], body["global_pipeline_id"]) == (9, 1)
    assert body["default_pipeline_id"] == 9


@respx.mock
def test_clearing_the_model_default_falls_back_to_the_global_one(
    client: TestClient, model: str
) -> None:
    configure(client, pipeline_id=1)
    pipelines_route()
    printers_route()
    presets_routes()
    client.put(f"/api/v1/print/models/{model}/pipeline", json={"pipeline_id": 9})

    cleared = client.put(f"/api/v1/print/models/{model}/pipeline", json={"pipeline_id": None})

    assert cleared.json()["pipeline_id"] is None
    body = client.get(f"/api/v1/print/models/{model}/pipelines").json()
    assert (body["model_pipeline_id"], body["default_pipeline_id"]) == (None, 1)


# --- presets ------------------------------------------------------------------------


@respx.mock
def test_presets_returns_only_printers_and_bed_types_until_a_printer_is_named(
    client: TestClient,
) -> None:
    """Unfiltered, process and filament are thousands of rows on the live instance."""
    configure(client)
    presets_routes()

    body = client.get("/api/v1/print/presets").json()

    assert body["process"] == []
    assert body["filament"] == []
    assert body["bed_types"] == list(BED_TYPES)
    names = [choice["name"] for choice in body["printer"]]
    assert "Bambu Lab A1 0.4 nozzle" in names


@respx.mock
def test_naming_a_printer_preset_filters_process_and_filament_by_compatibility(
    client: TestClient,
) -> None:
    """A preset is compatible when its ``compatible_printers`` names the chosen printer
    preset — by name, which is also the ``standard`` tier's id."""
    configure(client)
    presets_routes()

    body = client.get(
        "/api/v1/print/presets",
        params={"printer_preset_source": "cloud", "printer_preset_id": "GM029"},
    ).json()

    assert body["printer_preset"] == {"source": "cloud", "id": "GM029"}
    # GM029 is "Bambu Lab A1 0.2 nozzle"; the H2D/H2C/A1-mini rows drop out, and so do
    # the OrcaSlicer imports, which are all H2C.
    assert [choice["name"] for choice in body["process"]] == ["0.06mm Fine @BBL A1 0.2 nozzle"]
    assert [choice["name"] for choice in body["filament"]] == [
        "Bambu ABS @BBL A1 0.2 nozzle",
        "Bambu ABS @BBL A1 0.2 nozzle",
    ]


@respx.mock
def test_a_printer_preset_the_catalogue_cannot_name_filters_nothing(
    client: TestClient,
) -> None:
    """Compatibility is declared against a *name*, so an unresolvable ref means ScadBuddy
    cannot judge it — it offers everything rather than silently hiding valid presets.
    (This is also what a preset deleted in Bambuddy looks like.)"""
    configure(client)
    presets_routes()

    body = client.get(
        "/api/v1/print/presets",
        params={"printer_preset_source": "cloud", "printer_preset_id": "GM041"},
    ).json()

    assert len(body["process"]) == 6
    assert any(choice["ref"]["source"] == "local" for choice in body["filament"])


@respx.mock
def test_the_local_orcaslicer_presets_are_offered_alongside_the_cloud_ones(
    client: TestClient,
) -> None:
    """``/local-presets/`` is not the ``local`` tier of ``/slicer/presets`` — a user's own
    imported filament profile lives only there, and its columns are JSON strings."""
    configure(client)
    presets_routes()

    body = client.get(
        "/api/v1/print/presets",
        params={"printer_preset_source": "cloud", "printer_preset_id": "GM041"},
    ).json()

    local = [choice for choice in body["filament"] if choice["ref"]["source"] == "local"]
    # The ref stringifies the integer DB row id, in the order Bambuddy listed them.
    assert [choice["ref"]["id"] for choice in local] == ["2", "1", "3", "4"]
    magic = local[0]
    assert magic["filament_type"] == "PETG"
    # Both of these arrive as JSON-encoded strings on this route and are normalised here.
    assert magic["compatible_printers"] == ["Bambu Lab H2C 0.4 nozzle"]
    assert magic["filament_colour"] == "#3400AD"


@respx.mock
def test_creating_a_pipeline_passes_the_presets_through_and_reports_bambuddys_target(
    client: TestClient,
) -> None:
    configure(client)
    printers_route()
    presets_routes()
    created = respx.post(f"{API}/slicer-pipelines/").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 5,
                "name": "H2C · 0.2 mm",
                "printer_preset": {"source": "cloud", "id": "GM041"},
                "process_preset": {"source": "cloud", "id": "GP243"},
                "filament_presets": [{"source": "local", "id": "2"}],
                "bed_type": "Textured PEI Plate",
                "target_kind": "specific_printer",
                "target_printer_id": 1,
                "target_model_class": None,
                "fanout_strategy": "max_parallel",
            },
        )
    )

    body = client.post(
        "/api/v1/print/pipelines",
        json={
            "name": "H2C · 0.2 mm",
            "printer_preset": {"source": "cloud", "id": "GM041"},
            "process_preset": {"source": "cloud", "id": "GP243"},
            "filament_presets": [{"source": "local", "id": "2"}],
            "bed_type": "Textured PEI Plate",
        },
    ).json()

    sent = json.loads(created.calls.last.request.read())
    # SlicerPipelineCreate carries no target fields, so none are invented here.
    assert "target_kind" not in sent
    assert sent["filament_presets"] == [{"source": "local", "id": "2"}]
    assert body["id"] == 5
    # The name comes back resolved from the local catalogue, not echoed from the request.
    assert body["filament_preset_names"] == [
        "Cookiecad PETG Magic Dark Magic (3DFP 7JdoWkaDB) @H2C"
    ]
    assert body["target_printer_name"] == "3DP-31B-598"


def test_an_empty_filament_preset_list_is_refused_before_bambuddy_sees_it(
    client: TestClient,
) -> None:
    """Bambuddy's own ``minItems: 1``; a pipeline needs one preset per slot."""
    response = client.post(
        "/api/v1/print/pipelines",
        json={
            "name": "No filament",
            "printer_preset": {"source": "cloud", "id": "GM041"},
            "process_preset": {"source": "cloud", "id": "GP243"},
        },
    )

    assert response.status_code == 422


# --- eligibility --------------------------------------------------------------------


@respx.mock
def test_eligibility_uploads_once_and_reports_every_pipeline_verbatim(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload = upload_route()
    pipelines_route()
    check = respx.post(f"{API}/slicer-pipelines/1/check-eligibility").mock(
        return_value=httpx.Response(
            200,
            json=report(
                ok=False,
                issues=[
                    {
                        "kind": "filament_type_mismatch",
                        "slot_index": 0,
                        "expected": "PLA",
                        "actual": "PETG",
                    }
                ],
            ),
        )
    )

    body = client.post(f"/api/v1/print/outputs/{output_id}/eligibility", json={}).json()

    assert body["library_file_id"] == 41
    assert [entry["pipeline_id"] for entry in body["reports"]] == [1]
    # Bambuddy's issues reach the browser as they came, slot index and all.
    assert body["reports"][0]["report"]["issues"] == [
        {"kind": "filament_type_mismatch", "slot_index": 0, "expected": "PLA", "actual": "PETG"}
    ]
    assert json.loads(check.calls.last.request.read())["source_library_file_id"] == 41

    # Checking again reuses the file rather than uploading a second copy: an output is
    # immutable, so the id it recorded still describes this 3MF.
    client.post(f"/api/v1/print/outputs/{output_id}/eligibility", json={})
    assert upload.call_count == 1


@respx.mock
def test_every_pipeline_is_checked_concurrently_rather_than_one_after_another(
    client: TestClient, model: str
) -> None:
    """The picker cannot open until the last check answers, so they are gathered: the cost
    is the slowest pipeline's latency, not the sum of all of them."""
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    in_flight = 0
    peak = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200, json=report())

    for pipeline_id in (1, 2, 3):
        respx.post(f"{API}/slicer-pipelines/{pipeline_id}/check-eligibility").mock(side_effect=slow)

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/eligibility",
        json={"pipeline_ids": [1, 2, 3]},
    ).json()

    assert peak == 3
    # And the order still matches the ids that were asked for.
    assert [entry["pipeline_id"] for entry in body["reports"]] == [1, 2, 3]


@respx.mock
def test_an_ineligible_pipeline_is_a_200_not_a_409(client: TestClient, model: str) -> None:
    """``check-eligibility`` answers 200 with the report; only ``run`` turns it into 409.
    Reading it as an error would make the picker unable to list what is wrong."""
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    pipelines_route()
    respx.post(f"{API}/slicer-pipelines/1/check-eligibility").mock(
        return_value=httpx.Response(200, json=report(ok=False))
    )

    response = client.post(f"/api/v1/print/outputs/{output_id}/eligibility", json={})

    assert response.status_code == 200
    assert response.json()["reports"][0]["report"]["ok"] is False


@respx.mock
def test_a_printer_class_report_keeps_the_per_printer_reasons(
    client: TestClient, model: str
) -> None:
    """``ok: true`` under ``printer_class`` means *at least one* printer passes, so the
    picker has to show ``printer_reports`` rather than the empty top-level ``issues``."""
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    respx.post(f"{API}/slicer-pipelines/7/check-eligibility").mock(
        return_value=httpx.Response(
            200,
            json=report(
                ok=True,
                target_kind="printer_class",
                printer_reports=[
                    {"printer_id": 1, "printer_name": "3DP-31B-598", "ok": True, "issues": []},
                    {
                        "printer_id": 2,
                        "printer_name": "3DP-99C-001",
                        "ok": False,
                        "issues": [{"kind": "nozzle_diameter_mismatch", "slot_index": 1}],
                    },
                ],
            ),
        )
    )

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/eligibility", json={"pipeline_ids": [7]}
    ).json()

    entry = body["reports"][0]["report"]
    assert (entry["ok"], entry["issues"]) == (True, [])
    assert [(r["printer_id"], r["ok"]) for r in entry["printer_reports"]] == [(1, True), (2, False)]


@respx.mock
def test_eligibility_for_an_unknown_output_is_a_404(client: TestClient) -> None:
    configure(client)
    response = client.post(f"/api/v1/print/outputs/{'0' * 32}/eligibility", json={})
    assert response.status_code == 404


# --- run ----------------------------------------------------------------------------


@respx.mock
def test_run_uses_the_named_pipeline_with_copies_and_records_the_run(
    client: TestClient, model: str
) -> None:
    configure(client)
    # The run reads these to lay the 3MF out for the pipeline's printer (#105).
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    run = respx.post(f"{API}/slicer-pipelines/1/run").mock(
        return_value=httpx.Response(202, json=run_body())
    )

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={"pipeline_id": 1, "copies": 2},
    ).json()

    sent = json.loads(run.calls.last.request.read())
    assert (sent["copies"], sent["force"], sent["source_library_file_id"]) == (2, False, 41)
    assert body["pipeline_id"] == 1
    # jobs[] is Bambuddy's answer about which printer each copy landed on.
    assert body["run"]["jobs"][0]["assigned_printer_name"] == "3DP-31B-598"
    assert body["bambuddy_url"] == f"{BASE}/queue"
    assert client.get(f"/api/v1/outputs/{output_id}").json()["pipeline_run_id"] == 12


@respx.mock
def test_run_without_a_pipeline_uses_the_models_default_before_the_global_one(
    client: TestClient, model: str
) -> None:
    configure(client, pipeline_id=1)
    # The send path reads these to lay the 3MF out for the target printer (#105).
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    client.put(f"/api/v1/print/models/{model}/pipeline", json={"pipeline_id": 4})
    run = respx.post(f"{API}/slicer-pipelines/4/run").mock(
        return_value=httpx.Response(202, json=run_body())
    )

    body = client.post(f"/api/v1/print/outputs/{output_id}/run", json={}).json()

    assert run.called
    assert body["pipeline_id"] == 4


@respx.mock
def test_run_falls_back_to_the_global_pipeline_when_the_model_has_none(
    client: TestClient, model: str
) -> None:
    configure(client, pipeline_id=1)
    # The send path reads these to lay the 3MF out for the target printer (#105).
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    run = respx.post(f"{API}/slicer-pipelines/1/run").mock(
        return_value=httpx.Response(202, json=run_body())
    )

    assert client.post(f"/api/v1/print/outputs/{output_id}/run", json={}).status_code == 200
    assert run.called


@respx.mock
def test_run_with_no_pipeline_anywhere_says_so_rather_than_guessing(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json={})

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert "pipeline" in response.json()["detail"]


@respx.mock
def test_a_blocking_issue_surfaces_bambuddys_report_and_force_overrides_it(
    client: TestClient, model: str
) -> None:
    configure(client)
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    blocked = report(
        ok=False,
        issues=[{"kind": "filament_type_mismatch", "slot_index": 0, "expected": "PLA"}],
    )
    run = respx.post(f"{API}/slicer-pipelines/1/run").mock(
        side_effect=[
            httpx.Response(409, json=blocked),
            httpx.Response(202, json=run_body(overridden=True)),
        ]
    )

    refused = client.post(f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1})

    assert refused.status_code == 409
    assert refused.json()["bambuddy_body"] == blocked

    forced = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={"pipeline_id": 1, "force": True},
    )

    assert forced.status_code == 200
    assert forced.json()["run"]["eligibility_overridden"] is True
    assert json.loads(run.calls.last.request.read())["force"] is True


@respx.mock
def test_the_send_bar_still_works_and_now_honours_the_models_pipeline(
    client: TestClient, model: str
) -> None:
    """``POST /outputs/{id}/send`` predates this router; its global ``pipeline_id`` is
    the fallback now, not the only answer."""
    configure(client, pipeline_id=1)
    # The send path reads these to lay the 3MF out for the target printer (#105).
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    client.put(f"/api/v1/print/models/{model}/pipeline", json={"pipeline_id": 4})
    run = respx.post(f"{API}/slicer-pipelines/4/run").mock(
        return_value=httpx.Response(202, json=run_body())
    )

    body = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"}).json()

    assert run.called
    assert body["pipeline_run_id"] == 12


@respx.mock
def test_the_run_route_uploads_the_3mf_when_the_output_was_never_sent(
    client: TestClient, model: str
) -> None:
    """Printing straight from the picker, without pressing Send first."""
    configure(client)
    pipelines_route()
    printers_route()
    job_id = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}}).json()[
        "job_id"
    ]
    wait_for_job(client, job_id)
    output_id = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id}).json()["id"]
    upload = upload_route()
    respx.post(f"{API}/slicer-pipelines/1/run").mock(
        return_value=httpx.Response(202, json=run_body())
    )

    body = client.post(f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1}).json()

    assert upload.called
    assert body["library_file_id"] == 41


def _two_pipelines() -> dict[str, Any]:
    """Pipeline 1 aims at an H2C, pipeline 2 at a P1S — two different plates."""
    base = recording("slicer-pipelines-configured.json")["pipelines"][0]
    return {
        "pipelines": [
            {
                **base,
                "id": 1,
                "target_kind": "printer_class",
                "target_printer_id": None,
                "target_model_class": "H2C",
            },
            {
                **base,
                "id": 2,
                "target_kind": "printer_class",
                "target_printer_id": None,
                "target_model_class": "P1S",
            },
        ]
    }


@respx.mock
def test_changing_the_target_printer_re_uploads_instead_of_reusing_the_old_placement(
    client: TestClient, model: str
) -> None:
    """A recorded library file id is only good while the plate it was placed for holds.

    The 3MF is centred on the target printer's reachable area and carries that
    printer's prime-tower position (#105), so reusing it after the pipeline changed
    would hand Bambuddy a file laid out for the previous machine.
    """
    configure(client, pipeline_id=1)
    pipelines_route(_two_pipelines())
    printers_route()
    output_id = make_output(client, model)
    upload = upload_route()
    # Re-placing means replacing: the previous file is deleted, not duplicated.
    respx.delete(f"{API}/library/files/41").mock(return_value=httpx.Response(200, json={}))
    for pipeline_id in (1, 2):
        respx.post(f"{API}/slicer-pipelines/{pipeline_id}/check-eligibility").mock(
            return_value=httpx.Response(200, json=report())
        )

    client.post(f"/api/v1/print/outputs/{output_id}/eligibility", json={})
    assert upload.call_count == 1

    # Same output, same 3MF on disk — but now heading for a different printer.
    configure(client, pipeline_id=2)
    client.post(f"/api/v1/print/outputs/{output_id}/eligibility", json={})

    assert upload.call_count == 2, "the file was reused although the plate changed"

    # And back to the first: still re-placed, never reused across a change.
    configure(client, pipeline_id=1)
    client.post(f"/api/v1/print/outputs/{output_id}/eligibility", json={})
    assert upload.call_count == 3


@respx.mock
def test_running_a_pipeline_lays_the_file_out_for_that_pipeline_not_the_default(
    client: TestClient, model: str
) -> None:
    """``pipeline_id`` in the run request overrides the model's default (#86), so the
    plate has to follow the pipeline being run rather than the one settings would pick."""
    configure(client, pipeline_id=1)
    pipelines_route(_two_pipelines())
    printers_route()
    output_id = make_output(client, model)
    upload = upload_route()
    respx.delete(f"{API}/library/files/41").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{API}/slicer-pipelines/1/run").mock(
        return_value=httpx.Response(202, json=run_body())
    )
    respx.post(f"{API}/slicer-pipelines/2/run").mock(
        return_value=httpx.Response(202, json=run_body())
    )

    client.post(f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1})
    assert upload.call_count == 1

    client.post(f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 2})

    assert upload.call_count == 2, "the P1S run reused a file laid out for the H2C"
