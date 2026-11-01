"""Accepted fixes on a print (#284): checked against the run's findings, then laid over
the request, only in the verified targets (``analyzers/apply.py``)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from scadbuddy.analyzers.apply import (
    STALE_PROBLEM,
    UNAPPLICABLE_PROBLEM,
    UNVERIFIED_PROBLEM,
    accepted_fixes,
    amend,
)
from scadbuddy.analyzers.model import (
    AnalyzerDiagnostic,
    AppliedDecision,
    Decision,
    Fix,
    ScopeRef,
    SettingChange,
    Source,
    change,
    diff_digest,
)
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.runs import run_key
from scadbuddy.core.problems import ApiError

SOURCE = Source(url="https://example.invalid/", title="t", quote="q", accessed=date(2026, 11, 1))
DECISION_ID = "a" * 32


def _request(**extra: Any) -> PrintRunRequest:
    return PrintRunRequest.model_validate(
        {
            "printer_id": 1,
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
            "choices": {"nozzles": [{"size": "0.4"}], "bed_type": "Textured PEI Plate"},
            **extra,
        }
    )


def _fix(*changes: SettingChange) -> Fix:
    return Fix(id="fix", title="Fix", description="", changes=list(changes))


def _accepted(fix: Fix, *, stale: bool = False, kind: str = "accept") -> AnalyzerDiagnostic:
    decision = Decision(
        id=DECISION_ID,
        diagnostic_id="SB9001",
        instance="SB9001",
        kind=kind,  # type: ignore[arg-type]
        scope=ScopeRef(kind="print", key="o"),
        reason="r" if kind == "suppress" else None,
        fix_id=fix.id if kind == "accept" else None,
        diff_digest=diff_digest(fix),
        changes=fix.changes,
    )
    return AnalyzerDiagnostic(
        id="SB9001",
        key="SB9001",
        title="t",
        severity="info",
        category="profile",
        message="m",
        sources=[SOURCE],
        fixes=[fix],
        status="accepted" if kind == "accept" and not stale else "open",
        decision=AppliedDecision(decision=decision, stale=stale),
    )


def _refused(error: pytest.ExceptionInfo[ApiError], type_: str) -> dict[str, Any]:
    assert error.value.status == 409
    assert error.value.type == type_
    return error.value.extensions


def test_an_accepted_fix_lands_in_the_options_and_the_choices() -> None:
    fix = _fix(
        change("print_options", "timelapse", True, sources=[SOURCE]),
        change("print_request", "choices.bed_type", "Cool Plate", sources=[SOURCE]),
    )
    request = _request(options={"timelapse": False, "bed_levelling": "off"})
    (found,) = accepted_fixes([DECISION_ID], [_accepted(fix)])
    amended = amend(request, found.changes)
    assert amended.options.timelapse is True
    assert amended.options.bed_levelling == "off"  # what the request set stays
    assert amended.choices.bed_type == "Cool Plate"
    assert amended.choices.nozzles == request.choices.nozzles
    assert request.options.timelapse is False  # the sent request is not changed


@pytest.mark.parametrize(
    ("diagnostics", "reason"),
    [
        ([], "not accepted for this print"),
        ([_accepted(_fix(), stale=True)], "its diff changed"),
        ([_accepted(_fix(), kind="suppress")], "a suppress, not a fix"),
    ],
)
def test_a_fix_that_no_longer_holds_refuses_the_print(
    diagnostics: list[AnalyzerDiagnostic], reason: str
) -> None:
    with pytest.raises(ApiError) as error:
        accepted_fixes([DECISION_ID], diagnostics)
    (stale,) = _refused(error, STALE_PROBLEM)["stale"]
    assert stale.startswith(DECISION_ID) and reason in stale


def test_an_unverified_target_is_refused_never_dropped() -> None:
    changes = [
        change("print_options", "timelapse", True, sources=[SOURCE]),
        change("derived_process_preset", "enable_support", "1", sources=[SOURCE]),
    ]
    with pytest.raises(ApiError) as error:
        amend(_request(), changes)
    (blocker,) = _refused(error, UNVERIFIED_PROBLEM)["to_verify"]
    assert "local-presets" in blocker


@pytest.mark.parametrize(
    "changes",
    [
        # Nothing on the request takes these: an unknown option, the run's own controls,
        # a request setting outside the table, a value the field refuses.
        [change("print_options", "no_such_option", True, sources=[SOURCE])],
        [change("print_options", "quantity", 3, sources=[SOURCE])],
        [change("print_request", "printer_id", 2, sources=[SOURCE])],
        [change("print_options", "preheat_chamber_target_override", 99, sources=[SOURCE])],
        # Two fixes disagreeing on one setting.
        [
            change("print_options", "timelapse", True, sources=[SOURCE]),
            change("print_options", "timelapse", False, sources=[SOURCE]),
        ],
    ],
)
def test_a_change_the_print_cannot_take_is_refused(changes: list[SettingChange]) -> None:
    with pytest.raises(ApiError) as error:
        amend(_request(), changes)
    _refused(error, UNAPPLICABLE_PROBLEM)


def test_no_fixes_keep_the_run_key_and_fixes_are_one_key_in_any_order() -> None:
    plain = _request()
    assert run_key("o", plain) == run_key("o", _request(accepted_fixes=[]))
    one = _request(accepted_fixes=["b" * 32, DECISION_ID, "b" * 32])
    assert one.accepted_fixes == [DECISION_ID, "b" * 32]
    assert run_key("o", one) == run_key("o", _request(accepted_fixes=[DECISION_ID, "b" * 32]))
    assert run_key("o", one) != run_key("o", plain)
