"""``/api/v1/analyzers/\u2026`` through the real app (#284).

Bambuddy is mocked with respx from the recordings (``tests/bambuddy/recordings``), as
the print routes' tests do. Nothing here posts to Bambuddy: the analyzers only read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.analyzers import builtin
from scadbuddy.analyzers.context import AnalysisContext
from scadbuddy.analyzers.model import Analyzer, AnalyzerDiagnostic, Fix, Source, change
from scadbuddy.analyzers.sources import ACCESSED
from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.events import Event, InProcessEventBus
from tests.api.test_print import presets_routes
from tests.api.test_send import BASE, configure, make_output
from tests.bambuddy.conftest import recording

API = f"{BASE}/api/v1"
SILK_SPOOL = 5  # "Tri Color" subtype, preset "Bambu PLA Silk" (inventory-spools.json)


@pytest.fixture
def events(app: FastAPI) -> list[Event]:
    bus = getattr(app.state, STATE_ATTR).events
    assert isinstance(bus, InProcessEventBus)
    seen: list[Event] = []
    bus.add_listener(seen.append)
    return seen


def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def bambuddy_routes(*, spools: httpx.Response | None = None) -> dict[str, respx.Route]:
    presets_routes()
    return {
        "printers": respx.get(f"{API}/printers/").mock(
            return_value=httpx.Response(200, json=recording("printers.json"))
        ),
        "spools": respx.get(f"{API}/inventory/spools").mock(
            return_value=spools or httpx.Response(200, json=recording("inventory-spools.json"))
        ),
    }


SILK_REQUEST: dict[str, Any] = {
    "filament_plan": {"slots": [{"slot_id": 1, "spool_id": SILK_SPOOL}]},
    "choices": {
        "nozzles": [{"size": "0.2"}, {"size": "0.2"}],
        "tier": "fine",
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
    assert inputs["choices"]["reason"] == "no nozzle or quality is chosen for this model"
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

    # No printer named: the configured one, printer 1, an H2C (spool-first spec §7).
    assert report["base"]["printer_preset_name"] == "Bambu Lab H2C 0.2 nozzle"
    assert report["base"]["process_name"] == "0.08mm High Quality @BBL H2C 0.2 nozzle"
    assert report["base"]["printer_model"] == "H2C"
    assert report["base"]["bed_type"] == "Supertack Plate"
    scopes = [(scope["kind"], scope["key"]) for scope in report["scopes"]]
    assert ("material", "pla/tri color") in scopes and ("printer", "id:1") in scopes
    # Not uploaded, so nothing asked Bambuddy's plate slots.
    inputs = {row["name"]: row for row in report["inputs"]}
    assert inputs["inventory"]["reason"] == "this output has not been uploaded to Bambuddy yet"
    assert routes["spools"].called
    assert not any(call.request.method == "POST" for call in respx.calls)


def test_without_choices_the_models_remembered_ones_are_judged(
    client: TestClient, model: str
) -> None:
    """What the print dialog reopens with (spool-first spec §7): the model's nozzles
    and tier, on the plate remembered for its printer."""
    remembered = {"printer_id": 1, "nozzles": [{"size": "0.6"}], "tier": "draft"}
    _ok(client.put(f"/api/v1/print/models/{model}/choices", json=remembered))
    _ok(client.put("/api/v1/print/printers/1/bed-type", json={"bed_type": "Supertack Plate"}))
    report = _run(client, make_output(client, model), request={"printer_id": 1})
    base = report["base"]
    assert base["choices"]["nozzles"] == [
        {"size": "0.6", "flow": "standard"},
        {"size": "0.6", "flow": "standard"},
    ]
    assert base["process_name"] == "0.30mm Standard @BBL H2C 0.6 nozzle"
    assert base["bed_type"] == "Supertack Plate"


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


@respx.mock
def test_an_uploaded_output_is_judged_on_inventory_too(
    client: TestClient, model: str, app: FastAPI
) -> None:
    configure(client)
    bambuddy_routes()
    output_id = make_output(client, model)
    getattr(app.state, STATE_ATTR).outputs.record_send(output_id, library_file_id=41)
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
    assert {e["label"]: e["value"] for e in low["evidence"]}["needed per copy"] == 2000
    assert report["summary"]["errors"] == 0
    # Reads only: nothing is uploaded, sliced or queued.
    assert not any(call.request.method != "GET" for call in respx.calls)


# --- without a database -------------------------------------------------------------


def test_without_a_database_the_run_says_no_decisions_were_read(
    client: TestClient, model: str
) -> None:
    report = _run(client, make_output(client, model))
    assert report["decisions_available"] is False
    assert "SCADBUDDY_DATABASE_URL" in report["decisions_reason"]


def test_without_a_database_nothing_is_recorded_and_nothing_is_written(
    client: TestClient, model: str, events: list[Event], data_dir: Path
) -> None:
    output_id = make_output(client, model)
    before = sorted(str(path) for path in data_dir.rglob("*") if "analyzer" in path.name)
    decision = {"diagnostic_id": "SB1003", "kind": "ignore", "scope": {"kind": "global"}}
    apply = {
        "target": {"output_id": output_id},
        "diagnostic_key": "SB1003",
        "fix_id": "enable-support",
        "fingerprint": "0" * 64,
        "confirm": True,
    }
    for response in (
        client.post("/api/v1/analyzers/decisions", json=decision),
        client.get("/api/v1/analyzers/decisions"),
        client.delete(f"/api/v1/analyzers/decisions/{'0' * 32}"),
        client.post("/api/v1/analyzers/fixes/apply", json=apply),
    ):
        problem = _ok(response, 503)
        assert problem["type"].endswith("/database-required")
        assert "SCADBUDDY_DATABASE_URL" in problem["detail"]
    assert all(event.kind != "analyzer.decision" for event in events)
    assert sorted(str(path) for path in data_dir.rglob("*") if "analyzer" in path.name) == before
