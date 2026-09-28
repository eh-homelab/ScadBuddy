"""Decision scoping and both stores (#284 "Scopes")."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from scadbuddy.analyzers.context import (
    AnalysisContext,
    AnalysisRequest,
    FilamentSlot,
    base_profile,
    configuration_key,
)
from scadbuddy.analyzers.decisions import (
    DecisionStore,
    PostgresDecisionStore,
    new_decision_id,
    resolve,
    valid_scope,
)
from scadbuddy.analyzers.model import Decision, ScopeKind, ScopeRef, diff_digest
from scadbuddy.analyzers.runner import build_report
from scadbuddy.bambuddy.models import Printer
from tests.analyzers.conftest import (
    OUTPUT_ID,
    basic_slot,
    choices,
    context,
    geometry_of,
    output,
    silk_slot,
    tee,
)


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
        ("printer", "model:h2c"),
        ("printer", "id:1"),
        ("template", "demo"),
        ("template_version", f"demo@{'a' * 40}"),
        ("configuration", configuration_key("demo", {"width": 12})),
        ("print", OUTPUT_ID),
    ]
    assert all(valid_scope(scope) for scope in ctx.scopes())


def test_a_subtype_ranks_above_its_material_and_odd_names_still_make_valid_keys() -> None:
    odd = FilamentSlot(slot_id=2, spool_id=9, material="PETG (HF)", subtype="Tri Color/Matte")
    ctx = context(output=output(), filaments=[silk_slot(), odd])
    materials = [scope.key for scope in ctx.scopes() if scope.kind == "material"]
    assert materials == ["pla", "petg -hf", "pla/tri color", "petg -hf/tri color-matte"]
    assert all(valid_scope(scope) for scope in ctx.scopes())


def test_a_material_decision_applies_only_to_findings_about_that_material() -> None:
    # Silk in slot 1, PETG in slot 2: ignoring PETG must not hide the silk finding.
    petg = FilamentSlot(slot_id=2, spool_id=9, material="PETG", subtype="Basic")
    ctx = context(output=output(), filaments=[silk_slot(1), petg])
    petg_ignore = _decision("material", "petg", decision="ignore")
    report = build_report(ctx, [petg_ignore], detail="advanced")
    assert next(row for row in report.diagnostics if row.id == "SB2001").status == "open"

    silk_ignore = _decision("material", "pla/tri color", decision="ignore")
    plain_pla = _decision("material", "pla")
    report = build_report(ctx, [plain_pla, silk_ignore], detail="advanced")
    silk = next(row for row in report.diagnostics if row.id == "SB2001")
    # The subtype's decision beats the bare material's.
    assert silk.status == "ignored"
    assert silk.decision is not None and silk.decision.decision == silk_ignore


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
        diff_digest=diff_digest(fix),
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
    ctx.choices = ctx.request.choices
    ctx.base = base_profile(ctx.request, ctx.choices, ctx.printer, ctx.filaments, ctx.plate)
    report = build_report(ctx, [accepted], detail="advanced")
    silk = next(row for row in report.diagnostics if row.id == "SB2001")
    assert silk.status == "open"
    assert silk.decision is not None and silk.decision.stale
    assert report.accepted_changes == []


def test_the_report_lists_inputs_and_the_base() -> None:
    ctx = _silk_context()
    ctx.request = AnalysisRequest(copies=3, choices=choices("0.2", tier="fine"))
    ctx.choices = ctx.request.choices
    ctx.base = base_profile(ctx.request, ctx.choices, ctx.printer, ctx.filaments, ctx.plate)
    ctx.unavailable["inventory"] = "this output has not been uploaded to Bambuddy yet"
    report = build_report(ctx, [])
    inputs = {row.name: row for row in report.inputs}
    assert inputs["geometry"].available
    assert not inputs["inventory"].available
    assert inputs["inventory"].reason == "this output has not been uploaded to Bambuddy yet"
    base = report.base
    assert base.copies == 3 and base.bed_type == "Textured PEI Plate"
    assert base.printer_model == "H2C"
    # The resolver's own derivation (resolver.TIERS, printer_preset_name).
    assert base.printer_preset_name == "Bambu Lab H2C 0.2 nozzle"
    assert base.process_preset_name == "0.08mm High Quality @BBL H2C 0.2 nozzle"
    assert [(slot.slot_id, slot.spool_id, slot.preset) for slot in base.slots] == [
        (1, 5, "Bambu PLA Silk")
    ]


# --- the stores ---------------------------------------------------------------------


@pytest.fixture
def pg_store(pg_conninfo: str) -> Iterator[DecisionStore]:
    store = PostgresDecisionStore(pg_conninfo, pool_size=2)
    yield store
    store.close()


def _exercise(store: DecisionStore) -> None:
    assert store.list() == []
    first = _decision("template", "demo")
    first.created_at = datetime(2026, 9, 28, tzinfo=UTC)
    assert store.put(first) is None
    other = _decision("print", OUTPUT_ID, decision="ignore", diagnostic_id="SB1003")
    other.created_at = first.created_at + timedelta(seconds=1)
    store.put(other)
    assert [row.id for row in store.list()] == [first.id, other.id]
    assert store.list(scopes=[ScopeRef(kind="template", key="demo")]) == [first]
    assert store.list(diagnostic_id="SB1003") == [other]
    assert store.list(scopes=[]) == []
    assert store.get(first.id) == first

    # The same rule, instance and scope replaces rather than adds, and says what went.
    replacement = _decision("template", "demo", decision="ignore")
    assert store.put(replacement) == first
    assert store.get(first.id) is None
    assert {row.id for row in store.list()} == {replacement.id, other.id}

    assert store.remove(replacement.id) == replacement
    assert store.remove(replacement.id) is None
    assert store.list() == [other]


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
        diff_digest=diff_digest(silk.fixes[0]),
        changes=silk.fixes[0].changes,
    )
    pg_store.put(accepted)
    assert pg_store.get(accepted.id) == accepted


@pytest.mark.requires_postgres
def test_concurrent_puts_to_one_target_never_collide(pg_store: DecisionStore) -> None:
    """16 threads x 20 puts to the same rule, instance and scope: every put lands, each
    replaces exactly the one before it, and one row is left."""
    pg_store.list()  # open and migrate once, outside the race
    failures: list[BaseException] = []
    replaced: list[str] = []
    written: list[str] = []
    lock = threading.Lock()

    def writer() -> None:
        for _ in range(20):
            decision = _decision("template", "demo", decision="ignore")
            try:
                gone = pg_store.put(decision)
            except BaseException as error:
                with lock:
                    failures.append(error)
                continue
            with lock:
                written.append(decision.id)
                if gone is not None:
                    replaced.append(gone.id)

    threads = [threading.Thread(target=writer) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(written) == 320
    [survivor] = pg_store.list()
    # Every decision but the survivor was replaced exactly once.
    assert sorted(replaced) == sorted(set(written) - {survivor.id})


def test_a_basic_pla_finding_is_not_hidden_by_a_silk_decision() -> None:
    petg_silk = FilamentSlot(slot_id=1, spool_id=9, material="PLA", subtype="Silk")
    ctx = context(output=output(), filaments=[petg_silk, basic_slot(2)])
    scopes = ctx.scopes_for([2])
    assert ScopeRef(kind="material", key="pla/silk") not in scopes
    assert ScopeRef(kind="material", key="pla") in scopes
