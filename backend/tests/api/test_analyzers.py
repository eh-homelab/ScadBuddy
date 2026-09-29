"""``/api/v1/analyzers/\u2026`` through the real app (#284).

Bambuddy is mocked with respx from the recordings (``tests/bambuddy/recordings``), as
the print routes' tests do. Nothing here posts to Bambuddy: the analyzers only read.
"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.analyzers import builtin
from scadbuddy.analyzers.context import AnalysisContext
from scadbuddy.analyzers.decisions import PostgresDecisionStore
from scadbuddy.analyzers.model import Analyzer, AnalyzerDiagnostic, Fix, Source, change
from scadbuddy.analyzers.sources import ACCESSED
from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.bambuddy.uploads import LibraryCopy
from scadbuddy.core.paths import DataPaths
from tests.api.test_events import Recorded
from tests.api.test_send import BASE, configure, make_output
from tests.bambuddy.conftest import recording
from tests.test_bambu3mf import add_plate

API = f"{BASE}/api/v1"
SILK_SPOOL = 5  # "Tri Color" subtype, preset "Bambu PLA Silk" (inventory-spools.json)


@pytest.fixture
def events(app: FastAPI) -> Recorded:
    bus = getattr(app.state, STATE_ATTR).events
    seen = Recorded(bus)
    bus.add_listener(seen.record)
    return seen


def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def bambuddy_routes(*, spools: httpx.Response | None = None) -> dict[str, respx.Route]:
    """Only what an analysis reads: printers and the spool inventory."""
    return {
        "printers": respx.get(f"{API}/printers/").mock(
            return_value=httpx.Response(200, json=recording("printers.json"))
        ),
        "spools": respx.get(f"{API}/inventory/spools").mock(
            return_value=spools or httpx.Response(200, json=recording("inventory-spools.json"))
        ),
    }


#: The spool-first dialog's request (#335): printer 1 (the recording's H2C), the silk
#: spool in slot 1, 0.4 nozzles on both sides, and the SuperTack plate.
SILK_REQUEST: dict[str, Any] = {
    "printer_id": 1,
    "filament_plan": {"slots": [{"slot_id": 1, "spool_id": SILK_SPOOL}]},
    "choices": {
        "nozzles": [{"size": "0.4"}, {"size": "0.4"}],
        "tier": "standard",
        "bed_type": "Supertack Plate",
    },
}


def _run(client: TestClient, output_id: str, **body: Any) -> Any:
    return _ok(
        client.post("/api/v1/analyzers/run", json={"target": {"output_id": output_id}, **body})
    )


# --- catalogue ----------------------------------------------------------------------


def test_the_catalogue_lists_every_analyzer_with_its_sources(client: TestClient) -> None:
    rows = _ok(client.get("/api/v1/analyzers"))
    ids = [row["id"] for row in rows]
    assert ids == [analyzer.id for analyzer in builtin.BUILTIN]
    for row in rows:
        assert row["sources"] and all(s["url"] and s["quote"] for s in row["sources"])
    silk = next(row for row in rows if row["id"] == "SB2001")
    assert silk["fix_ids"] == ["silk-gloss"] and silk["needs"] == ["filaments"]


# --- running ------------------------------------------------------------------------


def test_without_bambuddy_the_geometry_analyzers_run_and_the_rest_say_why(
    client: TestClient, model: str
) -> None:
    output_id = make_output(client, model)
    report = _run(client, output_id, detail="advanced")
    assert report["output_id"] == output_id
    assert report["summary"]["headline"] == "Nothing to report"
    inputs = {row["name"]: row for row in report["inputs"]}
    assert inputs["geometry"]["available"] is True
    assert inputs["printer"] == {
        "name": "printer",
        "available": False,
        "reason": "Bambuddy is not configured",
    }
    assert inputs["choices"]["reason"] == "no nozzle, quality or plate was chosen"
    skipped = {row["id"] for row in report["skipped"]}
    assert {"SB2001", "SB2002", "SB3002"} <= skipped
    assert "SB1001" not in skipped
    kinds = [scope["kind"] for scope in report["scopes"]]
    assert kinds == ["global", "template", "template_version", "configuration", "print"]


def test_a_configuration_is_judged_on_its_newest_render(client: TestClient, model: str) -> None:
    output_id = make_output(client, model)
    body: dict[str, Any] = {"target": {"slug": model, "params": {"width": 12}}}
    assert _ok(client.post("/api/v1/analyzers/run", json=body))["output_id"] == output_id

    body = {"target": {"slug": model, "params": {"width": 13}}, "detail": "advanced"}
    report = _ok(client.post("/api/v1/analyzers/run", json=body))
    assert report["output_id"] is None
    geometry = next(row for row in report["inputs"] if row["name"] == "geometry")
    assert geometry["reason"] == "this configuration has not been rendered yet"
    assert "print" not in [scope["kind"] for scope in report["scopes"]]


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"output_id": "0" * 32, "slug": "demo"},
        {"output_id": "0" * 32, "params": {}},
        {"output_id": "nope"},
    ],
)
def test_the_target_must_name_one_subject(client: TestClient, target: dict[str, Any]) -> None:
    _ok(client.post("/api/v1/analyzers/run", json={"target": target}), 422)


def test_unknown_subjects_are_404(client: TestClient) -> None:
    _ok(client.post("/api/v1/analyzers/run", json={"target": {"output_id": "0" * 32}}), 404)
    _ok(client.post("/api/v1/analyzers/run", json={"target": {"slug": "missing"}}), 404)


@respx.mock
def test_silk_on_supertack_is_found_from_the_plan_and_the_choices(
    client: TestClient, model: str
) -> None:
    configure(client)
    routes = bambuddy_routes()
    output_id = make_output(client, model)
    report = _run(client, output_id, request=SILK_REQUEST)

    assert report["summary"] == {
        "headline": "1 warning, 1 suggestion",
        "errors": 0,
        "warnings": 1,
        "suggestions": 1,
    }
    found = {row["key"]: row for row in report["diagnostics"]}
    assert set(found) == {"SB2001", "SB2003"}
    silk = found["SB2001"]
    assert silk["sources"][0]["url"].startswith("https://wiki.bambulab.com/")
    # Simple detail: the message, its sources and fixes, without the evidence.
    assert silk["evidence"] == [] and silk["why"] is None
    assert silk["fixes"][0]["id"] == "silk-gloss"

    # The base is what the resolver derives from the choices, on printer 1 (an H2C).
    base = report["base"]
    assert base["printer_model"] == "H2C"
    assert base["printer_preset_name"] == "Bambu Lab H2C 0.4 nozzle"
    assert base["process_preset_name"] == "0.20mm Standard @BBL H2C"
    assert base["bed_type"] == "Supertack Plate"
    scopes = [(scope["kind"], scope["key"]) for scope in report["scopes"]]
    assert ("material", "pla/tri color") in scopes and ("printer", "id:1") in scopes
    # Not uploaded, so nothing asked Bambuddy for the plate's slots.
    inputs = {row["name"]: row for row in report["inputs"]}
    assert inputs["inventory"]["reason"] == "this output has not been uploaded to Bambuddy yet"
    assert routes["spools"].called
    assert not any(call.request.method == "POST" for call in respx.calls)


@respx.mock
def test_a_missing_scope_is_named_as_the_reason_an_input_is_unavailable(
    client: TestClient, model: str
) -> None:
    configure(client)
    bambuddy_routes(spools=httpx.Response(403, json={"detail": "forbidden"}))
    output_id = make_output(client, model)
    report = _run(client, output_id, request=SILK_REQUEST, detail="advanced")
    filaments = next(row for row in report["inputs"] if row["name"] == "filaments")
    assert filaments["available"] is False
    assert "Read Status" in filaments["reason"]


# --- fixes --------------------------------------------------------------------------


def test_previewing_a_diagnostic_that_is_not_reported_is_404(
    client: TestClient, model: str
) -> None:
    output_id = make_output(client, model)
    body = {"target": {"output_id": output_id}, "diagnostic_key": "SB2001", "fix_id": "x"}
    _ok(client.post("/api/v1/analyzers/fixes/preview", json=body), 404)


TIMELAPSE_SOURCE = Source(
    url="https://example.test/timelapse",
    title="A test source",
    quote="Timelapse on.",
    accessed=ACCESSED,
    supports=["SB9001", "timelapse"],
)


def _timelapse(context: AnalysisContext, analyzer: Analyzer) -> list[AnalyzerDiagnostic]:
    fix = Fix(
        id="timelapse",
        title="Record a timelapse",
        description="",
        changes=[change("print_options", "timelapse", True, sources=[TIMELAPSE_SOURCE])],
    )
    return [analyzer.diagnose(message="Record this one.", fixes=[fix])]


VERIFIED = Analyzer(
    id="SB9001",
    name="timelapse",
    title="Timelapse",
    severity="info",
    category="profile",
    description="A test rule whose fix lands in a verified target.",
    sources=(TIMELAPSE_SOURCE,),
    check=_timelapse,
    needs=frozenset({"output"}),
)


@pytest.mark.requires_postgres
@respx.mock
def test_an_uploaded_output_is_judged_on_the_inventory_too(
    client: TestClient, model: str, app: FastAPI
) -> None:
    configure(client)
    bambuddy_routes()
    output_id = make_output(client, model)
    uploads = getattr(app.state, STATE_ATTR).uploads
    asyncio.run(uploads.record(output_id, LibraryCopy(id=41, folder_id=2, target_key="H2C")))
    respx.get(f"{API}/library/files/41/filament-requirements").mock(
        return_value=httpx.Response(
            200,
            json={
                "file_id": 41,
                "filaments": [
                    {"slot_id": 1, "type": "PLA", "color": "#FFFFFF", "used_grams": 2000}
                ],
            },
        )
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

    report = _run(client, output_id, request=SILK_REQUEST, detail="advanced")
    found = {row["key"]: row for row in report["diagnostics"]}
    low = found["SB3002:slot-1"]
    assert low["slots"] == [1]
    assert {e["label"]: e["value"] for e in low["evidence"]}["needed per copy"] == 2000
    # Reads only: nothing is uploaded, sliced or queued.
    assert all(call.request.method == "GET" for call in respx.calls)


@pytest.mark.requires_postgres
@respx.mock
@pytest.mark.parametrize(("all_plates", "short"), [(True, True), (False, False)])
def test_an_all_plates_print_is_judged_on_every_plates_filament(
    client: TestClient, model: str, app: FastAPI, paths: DataPaths, all_plates: bool, short: bool
) -> None:
    """The silk spool has ~965 g left: plate 1's 600 g fits, both plates' 1200 g do not,
    as the print dialog's filament step sums them for "All plates" (#198)."""
    configure(client)
    routes = bambuddy_routes()
    output_id = make_output(client, model)
    [path] = paths.outputs.glob(f"*/{output_id}/model.3mf")
    add_plate(path, 2)
    uploads = getattr(app.state, STATE_ATTR).uploads
    asyncio.run(uploads.record(output_id, LibraryCopy(id=41, folder_id=2, target_key="H2C")))
    plates: list[int] = []

    def requirements(request: httpx.Request) -> httpx.Response:
        plate = int(request.url.params["plate_id"])
        plates.append(plate)
        return httpx.Response(
            200,
            json={
                "file_id": 41,
                "plate_id": plate,
                "filaments": [{"slot_id": 1, "type": "PLA", "color": "#FFFFFF", "used_grams": 600}],
            },
        )

    respx.get(f"{API}/library/files/41/filament-requirements").mock(side_effect=requirements)
    shared = {
        "spools": routes["spools"],
        "assignments": respx.get(f"{API}/inventory/assignments").mock(
            return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
        ),
        "printer": respx.get(f"{API}/printers/1").mock(
            return_value=httpx.Response(200, json=recording("printer.json"))
        ),
        "remain": respx.get(f"{API}/printers/1/inventory-remain").mock(
            return_value=httpx.Response(200, json=recording("inventory-remain.json"))
        ),
    }

    report = _run(
        client, output_id, request={**SILK_REQUEST, "all_plates": all_plates}, detail="advanced"
    )
    found = {row["key"]: row for row in report["diagnostics"]}
    assert sorted(plates) == ([1, 2] if all_plates else [1])
    # Only the slots are a plate's own: the inventory is read once for every plate
    # (the spools twice, once more for the filaments input's own read of the plan).
    assert {name: route.call_count for name, route in shared.items()} == {
        "spools": 2,
        "assignments": 1,
        "printer": 1,
        "remain": 1,
    }
    assert ("SB3002:slot-1" in found) is short
    if short:
        evidence = {e["label"]: e["value"] for e in found["SB3002:slot-1"]["evidence"]}
        assert evidence["needed per copy"] == 1200


