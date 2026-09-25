"""Issue #88 — what actually leaves ScadBuddy once print options are remembered.

Every assertion here is on the outgoing request body, because that is the only thing
Bambuddy sees. The acceptance case is the last test: timelapse turned off once for the
H2C, then queued with ``timelapse: false`` on every later send.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy.options import BAMBUDDY_DEFAULTS
from tests.api.test_send import (
    API,
    PRESETS,
    configure,
    make_output,
    presets_route,
    upload_route,
)
from tests.bambuddy.conftest import recording

OPTIONS_ROUTE = "/api/v1/settings/print-options"
PIPELINE = recording("slicer-pipeline.json")


def remember(
    client: TestClient, scope: str, options: dict[str, Any], key: str | None = None
) -> None:
    body: dict[str, Any] = {"scope": scope, "options": options}
    if key is not None:
        body["key"] = key
    assert client.put(OPTIONS_ROUTE, json=body).status_code == 200


def slice_routes(sliced_id: int = 52) -> respx.Route:
    # A send resolves the target printer's plate before uploading, to lay the 3MF
    # out on it (#105); with a configured printer that is a read of /printers/.
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=recording("printers.json"))
    )
    route = respx.post(f"{API}/library/files/41/slice").mock(
        return_value=httpx.Response(202, json={"job_id": 9, "status": "pending"})
    )
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(
            200, json={"id": 9, "status": "completed", "result": {"library_file_id": sliced_id}}
        )
    )
    return route


def queue_route() -> respx.Route:
    return respx.post(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue-item.json"))
    )


def pipeline_route(pipeline_id: int = 4, body: dict[str, Any] | None = None) -> respx.Route:
    # The send also resolves the target printer's plate to lay the 3MF out on it
    # (#105), which reads the pipeline list and the printers. Registered here so a
    # test that has a pipeline has the whole lookup, rather than in every caller. The
    # pipeline's printer preset is named through the catalogue for its nozzle (#126).
    presets_route()
    respx.get(f"{API}/slicer-pipelines/").mock(
        return_value=httpx.Response(
            200, json={"pipelines": [{**PIPELINE, **(body or {}), "id": pipeline_id}]}
        )
    )
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=recording("printers.json"))
    )
    return respx.get(f"{API}/slicer-pipelines/{pipeline_id}").mock(
        return_value=httpx.Response(200, json={**PIPELINE, **(body or {}), "id": pipeline_id})
    )


# --- the queue path ------------------------------------------------------------------


@respx.mock
def test_an_option_never_set_goes_out_as_bambuddys_own_default(
    client: TestClient, model: str
) -> None:
    """The regression this guards: ScadBuddy used to force four of these itself —
    ``bed_levelling``/``flow_cali`` to ``"off"`` and ``layer_inspect``/``timelapse`` to
    ``true`` — so a fresh install did not print the way Bambuddy says it will."""
    configure(client, printer_id=1, **PRESETS)
    output_id = make_output(client, model)
    upload_route()
    slice_routes()
    queue = queue_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    queued = json.loads(queue.calls.last.request.read())
    for option, default in BAMBUDDY_DEFAULTS.queue_fields().items():
        assert queued[option] == default, option
    # The two Bambuddy leaves null are simply absent, not sent as an explicit null.
    assert "project_id" not in queued
    assert "preheat_chamber_target_override" not in queued


@respx.mock
def test_the_scopes_merge_least_specific_first(client: TestClient, model: str) -> None:
    configure(client, printer_id=1, **PRESETS)
    remember(client, "global", {"timelapse": True, "layer_inspect": True, "use_ams": False})
    remember(client, "printer", {"timelapse": False}, key="1")
    remember(client, "model", {"bed_levelling": "off"}, key=model)
    output_id = make_output(client, model)
    upload_route()
    slice_routes()
    queue = queue_route()

    body = client.post(
        f"/api/v1/outputs/{output_id}/send",
        json={"mode": "queue", "options": {"manual_start": True}},
    ).json()

    queued = json.loads(queue.calls.last.request.read())
    assert queued["timelapse"] is False  # the printer scope beat the global one
    assert queued["layer_inspect"] is True  # and did not clear its siblings
    assert queued["use_ams"] is False
    assert queued["bed_levelling"] == "off"  # the model scope
    assert queued["manual_start"] is True  # the request scope
    # The result reports back exactly what went out.
    assert body["options"]["timelapse"] is False
    assert body["options"]["manual_start"] is True


@respx.mock
def test_a_per_printer_override_for_another_printer_is_ignored(
    client: TestClient, model: str
) -> None:
    configure(client, printer_id=1, **PRESETS)
    remember(client, "printer", {"timelapse": False}, key="7")
    output_id = make_output(client, model)
    upload_route()
    slice_routes()
    queue = queue_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert json.loads(queue.calls.last.request.read())["timelapse"] == (BAMBUDDY_DEFAULTS.timelapse)


@respx.mock
def test_a_per_model_override_for_another_model_is_ignored(client: TestClient, model: str) -> None:
    configure(client, printer_id=1, **PRESETS)
    remember(client, "model", {"timelapse": False}, key="some-other-model")
    output_id = make_output(client, model)
    upload_route()
    slice_routes()
    queue = queue_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert json.loads(queue.calls.last.request.read())["timelapse"] == (BAMBUDDY_DEFAULTS.timelapse)


@respx.mock
def test_copies_still_drives_the_quantity_and_beats_a_remembered_one(
    client: TestClient, model: str
) -> None:
    configure(client, printer_id=1, **PRESETS)
    remember(client, "model", {"quantity": 5}, key=model)
    output_id = make_output(client, model)
    upload_route()
    slice_routes()
    queue = queue_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue", "copies": 2})

    assert json.loads(queue.calls.last.request.read())["quantity"] == 2


@respx.mock
def test_a_remembered_quantity_applies_when_no_copies_is_sent(
    client: TestClient, model: str
) -> None:
    configure(client, printer_id=1, **PRESETS)
    remember(client, "model", {"quantity": 5}, key=model)
    output_id = make_output(client, model)
    upload_route()
    slice_routes()
    queue = queue_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert json.loads(queue.calls.last.request.read())["quantity"] == 5


# --- the pipeline path ---------------------------------------------------------------


@respx.mock
def test_with_no_options_set_the_pipeline_still_runs_and_is_never_even_read(
    client: TestClient, model: str
) -> None:
    """The extra GET is only paid for when it can change the outcome."""
    configure(client, pipeline_id=4)
    output_id = make_output(client, model)
    upload_route()
    read = pipeline_route()
    run = respx.post(f"{API}/slicer-pipelines/4/run").mock(
        return_value=httpx.Response(202, json={"id": 12, "status": "queued"})
    )

    body = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"}).json()

    assert body["pipeline_run_id"] == 12
    assert not read.called
    assert json.loads(run.calls.last.request.read())["copies"] == 1


@respx.mock
def test_a_remembered_quantity_rides_a_pipeline_run_as_copies(
    client: TestClient, model: str
) -> None:
    """``quantity`` is the one option ``POST /run`` can express, so it stays a run."""
    configure(client, pipeline_id=4)
    remember(client, "global", {"quantity": 3})
    output_id = make_output(client, model)
    upload_route()
    # The send resolves the pipeline's printer to lay the 3MF out on its plate (#105),
    # and names its printer preset for the nozzle the 3MF states (#126).
    presets_route()
    respx.get(f"{API}/slicer-pipelines/").mock(
        return_value=httpx.Response(200, json={"pipelines": [{**PIPELINE, "id": 4}]})
    )
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=recording("printers.json"))
    )
    run = respx.post(f"{API}/slicer-pipelines/4/run").mock(
        return_value=httpx.Response(202, json={"id": 12, "status": "queued"})
    )
    queue = queue_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert json.loads(run.calls.last.request.read())["copies"] == 3
    assert not queue.called


@respx.mock
def test_an_option_a_run_cannot_carry_slices_with_the_pipelines_own_presets(
    client: TestClient, model: str
) -> None:
    configure(client, pipeline_id=4)
    remember(client, "global", {"timelapse": False})
    output_id = make_output(client, model)
    upload_route()
    pipeline_route()
    run = respx.post(f"{API}/slicer-pipelines/4/run")
    sliced = slice_routes()
    queue = queue_route()

    body = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"}).json()

    assert not run.called  # a run would have dropped the option on the floor
    request = json.loads(sliced.calls.last.request.read())
    assert request["printer_preset"] == PIPELINE["printer_preset"]
    assert request["process_preset"] == PIPELINE["process_preset"]
    assert request["filament_presets"] == PIPELINE["filament_presets"]
    assert request["bed_type"] == PIPELINE["bed_type"]

    queued = json.loads(queue.calls.last.request.read())
    assert queued["timelapse"] is False
    assert queued["printer_id"] == PIPELINE["target_printer_id"]
    assert queued["library_file_id"] == 52
    assert body["queue_item_id"] == 9
    assert body["pipeline_run_id"] is None


@respx.mock
def test_a_printer_class_pipeline_queues_by_target_model(client: TestClient, model: str) -> None:
    configure(client, pipeline_id=4)
    remember(client, "global", {"timelapse": False})
    output_id = make_output(client, model)
    upload_route()
    pipeline_route(
        body={
            "target_kind": "printer_class",
            "target_printer_id": None,
            "target_model_class": "H2C",
        }
    )
    slice_routes()
    queue = queue_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    queued = json.loads(queue.calls.last.request.read())
    assert queued["target_model"] == "H2C"
    assert "printer_id" not in queued


@respx.mock
def test_a_per_printer_override_finds_the_pipelines_target_printer(
    client: TestClient, model: str
) -> None:
    """No ``printer_id`` is configured, so the scope key can only come from the pipeline.

    The remembered value is deliberately ``True`` — the opposite of Bambuddy's default —
    because asserting the default would pass just as well if the override were dropped on
    the floor, which is the one thing this test exists to rule out.
    """
    configure(client, pipeline_id=4)
    assert BAMBUDDY_DEFAULTS.timelapse is False
    remember(client, "printer", {"timelapse": True}, key=str(PIPELINE["target_printer_id"]))
    output_id = make_output(client, model)
    upload_route()
    read = pipeline_route()
    slice_routes()
    queue = queue_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    # The premise: the printer id could only have come from reading the pipeline.
    assert read.called
    assert json.loads(queue.calls.last.request.read())["timelapse"] is True


@respx.mock
def test_a_pipeline_with_no_presets_says_so_rather_than_dropping_the_options(
    client: TestClient, model: str
) -> None:
    configure(client, pipeline_id=4)
    remember(client, "global", {"timelapse": False})
    output_id = make_output(client, model)
    upload_route()
    pipeline_route(body={"filament_presets": []})
    sliced = respx.post(f"{API}/library/files/41/slice")

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert response.status_code == 409
    assert "filament presets" in response.json()["detail"]
    assert not sliced.called


@respx.mock
def test_another_printers_override_does_not_cost_a_pipeline_read(
    client: TestClient, model: str
) -> None:
    """One saved override must not make every later send pay for a GET it cannot use, nor
    turn that GET's failure into a send failure."""
    configure(client, pipeline_id=4, printer_id=1)
    remember(client, "printer", {"timelapse": False}, key="7")
    output_id = make_output(client, model)
    upload_route()
    read = pipeline_route()
    run = respx.post(f"{API}/slicer-pipelines/4/run").mock(
        return_value=httpx.Response(202, json={"id": 12, "status": "queued"})
    )

    body = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"}).json()

    assert body["pipeline_run_id"] == 12
    assert run.called
    assert not read.called


