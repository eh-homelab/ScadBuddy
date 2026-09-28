"""``/api/v1/analyzers/\u2026`` through the real app (#284).

Bambuddy is mocked with respx from the recordings (``tests/bambuddy/recordings``), as
the print routes' tests do. Nothing here posts to Bambuddy: the analyzers only read.
"""

from __future__ import annotations

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
        "pipelines": respx.get(f"{API}/slicer-pipelines/").mock(
            return_value=httpx.Response(200, json=recording("slicer-pipelines-configured.json"))
        ),
        "printers": respx.get(f"{API}/printers/").mock(
            return_value=httpx.Response(200, json=recording("printers.json"))
        ),
        "spools": respx.get(f"{API}/inventory/spools").mock(
            return_value=spools or httpx.Response(200, json=recording("inventory-spools.json"))
        ),
    }


SILK_REQUEST: dict[str, Any] = {
    "pipeline_id": 1,
    "filament_plan": {"slots": [{"slot_id": 1, "spool_id": SILK_SPOOL}]},
    "bed_type": "Supertack Plate",
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
    assert inputs["pipeline"] == {
        "name": "pipeline",
        "available": False,
        "reason": "Bambuddy is not configured",
    }
    skipped = {row["id"] for row in report["skipped"]}
    assert {"SB2001", "SB3002", "SB5001"} <= skipped
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
def test_silk_on_supertack_is_found_from_the_plan_and_the_pipeline(
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

    # The recording's pipeline slices for a 0.2 nozzle on printer 1, an H2C.
    assert report["base"]["pipeline"]["nozzle_diameter"] == "0.2"
    assert report["base"]["printer_model"] == "H2C"
    assert report["base"]["bed_type"] == "Supertack Plate"
    scopes = [(scope["kind"], scope["key"]) for scope in report["scopes"]]
    assert ("material", "pla/tri color") in scopes and ("printer", "id:1") in scopes
    # Not uploaded, so nothing asked Bambuddy's eligibility or plate slots.
    inputs = {row["name"]: row for row in report["inputs"]}
    assert inputs["eligibility"]["reason"] == "this output has not been uploaded to Bambuddy yet"
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


@respx.mock
def test_a_fix_whose_target_is_unverified_previews_but_cannot_be_applied(
    client: TestClient, model: str, events: list[Event]
) -> None:
    configure(client)
    bambuddy_routes()
    output_id = make_output(client, model)
    body = {
        "target": {"output_id": output_id},
        "request": SILK_REQUEST,
        "diagnostic_key": "SB2001",
        "fix_id": "silk-gloss",
    }
    preview = _ok(client.post("/api/v1/analyzers/fixes/preview", json=body))
    assert preview["applicable"] is False and preview["outward"] is True
    assert len(preview["blockers"]) == 2
    assert "outer_wall_speed: unknown \u2192 50 mm/s [derived_process_preset]" in preview["summary"]
    assert "slicing and queueing" in preview["route_note"]

    events.clear()
    problem = _ok(
        client.post(
            "/api/v1/analyzers/fixes/apply",
            json={**body, "fingerprint": preview["fingerprint"], "confirm": True},
        ),
        409,
    )
    assert problem["type"].endswith("/analyzer-fix-unverified")
    assert any("filament_overrides" in item for item in problem["to_verify"])
    assert events == []
    assert _ok(client.get("/api/v1/analyzers/decisions")) == []


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


def test_applying_is_confirmed_against_the_previewed_diff(
    client: TestClient, model: str, events: list[Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builtin, "BUILTIN", (*builtin.BUILTIN, VERIFIED))
    output_id = make_output(client, model)
    body: dict[str, Any] = {
        "target": {"output_id": output_id},
        "diagnostic_key": "SB9001",
        "fix_id": "timelapse",
    }
    preview = _ok(client.post("/api/v1/analyzers/fixes/preview", json=body))
    assert preview["applicable"] is True and preview["outward"] is True
    apply = {
        **body,
        "fingerprint": preview["fingerprint"],
        "scope": {"kind": "template", "key": model},
    }

    stale = _ok(
        client.post(
            "/api/v1/analyzers/fixes/apply",
            json={**apply, "fingerprint": "0" * 64, "confirm": True},
        ),
        409,
    )
    assert stale["type"].endswith("/analyzer-fix-stale")
    unconfirmed = _ok(client.post("/api/v1/analyzers/fixes/apply", json=apply), 428)
    assert unconfirmed["type"].endswith("/confirmation-required")
    assert "timelapse" in unconfirmed["summary"]
    elsewhere = {**apply, "confirm": True, "scope": {"kind": "template", "key": "other"}}
    _ok(client.post("/api/v1/analyzers/fixes/apply", json=elsewhere), 422)
    assert events == [] or all(event.kind != "analyzer.decision" for event in events)

    decision = _ok(client.post("/api/v1/analyzers/fixes/apply", json={**apply, "confirm": True}))
    assert decision["kind"] == "accept" and decision["scope"] == {"kind": "template", "key": model}
    assert decision["changes"][0]["setting"] == "timelapse"
    [event] = [event for event in events if event.kind == "analyzer.decision"]
    assert event.model_dump(exclude={"id", "at"}) == {
        "kind": "analyzer.decision",
        "decision_id": decision["id"],
        "diagnostic_id": "SB9001",
        "scope": "template",
        "scope_key": model,
        "action": "recorded",
    }

    report = _run(client, output_id)
    row = next(row for row in report["diagnostics"] if row["id"] == "SB9001")
    assert row["status"] == "accepted"
    assert [line["change"]["setting"] for line in report["accepted_changes"]] == ["timelapse"]
    assert report["summary"]["suggestions"] == 0


# --- decisions ----------------------------------------------------------------------


def test_a_suppression_needs_a_reason_and_a_valid_scope(client: TestClient) -> None:
    body: dict[str, Any] = {
        "diagnostic_id": "SB1003",
        "kind": "suppress",
        "scope": {"kind": "template", "key": "demo"},
    }
    _ok(client.post("/api/v1/analyzers/decisions", json=body), 422)
    _ok(client.post("/api/v1/analyzers/decisions", json={**body, "reason": "  "}), 422)
    bad = {**body, "reason": "ok", "scope": {"kind": "print", "key": "demo"}}
    assert _ok(client.post("/api/v1/analyzers/decisions", json=bad), 422)["type"].endswith(
        "/analyzer-scope"
    )
    _ok(client.post("/api/v1/analyzers/decisions", json={**body, "diagnostic_id": "X1"}), 422)


def test_decisions_are_recorded_listed_and_removed_with_events(
    client: TestClient, model: str, events: list[Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builtin, "BUILTIN", (*builtin.BUILTIN, VERIFIED))
    output_id = make_output(client, model)
    events.clear()
    created = _ok(
        client.post(
            "/api/v1/analyzers/decisions",
            json={
                "diagnostic_id": "SB9001",
                "kind": "suppress",
                "scope": {"kind": "template", "key": model},
                "reason": "keychains are always tiny",
            },
        ),
        201,
    )
    ignored = _ok(
        client.post(
            "/api/v1/analyzers/decisions",
            json={"diagnostic_id": "SB1003", "kind": "ignore", "scope": {"kind": "global"}},
        ),
        201,
    )

    everything = _ok(client.get("/api/v1/analyzers/decisions"))
    assert [row["id"] for row in everything] == [ignored["id"], created["id"]]
    at_template = _ok(client.get("/api/v1/analyzers/decisions", params={"scope": "template"}))
    assert [row["id"] for row in at_template] == [created["id"]]
    _ok(client.get("/api/v1/analyzers/decisions", params={"scope_key": model}), 422)

    simple = _run(client, output_id)
    assert all(row["id"] != "SB9001" for row in simple["diagnostics"])
    advanced = _run(client, output_id, detail="advanced")
    row = next(row for row in advanced["diagnostics"] if row["id"] == "SB9001")
    assert row["status"] == "suppressed"
    assert row["decision"]["decision"]["reason"] == "keychains are always tiny"

    _ok(client.delete(f"/api/v1/analyzers/decisions/{created['id']}"), 204)
    _ok(client.delete(f"/api/v1/analyzers/decisions/{created['id']}"), 404)
    assert [e.model_dump(include={"decision_id", "action"}) for e in events] == [
        {"decision_id": created["id"], "action": "recorded"},
        {"decision_id": ignored["id"], "action": "recorded"},
        {"decision_id": created["id"], "action": "removed"},
    ]
    after = _run(client, output_id)
    assert any(row["id"] == "SB9001" for row in after["diagnostics"])


def test_an_accepted_fix_defaults_to_this_print(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builtin, "BUILTIN", (*builtin.BUILTIN, VERIFIED))
    output_id = make_output(client, model)
    body: dict[str, Any] = {
        "target": {"output_id": output_id},
        "diagnostic_key": "SB9001",
        "fix_id": "timelapse",
    }
    preview = _ok(client.post("/api/v1/analyzers/fixes/preview", json=body))
    decision = _ok(
        client.post(
            "/api/v1/analyzers/fixes/apply",
            json={**body, "fingerprint": preview["fingerprint"], "confirm": True},
        )
    )
    assert decision["scope"] == {"kind": "print", "key": output_id}


@respx.mock
def test_an_uploaded_output_is_judged_on_eligibility_and_inventory_too(
    client: TestClient, model: str, app: FastAPI
) -> None:
    configure(client)
    bambuddy_routes()
    output_id = make_output(client, model)
    getattr(app.state, STATE_ATTR).outputs.record_send(output_id, library_file_id=41)
    check = respx.post(f"{API}/slicer-pipelines/1/check-eligibility").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": False,
                "target_kind": "specific_printer",
                "issues": [
                    {
                        "kind": "filament_type_mismatch",
                        "slot_index": 1,
                        "expected": "PETG",
                        "actual": "PLA",
                    }
                ],
                "printer_reports": [],
            },
        )
    )
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
    assert check.calls.last.request.read() == b'{"source_library_file_id":41,"force":false}'
    found = {row["key"]: row for row in report["diagnostics"]}
    assert found["SB5001:1"]["severity"] == "error"
    low = found["SB3002:slot-1"]
    assert {e["label"]: e["value"] for e in low["evidence"]}["needed per copy"] == 2000
    assert report["summary"]["errors"] == 1
    # Reads only: the eligibility check is the one POST, and it starts nothing.
    posts = [call.request.url.path for call in respx.calls if call.request.method != "GET"]
    assert posts == ["/api/v1/slicer-pipelines/1/check-eligibility"]
