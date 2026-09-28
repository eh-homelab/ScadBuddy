"""Running the analyzers over a context and resolving decisions into a report."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.analyzers import builtin
from scadbuddy.analyzers.builtin import CRASHED
from scadbuddy.analyzers.context import INPUT_NAMES, AnalysisContext, InputName
from scadbuddy.analyzers.decisions import resolve
from scadbuddy.analyzers.model import (
    Analyzer,
    AnalyzerDiagnostic,
    AppliedDecision,
    Category,
    Decision,
    DiagnosticLocation,
    Evidence,
    ScopeRef,
    SettingChange,
    Severity,
    Source,
    fingerprint,
)
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.pipelines import PipelineView

logger = logging.getLogger(__name__)

Detail = Literal["simple", "advanced"]
_SEVERITY_ORDER: dict[Severity, int] = {"error": 0, "warning": 1, "info": 2, "hidden": 3}


class InputStatus(BaseModel):
    name: InputName
    available: bool
    reason: str | None = None


class SkippedAnalyzer(BaseModel):
    id: str
    title: str
    #: The inputs it needed that could not be read, with why.
    missing: list[InputStatus]


class AcceptedChange(BaseModel):
    """One line of the effective diff: base + every accepted fix that still holds."""

    diagnostic_id: str
    diagnostic_key: str
    fix_id: str
    scope: ScopeRef
    change: SettingChange


class AnalysisSummary(BaseModel):
    """The simple mode's one line, and the counts behind it. Only open diagnostics
    count; ``hidden``, suppressed, ignored and accepted ones do not."""

    headline: str
    errors: int
    warnings: int
    suggestions: int


class BaseProfile(BaseModel):
    """What the diffs are against: the pipeline's presets, bed type and plan (#84)."""

    pipeline: PipelineView | None = None
    printer_id: int | None = None
    printer_model: str | None = None
    bed_type: str | None = None
    plate_id: int
    plate_model: str | None = None
    filament_plan: FilamentPlan | None = None
    copies: int


class AnalysisReport(BaseModel):
    output_id: str | None
    slug: str
    detail: Detail
    summary: AnalysisSummary
    diagnostics: list[AnalyzerDiagnostic]
    accepted_changes: list[AcceptedChange] = Field(default_factory=list)
    skipped: list[SkippedAnalyzer] = Field(default_factory=list)
    inputs: list[InputStatus]
    #: The scopes a decision about this print can be stored at, broadest first.
    scopes: list[ScopeRef]
    base: BaseProfile


class AnalyzerInfo(BaseModel):
    """One catalogue row: the rule, where it is defined and what it cites."""

    id: str
    name: str
    title: str
    severity: Severity
    category: Category
    description: str
    scope: str
    needs: list[InputName]
    fix_ids: list[str]
    sources: list[Source]


def describe(analyzers: Sequence[Analyzer] | None = None) -> list[AnalyzerInfo]:
    return [
        AnalyzerInfo(
            id=analyzer.id,
            name=analyzer.name,
            title=analyzer.title,
            severity=analyzer.severity,
            category=analyzer.category,
            description=analyzer.description,
            scope=analyzer.scope,
            needs=sorted(analyzer.needs),
            fix_ids=list(analyzer.fix_ids),
            sources=list(analyzer.sources),
        )
        for analyzer in _active(analyzers)
    ]


def _crashed(analyzer: Analyzer, error: Exception) -> AnalyzerDiagnostic:
    return CRASHED.diagnose(
        key=analyzer.id,
        message=f"{analyzer.id} ({analyzer.title}) failed and did not run: {type(error).__name__}.",
        location=DiagnosticLocation(kind="analyzer"),
        evidence=[Evidence(label="analyzer", value=analyzer.id, origin="runner")],
    )


def _active(analyzers: Sequence[Analyzer] | None) -> Sequence[Analyzer]:
    """The analyzers to run: the ones given, else the bundled set, read at call time."""
    return analyzers if analyzers is not None else builtin.BUILTIN


def run_checks(
    context: AnalysisContext, analyzers: Sequence[Analyzer] | None = None
) -> tuple[list[AnalyzerDiagnostic], list[SkippedAnalyzer]]:
    """Every analyzer whose inputs are there, a crash reported as ``SB0001``."""
    diagnostics: list[AnalyzerDiagnostic] = []
    skipped: list[SkippedAnalyzer] = []
    for analyzer in _active(analyzers):
        if analyzer is CRASHED:
            continue
        missing = [name for name in sorted(analyzer.needs) if not context.has(name)]
        if missing:
            skipped.append(
                SkippedAnalyzer(
                    id=analyzer.id,
                    title=analyzer.title,
                    missing=[
                        InputStatus(
                            name=name,
                            available=False,
                            reason=context.unavailable.get(name, "not provided"),
                        )
                        for name in missing
                    ],
                )
            )
            continue
        try:
            diagnostics.extend(analyzer.check(context, analyzer))
        except Exception as error:
            logger.exception("an analyzer failed", extra={"analyzer": analyzer.id})
            diagnostics.append(_crashed(analyzer, error))
    return diagnostics, skipped


