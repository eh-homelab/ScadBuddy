"""Decision scoping and both stores (#284 "Scopes")."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scadbuddy.analyzers.context import AnalysisContext, AnalysisRequest, configuration_key
from scadbuddy.analyzers.decisions import (
    DecisionStore,
    FileDecisionStore,
    PostgresDecisionStore,
    new_decision_id,
    resolve,
    valid_scope,
)
from scadbuddy.analyzers.model import Decision, ScopeKind, ScopeRef, fingerprint
from scadbuddy.analyzers.runner import build_report
from scadbuddy.bambuddy.models import Printer
from tests.analyzers.conftest import OUTPUT_ID, context, geometry_of, output, silk_slot, tee


def _decision(
    kind: ScopeKind,
    key: str = "",
    *,
    decision: str = "suppress",
    instance: str | None = None,
    enforced: bool = False,
    diagnostic_id: str = "SB2001",
) -> Decision:
    return Decision(
        id=new_decision_id(),
        diagnostic_id=diagnostic_id,
        instance=instance,
        kind=decision,  # type: ignore[arg-type]
        scope=ScopeRef(kind=kind, key=key),
        reason="because" if decision == "suppress" else None,
        enforced=enforced,
    )


def _silk_context() -> AnalysisContext:
    return context(
        output=output(),
        filaments=[silk_slot()],
        printer=Printer(id=1, name="3DP-31B-598", model="H2C"),
        geometry=geometry_of(tee()),
    )


# --- the scopes a print falls in ----------------------------------------------------


def test_a_print_falls_in_every_scope_broadest_first() -> None:
    ctx = _silk_context()
    assert [(scope.kind, scope.key) for scope in ctx.scopes()] == [
        ("global", ""),
        ("material", "pla"),
        ("material", "pla/tri color"),
        ("printer", "model:H2C"),
        ("printer", "id:1"),
        ("template", "demo"),
        ("template_version", f"demo@{'a' * 40}"),
        ("configuration", configuration_key("demo", {"width": 12})),
        ("print", OUTPUT_ID),
    ]
    assert all(valid_scope(scope) for scope in ctx.scopes())


def test_the_configuration_key_ignores_parameter_order() -> None:
    assert configuration_key("demo", {"a": 1, "b": "x"}) == configuration_key(
        "demo", {"b": "x", "a": 1}
    )
    assert configuration_key("demo", {"a": 1}) != configuration_key("demo", {"a": 2})


@pytest.mark.parametrize(
    ("kind", "key", "valid"),
    [
        ("global", "", True),
        ("global", "x", False),
        ("material", "pla/silk", True),
        ("material", "PLA", False),
        ("printer", "id:3", True),
        ("printer", "model:A1 mini", True),
        ("printer", "3", False),
        ("template", "demo", True),
        ("template", "builtin:demo", True),
        ("template", "../etc", False),
        ("print", OUTPUT_ID, True),
        ("print", "nope", False),
    ],
)
def test_scope_keys_are_validated(kind: ScopeKind, key: str, valid: bool) -> None:
    assert valid_scope(ScopeRef(kind=kind, key=key)) is valid


# --- resolution ---------------------------------------------------------------------


def test_the_narrowest_scope_wins() -> None:
    scopes = _silk_context().scopes()
    broad = _decision("global", decision="ignore")
    narrow = _decision("template", "demo")
    assert resolve("SB2001", "SB2001", [broad, narrow], scopes) == narrow
    assert resolve("SB2001", "SB2001", [narrow, broad], scopes) == narrow


def test_a_printer_id_outranks_its_model_and_a_template_outranks_a_material() -> None:
    scopes = _silk_context().scopes()
    model = _decision("printer", "model:H2C")
    by_id = _decision("printer", "id:1", decision="ignore")
    material = _decision("material", "pla/tri color")
    template = _decision("template", "demo", decision="ignore")
    assert resolve("SB2001", "SB2001", [by_id, model], scopes) == by_id
    assert resolve("SB2001", "SB2001", [template, material], scopes) == template


def test_an_enforced_broad_decision_wins_over_narrower_ones() -> None:
    scopes = _silk_context().scopes()
    enforced = _decision("material", "pla", decision="ignore", enforced=True)
    narrow = _decision("print", OUTPUT_ID)
    assert resolve("SB2001", "SB2001", [enforced, narrow], scopes) == enforced


def test_an_instance_decision_wins_at_the_same_scope_and_others_are_untouched() -> None:
    scopes = _silk_context().scopes()
    every = _decision("template", "demo", diagnostic_id="SB3002")
    one = _decision(
        "template", "demo", decision="ignore", instance="SB3002:slot-1", diagnostic_id="SB3002"
    )
    assert resolve("SB3002", "SB3002:slot-1", [every, one], scopes) == one
    assert resolve("SB3002", "SB3002:slot-2", [every, one], scopes) == every


def test_a_decision_at_a_scope_the_print_is_not_in_does_not_apply() -> None:
    scopes = _silk_context().scopes()
    other = _decision("template", "someone-else")
    assert resolve("SB2001", "SB2001", [other], scopes) is None


def test_suppressed_and_ignored_findings_leave_the_simple_view_but_not_the_advanced() -> None:
    ctx = _silk_context()
    decisions = [
        _decision("template", "demo"),
        _decision("print", OUTPUT_ID, decision="ignore", diagnostic_id="SB1003"),
    ]
    simple = build_report(ctx, decisions, detail="simple")
    assert {row.id for row in simple.diagnostics} == set()
    assert simple.summary.headline == "Nothing to report"
    advanced = build_report(ctx, decisions, detail="advanced")
    statuses = {row.id: row.status for row in advanced.diagnostics}
    assert statuses == {"SB2001": "suppressed", "SB1003": "ignored"}
    assert all(row.decision is not None for row in advanced.diagnostics)


def test_an_accepted_fix_joins_the_effective_diff_until_its_diff_changes() -> None:
    ctx = _silk_context()
    report = build_report(ctx, [], detail="advanced")
    silk = next(row for row in report.diagnostics if row.id == "SB2001")
    fix = silk.fixes[0]
    accepted = Decision(
        id=new_decision_id(),
        diagnostic_id="SB2001",
        instance=silk.key,
        kind="accept",
        scope=ScopeRef(kind="template", key="demo"),
        fix_id=fix.id,
        fingerprint=fingerprint(silk.key, fix),
        changes=fix.changes,
    )
    report = build_report(ctx, [accepted], detail="advanced")
    assert next(row for row in report.diagnostics if row.id == "SB2001").status == "accepted"
    assert {line.change.setting for line in report.accepted_changes} == {
        "outer_wall_speed",
        "nozzle_temperature",
        "nozzle_temperature_initial_layer",
    }
    assert report.summary.suggestions == 1  # the overhang, still open

    # A second silk slot changes the diff: the acceptance is stale, the finding open.
    ctx.filaments.append(silk_slot(2))
    report = build_report(ctx, [accepted], detail="advanced")
    silk = next(row for row in report.diagnostics if row.id == "SB2001")
    assert silk.status == "open"
    assert silk.decision is not None and silk.decision.stale
    assert report.accepted_changes == []


def test_the_report_lists_inputs_and_the_base() -> None:
    ctx = _silk_context()
    ctx.request = AnalysisRequest(copies=3, bed_type="Textured PEI Plate")
    ctx.unavailable["eligibility"] = "this output has not been uploaded to Bambuddy yet"
    report = build_report(ctx, [])
    inputs = {row.name: row for row in report.inputs}
    assert inputs["geometry"].available
    assert not inputs["eligibility"].available
    assert inputs["eligibility"].reason == "this output has not been uploaded to Bambuddy yet"
    assert report.base.copies == 3 and report.base.bed_type == "Textured PEI Plate"
    assert report.base.printer_model == "H2C"


# --- the stores ---------------------------------------------------------------------


@pytest.fixture
def file_store(tmp_path: Path) -> DecisionStore:
    return FileDecisionStore(tmp_path / "analyzers")


@pytest.fixture
def pg_store(pg_conninfo: str) -> Iterator[DecisionStore]:
    store = PostgresDecisionStore(pg_conninfo, pool_size=2)
    yield store
    store.close()


def _exercise(store: DecisionStore) -> None:
    assert store.list() == []
    first = _decision("template", "demo")
    first.created_at = datetime(2026, 9, 28, tzinfo=UTC)
    store.put(first)
    other = _decision("print", OUTPUT_ID, decision="ignore", diagnostic_id="SB1003")
    other.created_at = first.created_at + timedelta(seconds=1)
    store.put(other)
    assert [row.id for row in store.list()] == [first.id, other.id]
    assert store.list(scopes=[ScopeRef(kind="template", key="demo")]) == [first]
    assert store.list(diagnostic_id="SB1003") == [other]
    assert store.list(scopes=[]) == []
    assert store.get(first.id) == first

    # The same rule, instance and scope replaces rather than adds.
    replacement = _decision("template", "demo", decision="ignore")
    store.put(replacement)
    assert store.get(first.id) is None
    assert {row.id for row in store.list()} == {replacement.id, other.id}

    assert store.remove(replacement.id) == replacement
    assert store.remove(replacement.id) is None
    assert store.list() == [other]


def test_the_file_store(file_store: DecisionStore, tmp_path: Path) -> None:
    _exercise(file_store)
    assert (tmp_path / "analyzers" / "decisions.json").is_file()
    # A second store over the same file sees the same decisions.
    assert FileDecisionStore(tmp_path / "analyzers").list() == file_store.list()


@pytest.mark.requires_postgres
def test_the_postgres_store(pg_store: DecisionStore) -> None:
    _exercise(pg_store)


@pytest.mark.requires_postgres
def test_the_postgres_store_keeps_an_accepted_diff_whole(pg_store: DecisionStore) -> None:
    ctx = _silk_context()
    report = build_report(ctx, [], detail="advanced")
    silk = next(row for row in report.diagnostics if row.id == "SB2001")
    accepted = Decision(
        id=new_decision_id(),
        diagnostic_id="SB2001",
        instance=silk.key,
        kind="accept",
        scope=ScopeRef(kind="print", key=OUTPUT_ID),
        fix_id=silk.fixes[0].id,
        fingerprint=fingerprint(silk.key, silk.fixes[0]),
        changes=silk.fixes[0].changes,
    )
    pg_store.put(accepted)
    assert pg_store.get(accepted.id) == accepted
