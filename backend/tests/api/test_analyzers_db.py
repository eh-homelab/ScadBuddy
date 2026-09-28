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

APPLY = "/api/v1/analyzers/fixes/apply"
PREVIEW = "/api/v1/analyzers/fixes/preview"
DECISIONS = "/api/v1/analyzers/decisions"


@pytest.fixture
def settings(settings: Settings, pg_conninfo: str) -> Settings:
    """The app of ``tests/api/conftest.py``, on a throwaway Postgres schema."""
    return settings.model_copy(update={"database_url": pg_conninfo})


@pytest.fixture
def verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adds SB9001, a test rule whose fix lands in a verified target."""
    monkeypatch.setattr(builtin, "BUILTIN", (*builtin.BUILTIN, VERIFIED))


def _decisions(events: list[Event]) -> list[dict[str, Any]]:
    return [
        event.model_dump(include={"decision_id", "action"})
        for event in events
        if event.kind == "analyzer.decision"
    ]


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
    preview = _ok(client.post(PREVIEW, json=body))
    assert preview["applicable"] is False and preview["outward"] is True
    assert preview["scope"] == {"kind": "print", "key": output_id}
    assert len(preview["blockers"]) == 2
    assert "outer_wall_speed: unknown → 50 mm/s [derived_process_preset]" in preview["summary"]
    assert "nothing sends it yet" in preview["route_note"]

    events.clear()
    problem = _ok(
        client.post(APPLY, json={**body, "fingerprint": preview["fingerprint"], "confirm": True}),
        409,
    )
    assert problem["type"].endswith("/analyzer-fix-unverified")
    assert any("filament_overrides" in item for item in problem["to_verify"])
    assert _decisions(events) == []
    assert _ok(client.get(DECISIONS)) == []


@pytest.mark.usefixtures("verified")
def test_the_fingerprint_binds_the_scope_the_print_and_the_base(
    client: TestClient, model: str, events: list[Event]
) -> None:
    output_id = make_output(client, model)
    other_id = make_output(client, model, name="Other")
    request = {"choices": {"nozzles": [{"size": "0.4"}], "tier": "standard"}}
    body: dict[str, Any] = {
        "target": {"output_id": output_id},
        "request": request,
        "diagnostic_key": "SB9001",
        "fix_id": "timelapse",
    }
    # Previewed with no scope: the narrowest, this print.
    at_print = _ok(client.post(PREVIEW, json=body))
    assert at_print["scope"] == {"kind": "print", "key": output_id}
    template = {"kind": "template", "key": model}
    at_template = _ok(client.post(PREVIEW, json={**body, "scope": template}))
    assert at_template["fingerprint"] != at_print["fingerprint"]
    events.clear()

    def refused(**changes: Any) -> dict[str, Any]:
        sent = {**body, "fingerprint": at_print["fingerprint"], "confirm": True, **changes}
        problem: dict[str, Any] = _ok(client.post(APPLY, json=sent), 409)
        assert problem["type"].endswith("/analyzer-fix-stale")
        assert "fingerprint" not in problem  # no way round a fresh preview
        return problem

    # A print-scope preview cannot be applied at template scope,
    refused(scope=template)
    # nor for another output of the same template,
    refused(target={"output_id": other_id})
    # nor once the base has moved (another quality tier is another process preset).
    refused(request={"choices": {"nozzles": [{"size": "0.4"}], "tier": "fine"}})
    assert _decisions(events) == []

    decision = _ok(
        client.post(
            APPLY,
            json={
                **body,
                "fingerprint": at_template["fingerprint"],
                "scope": template,
                "confirm": True,
            },
        )
    )
    assert decision["scope"] == template
    assert decision["fingerprint"] == at_template["fingerprint"]


@pytest.mark.usefixtures("verified")
def test_applying_needs_a_strict_confirmation_and_a_scope_the_finding_is_in(
    client: TestClient, model: str, events: list[Event]
) -> None:
    output_id = make_output(client, model)
    body: dict[str, Any] = {
        "target": {"output_id": output_id},
        "diagnostic_key": "SB9001",
        "fix_id": "timelapse",
    }
    preview = _ok(client.post(PREVIEW, json=body))
    assert preview["applicable"] is True and preview["outward"] is True
    apply = {**body, "fingerprint": preview["fingerprint"]}
    events.clear()

    unconfirmed = _ok(client.post(APPLY, json=apply), 428)
    assert unconfirmed["type"].endswith("/confirmation-required")
    assert "timelapse" in unconfirmed["summary"]
    _ok(client.post(APPLY, json={**apply, "confirm": "true"}), 422)
    _ok(client.post(APPLY, json={**apply, "confirm": 1}), 422)
    _ok(client.post(APPLY, json={**apply, "confirm": True, "approved": True}), 422)
    elsewhere = {**apply, "confirm": True, "scope": {"kind": "template", "key": "other"}}
    assert _ok(client.post(APPLY, json=elsewhere), 422)["type"].endswith("/analyzer-scope")
    assert _decisions(events) == []

    decision = _ok(client.post(APPLY, json={**apply, "confirm": True}))
    assert decision["kind"] == "accept"
    assert decision["scope"] == {"kind": "print", "key": output_id}
    assert decision["changes"][0]["setting"] == "timelapse"
    [event] = [event for event in events if event.kind == "analyzer.decision"]
    assert event.model_dump(exclude={"id", "at"}) == {
        "kind": "analyzer.decision",
        "decision_id": decision["id"],
        "diagnostic_id": "SB9001",
        "scope": "print",
        "scope_key": output_id,
        "action": "recorded",
    }

    report = _run(client, output_id)
    row = next(row for row in report["diagnostics"] if row["id"] == "SB9001")
    assert row["status"] == "accepted"
    assert [line["change"]["setting"] for line in report["accepted_changes"]] == ["timelapse"]
    assert report["summary"]["suggestions"] == 0


def test_a_decision_must_name_a_known_rule_a_matching_instance_and_a_reason(
    client: TestClient,
) -> None:
    body: dict[str, Any] = {
        "diagnostic_id": "SB1003",
        "kind": "suppress",
        "scope": {"kind": "template", "key": "demo"},
        "reason": "ok",
    }
    for bad in (
        {"reason": None},
        {"reason": "  "},
        {"diagnostic_id": "X1"},
        # An empty instance would silently replace the rule-wide decision.
        {"instance": ""},
        # An instance of another rule.
        {"instance": "SB1002"},
        {"instance": "SB10031"},
        {"unknown": True},
    ):
        _ok(client.post(DECISIONS, json={**body, **bad}), 422)
    wrong_scope = {**body, "scope": {"kind": "print", "key": "demo"}}
    assert _ok(client.post(DECISIONS, json=wrong_scope), 422)["type"].endswith("/analyzer-scope")
    unknown = _ok(client.post(DECISIONS, json={**body, "diagnostic_id": "SB9999"}), 422)
    assert unknown["type"].endswith("/analyzer-unknown-rule")

    _ok(client.post(DECISIONS, json={**body, "instance": "SB1003"}), 201)
    _ok(client.post(DECISIONS, json={**body, "instance": "SB1003:part-1"}), 201)


def test_enforcing_the_suppression_of_an_error_needs_a_confirmation(client: TestClient) -> None:
    body: dict[str, Any] = {
        "diagnostic_id": "SB1001",
        "kind": "suppress",
        "scope": {"kind": "global"},
        "reason": "our meshes are fine",
        "enforced": True,
    }
    problem = _ok(client.post(DECISIONS, json=body), 428)
    assert problem["type"].endswith("/confirmation-required")
    assert _ok(client.post(DECISIONS, json={**body, "confirm": True}), 201)["enforced"] is True
    # Not enforced, or not an error: no confirmation needed.
    _ok(
        client.post(
            DECISIONS,
            json={**body, "enforced": False, "scope": {"kind": "template", "key": "demo"}},
        ),
        201,
    )
    _ok(client.post(DECISIONS, json={**body, "diagnostic_id": "SB1003"}), 201)


@pytest.mark.usefixtures("verified")
def test_decisions_are_recorded_replaced_listed_and_removed_with_events(
    client: TestClient, model: str, events: list[Event]
) -> None:
    output_id = make_output(client, model)
    events.clear()
    first = _ok(
        client.post(
            DECISIONS,
            json={
                "diagnostic_id": "SB9001",
                "kind": "ignore",
                "scope": {"kind": "template", "key": model},
            },
        ),
        201,
    )
    created = _ok(
        client.post(
            DECISIONS,
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
            DECISIONS,
            json={"diagnostic_id": "SB1003", "kind": "ignore", "scope": {"kind": "global"}},
        ),
        201,
    )

    everything = _ok(client.get(DECISIONS))
    assert [row["id"] for row in everything] == [ignored["id"], created["id"]]
    at_template = _ok(client.get(DECISIONS, params={"scope": "template"}))
    assert [row["id"] for row in at_template] == [created["id"]]
    _ok(client.get(DECISIONS, params={"scope_key": model}), 422)

    simple = _run(client, output_id)
    assert all(row["id"] != "SB9001" for row in simple["diagnostics"])
    advanced = _run(client, output_id, detail="advanced")
    row = next(row for row in advanced["diagnostics"] if row["id"] == "SB9001")
    assert row["status"] == "suppressed"
    assert row["decision"]["decision"]["reason"] == "keychains are always tiny"

    _ok(client.delete(f"{DECISIONS}/{created['id']}"), 204)
    _ok(client.delete(f"{DECISIONS}/{created['id']}"), 404)
    # The replaced decision is announced removed before its replacement is recorded.
    assert _decisions(events) == [
        {"decision_id": first["id"], "action": "recorded"},
        {"decision_id": first["id"], "action": "removed"},
        {"decision_id": created["id"], "action": "recorded"},
        {"decision_id": ignored["id"], "action": "recorded"},
        {"decision_id": created["id"], "action": "removed"},
    ]
    after = _run(client, output_id)
    assert any(row["id"] == "SB9001" for row in after["diagnostics"])