@respx.mock
def test_the_targeted_printers_override_still_takes_the_queue_path(
    client: TestClient, model: str
) -> None:
    configure(client, pipeline_id=4, printer_id=1)
    remember(client, "printer", {"timelapse": False}, key="1")
    output_id = make_output(client, model)
    upload_route()
    read = pipeline_route()
    run = respx.post(f"{API}/slicer-pipelines/4/run")
    slice_routes()
    queue = queue_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert read.called
    assert not run.called
    assert json.loads(queue.calls.last.request.read())["timelapse"] is False


@respx.mock
def test_a_models_own_pipeline_is_the_one_read(client: TestClient, model: str) -> None:
    """#86 lets a model default to its own pipeline; the send must follow that one."""
    configure(client, pipeline_id=4)
    assert (
        client.put(f"/api/v1/print/models/{model}/pipeline", json={"pipeline_id": 9}).status_code
        == 200
    )
    remember(client, "global", {"timelapse": False})
    output_id = make_output(client, model)
    upload_route()
    global_pipeline = pipeline_route(4)
    model_pipeline = pipeline_route(9)
    slice_routes()
    queue_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert model_pipeline.called
    assert not global_pipeline.called


@respx.mock
def test_a_printer_class_pipelines_fanout_survives_a_configured_printer(
    client: TestClient, model: str
) -> None:
    """A configured ``printer_id`` keys the option scopes; it must not become the target.

    Setting one is the only way to use the per-printer scope with a printer-class pipeline,
    and taking it as the queue target pinned every copy to that single printer — quietly
    ending the fan-out the pipeline exists for, and only once some unrelated option
    happened to be remembered.
    """
    configure(client, pipeline_id=4, printer_id=1)
    remember(client, "printer", {"insert_at_top": True}, key="1")
    output_id = make_output(client, model)
    upload_route()
    pipeline_route(
        body={
            "target_kind": "printer_class",
            "target_printer_id": None,
            "target_model_class": "H2C",
        }
    )
    slice_routes()
    queue = queue_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    queued = json.loads(queue.calls.last.request.read())
    assert queued["target_model"] == "H2C"
    assert "printer_id" not in queued
    # And the scope still keyed on the configured printer, which is why it applied at all.
    assert queued["insert_at_top"] is True


# --- the acceptance case ------------------------------------------------------------


@respx.mock
def test_timelapse_turned_off_once_for_the_h2c_sticks_for_every_later_send(
    client: TestClient, model: str
) -> None:
    """#88's acceptance case, end to end through the real app."""
    configure(client, printer_id=1, **PRESETS)
    output_id = make_output(client, model)
    upload_route()
    # Each re-send replaces the file Bambuddy already holds, so the delete is expected.
    respx.delete(f"{API}/library/files/41").mock(return_value=httpx.Response(200, json={}))
    slice_routes()
    queue = queue_route()

    remember(client, "printer", {"timelapse": True}, key="1")
    for _ in range(3):
        client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})
        assert json.loads(queue.calls.last.request.read())["timelapse"] is True

    remember(client, "printer", {"timelapse": False}, key="1")
    for _ in range(3):
        client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})
        assert json.loads(queue.calls.last.request.read())["timelapse"] is False

    assert client.get(OPTIONS_ROUTE).json()["printers"]["1"]["timelapse"] is False