def _strip_plater_id(path: Path, index: int) -> None:
    """Plate ``index`` loses its ``plater_id``, which ``plates_of`` refuses (bambu3mf.py)."""
    with zipfile.ZipFile(path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    config = entries["Metadata/model_settings.config"].decode("utf-8")
    line = f'  <metadata key="plater_id" value="{index}"/>\n'
    assert line in config
    entries["Metadata/model_settings.config"] = config.replace(line, "").encode("utf-8")
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)


@pytest.mark.requires_postgres
@respx.mock
def test_an_all_plates_print_whose_plates_cannot_be_listed_says_so(
    client: TestClient, model: str, app: FastAPI, paths: DataPaths
) -> None:
    """A 3MF ``plates_of`` refuses leaves the inventory unavailable with the reason, as
    every other unreadable input does, rather than failing the whole run."""
    configure(client)
    bambuddy_routes()
    output_id = make_output(client, model)
    [path] = paths.outputs.glob(f"*/{output_id}/model.3mf")
    add_plate(path, 2)
    _strip_plater_id(path, 2)
    uploads = getattr(app.state, STATE_ATTR).uploads
    asyncio.run(uploads.record(output_id, LibraryCopy(id=41, folder_id=2, target_key="H2C")))
    requirements = respx.get(f"{API}/library/files/41/filament-requirements").mock(
        return_value=httpx.Response(200, json={"file_id": 41, "plate_id": 1, "filaments": []})
    )

    request = {**SILK_REQUEST, "all_plates": True}
    report = _run(client, output_id, request=request, detail="advanced")
    inputs = {row["name"]: row for row in report["inputs"]}
    assert inputs["inventory"] == {
        "name": "inventory",
        "available": False,
        "reason": "the 3MF's plates cannot be read: a <plate> in the 3MF's model settings "
        "has no plater_id",
    }
    assert not requirements.called
    skipped = {row["id"]: row for row in report["skipped"]}
    assert [row["name"] for row in skipped["SB3002"]["missing"]] == ["inventory"]


