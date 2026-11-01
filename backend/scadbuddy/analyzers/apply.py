"""Accepted analyzer fixes on a print (#284): checked again, then laid over the request.

A print names the fixes it applies by decision id (``PrintRunRequest.accepted_fixes``).
Before its run starts, the analyzers run again over that very request and the stored
decisions are resolved as a report resolves them (``runner.apply_decisions``): a fix
applies only while its decision still decides its finding for this print and its diff
is unchanged. Anything else refuses the print, naming each fix, rather than printing
without it or with a diff nobody saw.

Only the verified targets land, and only by changing the request itself, so both slicer
backends get them through the one path every choice already takes: ``print_options``
over the request's options overlay (#88), and ``print_request`` over its own choices
(``choices.bed_type``). A change whose target is still on AI spec §3.2's list
(``model.UNVERIFIED_TARGETS``) is refused, never dropped.

The print carrying them is itself the outward action of AI spec §8.2: the dialog's
Print, or the agent's ``print_output`` behind its approval, which names the fixes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import status
from pydantic import ValidationError

from scadbuddy.analyzers.model import (
    UNVERIFIED_TARGETS,
    AnalyzerDiagnostic,
    Fix,
    SettingChange,
)
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.resolver import PrintChoices
from scadbuddy.core.problems import ApiError

#: Problem ``type`` URIs, shared with ``api/analyzers.py``'s apply.
UNVERIFIED_PROBLEM = "https://scadbuddy.dev/problems/analyzer-fix-unverified"
STALE_PROBLEM = "https://scadbuddy.dev/problems/analyzer-fix-stale"
UNAPPLICABLE_PROBLEM = "https://scadbuddy.dev/problems/analyzer-fix-unapplicable"

#: The ``print_request`` settings a fix may set, by the ``PrintChoices`` field each is.
REQUEST_SETTINGS: dict[str, str] = {"choices.bed_type": "bed_type"}
#: Options a fix may not set: the run's own controls for them (``copies``, the project
#: picker) win, and the run drops a remembered ``project_id`` (``plan_run``).
FIXED_OPTIONS = frozenset({"quantity", "project_id"})


def accepted_fixes(ids: Sequence[str], diagnostics: Sequence[AnalyzerDiagnostic]) -> list[Fix]:
    """The fix each decision in ``ids`` accepted, from ``diagnostics`` after
    ``apply_decisions``; 409 ``analyzer-fix-stale`` naming every one that no longer
    holds for this print."""
    stale: list[str] = []
    fixes: list[Fix] = []
    for decision_id in ids:
        decided = [
            row
            for row in diagnostics
            if row.decision is not None and row.decision.decision.id == decision_id
        ]
        if not decided:
            stale.append(
                f"{decision_id}: not accepted for this print (it was removed, its finding is "
                "no longer reported, or another decision decides it)"
            )
            continue
        for row in decided:
            applied = row.decision
            assert applied is not None
            if applied.decision.kind != "accept":
                stale.append(f"{decision_id} ({row.key}): a {applied.decision.kind}, not a fix")
            elif applied.stale:
                stale.append(f"{decision_id} ({row.key}): its diff changed since it was accepted")
            else:
                fix = next(item for item in row.fixes if item.id == applied.decision.fix_id)
                fixes.append(fix)
    if stale:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "an accepted fix no longer applies to this print; run the checks again",
            type_=STALE_PROBLEM,
            stale=stale,
        )
    return fixes


def _unapplicable(detail: str) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, detail, type_=UNAPPLICABLE_PROBLEM)


def amend(request: PrintRunRequest, changes: Sequence[SettingChange]) -> PrintRunRequest:
    """``request`` with ``changes`` laid over its options and choices. Refused (409),
    with nothing applied: a target still unverified, a setting the print has no place
    for, a value that place refuses, or two fixes setting one setting differently."""
    blockers = list(
        dict.fromkeys(
            UNVERIFIED_TARGETS.get(item.target) or item.to_verify or item.target
            for item in changes
            if not item.verified or item.target in UNVERIFIED_TARGETS
        )
    )
    if blockers:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "an accepted fix sets something whose landing is not verified yet",
            type_=UNVERIFIED_PROBLEM,
            to_verify=blockers,
        )
    options: dict[str, Any] = request.options.queue_fields()
    choices: dict[str, Any] = request.choices.model_dump(mode="python")
    set_by_fix: dict[tuple[str, str], Any] = {}
    for item in changes:
        place = (item.target, item.setting)
        if place in set_by_fix and set_by_fix[place] != item.proposed:
            raise _unapplicable(
                f"two accepted fixes set {item.setting} [{item.target}] to different values"
            )
        set_by_fix[place] = item.proposed
        if item.target == "print_options":
            if item.setting not in PrintOptions.model_fields or item.setting in FIXED_OPTIONS:
                raise _unapplicable(f"a print has no option {item.setting!r} a fix can set")
            options[item.setting] = item.proposed
        elif item.target == "print_request" and item.setting in REQUEST_SETTINGS:
            choices[REQUEST_SETTINGS[item.setting]] = item.proposed
        else:
            raise _unapplicable(f"a print has no {item.setting!r} [{item.target}] a fix can set")
    try:
        amended_options = PrintOptions.model_validate(options)
        amended_choices = PrintChoices.model_validate(choices)
    except ValidationError as error:
        raise _unapplicable(
            f"an accepted fix sets a value the print refuses: {error.errors()[0]['msg']}"
        ) from None
    return request.model_copy(update={"options": amended_options, "choices": amended_choices})