def apply_decisions(
    diagnostics: list[AnalyzerDiagnostic], decisions: Sequence[Decision], scopes: Sequence[ScopeRef]
) -> list[AcceptedChange]:
    """Set each diagnostic's status from the decision that applies; return the
    effective diff. An accepted fix whose diff has changed is stale and open again."""
    accepted: list[AcceptedChange] = []
    for diagnostic in diagnostics:
        decision = resolve(diagnostic.id, diagnostic.key, decisions, scopes)
        if decision is None:
            continue
        if decision.kind == "suppress":
            diagnostic.status = "suppressed"
            diagnostic.decision = AppliedDecision(decision=decision)
            continue
        if decision.kind == "ignore":
            diagnostic.status = "ignored"
            diagnostic.decision = AppliedDecision(decision=decision)
            continue
        fix = next((row for row in diagnostic.fixes if row.id == decision.fix_id), None)
        current = fingerprint(diagnostic.key, fix) if fix is not None else None
        if current is None or current != decision.fingerprint:
            diagnostic.decision = AppliedDecision(decision=decision, stale=True)
            continue
        assert fix is not None
        diagnostic.status = "accepted"
        diagnostic.decision = AppliedDecision(decision=decision)
        accepted.extend(
            AcceptedChange(
                diagnostic_id=diagnostic.id,
                diagnostic_key=diagnostic.key,
                fix_id=fix.id,
                scope=decision.scope,
                change=item,
            )
            for item in fix.changes
        )
    return accepted


def summarise(diagnostics: Sequence[AnalyzerDiagnostic]) -> AnalysisSummary:
    open_ = [row for row in diagnostics if row.status == "open"]
    errors = sum(1 for row in open_ if row.severity == "error")
    warnings = sum(1 for row in open_ if row.severity == "warning")
    suggestions = sum(1 for row in open_ if row.severity == "info")
    parts = [
        f"{count} {noun}{'s' if count != 1 else ''}"
        for count, noun in ((errors, "problem"), (warnings, "warning"), (suggestions, "suggestion"))
        if count
    ]
    return AnalysisSummary(
        headline=", ".join(parts) if parts else "Nothing to report",
        errors=errors,
        warnings=warnings,
        suggestions=suggestions,
    )


def _simple(diagnostic: AnalyzerDiagnostic) -> AnalyzerDiagnostic | None:
    """What simple mode shows: open and accepted findings, message and sources, and
    each fix's diff. Evidence, location and the long explanation are advanced detail."""
    if diagnostic.severity == "hidden" or diagnostic.status in ("suppressed", "ignored"):
        return None
    return diagnostic.model_copy(update={"why": None, "evidence": [], "location": None})


def build_report(
    context: AnalysisContext,
    decisions: Sequence[Decision],
    *,
    detail: Detail = "simple",
    analyzers: Sequence[Analyzer] | None = None,
) -> AnalysisReport:
    scopes = context.scopes()
    diagnostics, skipped = run_checks(context, analyzers)
    accepted = apply_decisions(diagnostics, decisions, scopes)
    diagnostics.sort(key=lambda row: (_SEVERITY_ORDER[row.severity], row.key))
    summary = summarise(diagnostics)
    if detail == "simple":
        diagnostics = [shown for row in diagnostics if (shown := _simple(row)) is not None]
    printer = context.printer
    return AnalysisReport(
        output_id=context.output.id if context.output else None,
        slug=context.slug,
        detail=detail,
        summary=summary,
        diagnostics=diagnostics,
        accepted_changes=accepted,
        skipped=skipped,
        inputs=[
            InputStatus(
                name=name,
                available=context.has(name),
                reason=context.unavailable.get(name),
            )
            for name in INPUT_NAMES
        ],
        scopes=scopes,
        base=BaseProfile(
            pipeline=context.pipeline,
            printer_id=printer.id if printer else None,
            printer_model=printer.model if printer else None,
            bed_type=context.bed_type,
            plate_id=context.request.plate_id,
            plate_model=context.plate.model if context.plate else None,
            filament_plan=context.request.filament_plan,
            copies=context.copies,
        ),
    )