def test_a_database_that_cannot_be_reached_degrades_to_a_503(
    client: TestClient, model: str, app: FastAPI, events: Recorded
) -> None:
    # Nothing listens on port 1: every connect is refused, and the store gives up
    # within its connect timeout instead of hanging the request.
    state = getattr(app.state, STATE_ATTR)
    state.decisions = PostgresDecisionStore(
        "postgresql://nobody@127.0.0.1:1/none", connect_timeout=1.0
    )
    try:
        output_id = make_output(client, model)
        report = _run(client, output_id)
        assert report["decisions_available"] is False
        assert "cannot be reached" in report["decisions_reason"]
        events.clear()
        decision = {"diagnostic_id": "SB1003", "kind": "ignore", "scope": {"kind": "global"}}
        for response in (
            client.post("/api/v1/analyzers/decisions", json=decision),
            client.get("/api/v1/analyzers/decisions"),
        ):
            problem = _ok(response, 503)
            assert problem["type"].endswith("/database-unavailable")
        events.settle()
        assert [event for event in events if event.kind == "analyzer.decision"] == []
    finally:
        state.decisions.close()
        state.decisions = None


@respx.mock
def test_without_choices_the_models_remembered_ones_and_printer_are_used(
    client: TestClient, model: str
) -> None:
    configure(client)
    bambuddy_routes()
    remembered = {
        "printer_id": 1,
        "nozzles": [{"size": "0.2"}, {"size": "0.2"}],
        "tier": "fine",
    }
    _ok(client.put(f"/api/v1/print/models/{model}/choices", json=remembered))
    _ok(client.put("/api/v1/print/printers/1/bed-type", json={"bed_type": "Supertack Plate"}))
    output_id = make_output(client, model)
    request = {"filament_plan": SILK_REQUEST["filament_plan"]}
    report = _run(client, output_id, request=request, detail="advanced")
    base = report["base"]
    assert base["choices_origin"] == "remembered"
    assert base["printer_id"] == 1
    assert base["process_preset_name"] == "0.08mm High Quality @BBL H2C 0.2 nozzle"
    assert base["bed_type"] == "Supertack Plate"
    assert {row["id"] for row in report["diagnostics"]} >= {"SB2001", "SB2003"}
