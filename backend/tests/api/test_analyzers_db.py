"""The ``/api/v1/analyzers`` routes that persist, against Postgres (#284).

Decisions live in Postgres only; without ``SCADBUDDY_DATABASE_URL`` these routes
answer 503 (``test_analyzers.py``). Bambuddy is mocked with respx as there.
"""

from __future__ import annotations

from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.analyzers import builtin
from scadbuddy.core.events import Event
from scadbuddy.core.settings import Settings
from tests.api.test_analyzers import (
    SILK_REQUEST,
    VERIFIED,
    _ok,
    _run,
    bambuddy_routes,
    events,
)
from tests.api.test_send import configure, make_output

pytestmark = pytest.mark.requires_postgres

__all__ = ["events"]


@pytest.fixture
def settings(settings: Settings, pg_conninfo: str) -> Settings:
    """The app of ``tests/api/conftest.py``, on a throwaway Postgres schema."""
    return settings.model_copy(update={"database_url": pg_conninfo})


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
    assert "always slices then queues" in preview["route_note"]

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
