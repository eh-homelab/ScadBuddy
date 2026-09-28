"""``/api/v1/analyzers/…`` — print analyzers, their fixes, and decisions (#284).

Run the bundled analyzers over an output or a configuration, preview a fix, apply
one, and record or remove a decision at a scope. Agent tools (#368) wrap these; the
UI is a later story.

**Applying is outward.** Every fix target lands in Bambuddy when the print is sent
(AI spec §8.1), so applying is the two-step "prepare, then confirm" of §8.2: the
preview returns the diff and a fingerprint of it, and the apply must carry that
fingerprint back with ``confirm: true``. A diff that changed in between is refused
rather than applied unseen. A target still on the AI spec's §3.2 "to verify" list
cannot be applied at all; the problem names the item it waits on.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Response, status
from pydantic import BaseModel, Field, model_validator

from scadbuddy.analyzers.context import AnalysisContext, AnalysisRequest
from scadbuddy.analyzers.decisions import new_decision_id, valid_scope
from scadbuddy.analyzers.gather import gather_context
from scadbuddy.analyzers.model import (
    SCOPE_ORDER,
    AnalyzerDiagnostic,
    Decision,
    Fix,
    ScopeKind,
    ScopeRef,
    fingerprint,
)
from scadbuddy.analyzers.runner import (
    AnalysisReport,
    AnalyzerInfo,
    Detail,
    build_report,
    describe,
    run_checks,
)
from scadbuddy.api.deps import (
    CatalogueDep,
    DecisionsDep,
    EventsDep,
    OutputsDep,
    SettingsStoreDep,
)
from scadbuddy.api.models import require_model_exists
from scadbuddy.api.outputs import require_output
from scadbuddy.core.events import AnalyzerDecisionEvent, EventBus, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.outputs import OUTPUT_ID_PATTERN, OutputMeta, OutputStore
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.library.slugs import MAX_MODEL_ID_LENGTH, MODEL_ID_PATTERN
from scadbuddy.render.schema import ParamValue

router = APIRouter(prefix="/analyzers", tags=["analyzers"])

#: Problem ``type`` URIs, as ``bambuddy/errors.py`` spells its own.
UNVERIFIED_PROBLEM = "https://scadbuddy.dev/problems/analyzer-fix-unverified"
STALE_PROBLEM = "https://scadbuddy.dev/problems/analyzer-fix-stale"
CONFIRMATION_PROBLEM = "https://scadbuddy.dev/problems/confirmation-required"
SCOPE_PROBLEM = "https://scadbuddy.dev/problems/analyzer-scope"

DIAGNOSTIC_ID_PATTERN = r"^SB[0-9]{4}$"
ROUTE_NOTE = (
    "Accepting a settings diff sends this print by slicing and queueing rather than "
    "running the pipeline, because a pipeline run carries no per-print settings "
    "(AI spec §11, print-flow spec §2)."
)


class AnalysisTarget(BaseModel):
    """An output, or a configuration: a template and one parameter set.

    A configuration is judged on the newest output rendered with exactly those
    parameters when there is one; without one, the analyzers that need a render are
    skipped and say so.
    """

    output_id: str | None = Field(default=None, pattern=OUTPUT_ID_PATTERN)
    slug: str | None = Field(default=None, pattern=MODEL_ID_PATTERN, max_length=MAX_MODEL_ID_LENGTH)
    params: dict[str, ParamValue] | None = None

    @model_validator(mode="after")
    def _one_subject(self) -> AnalysisTarget:
        if (self.output_id is None) == (self.slug is None):
            raise ValueError("name either an output_id or a slug (with its params), not both")
        if self.output_id is not None and self.params is not None:
            raise ValueError("an output's parameters are its own; params go with a slug")
        return self


class AnalysisRun(BaseModel):
    target: AnalysisTarget
    request: AnalysisRequest = Field(default_factory=AnalysisRequest)
    detail: Detail = "simple"


class FixRequest(BaseModel):
    target: AnalysisTarget
    request: AnalysisRequest = Field(default_factory=AnalysisRequest)
    #: A diagnostic's ``key`` from the run (``SB2001``, ``SB3002:slot-2``).
    diagnostic_key: str = Field(max_length=100)
    fix_id: str = Field(max_length=100)


class FixPreview(BaseModel):
    diagnostic_id: str
    diagnostic_key: str
    fix: Fix
    #: What an apply must carry back, so it lands exactly this diff.
    fingerprint: str
    #: The change reaches Bambuddy, so applying needs ``confirm: true``.
    outward: bool
    #: False while any change waits on an AI spec §3.2 item; ``blockers`` names them.
    applicable: bool
    blockers: list[str]
    #: A human-readable account of the diff, for the confirmation card.
    summary: str
    route_note: str = ROUTE_NOTE


class FixApply(FixRequest):
    fingerprint: str = Field(min_length=64, max_length=64)
    confirm: bool = False
    #: Where to remember the acceptance: one of the run's ``scopes``. Omitted means the
    #: narrowest, this print (or this configuration, when it has no render yet), which
    #: is simple mode's default in #284.
    scope: ScopeRef | None = None
    reason: str | None = Field(default=None, max_length=500)


class DecisionCreate(BaseModel):
    """Ignore or suppress a diagnostic at a scope. Accepting goes through a fix's
    apply, which is where its diff is confirmed."""

    diagnostic_id: str = Field(pattern=DIAGNOSTIC_ID_PATTERN)
    #: One instance (a diagnostic ``key``), or ``null`` for every instance of the rule.
    instance: str | None = Field(default=None, max_length=100)
    kind: Literal["ignore", "suppress"]
    scope: ScopeRef
    reason: str | None = Field(default=None, max_length=500)
    enforced: bool = False

    @model_validator(mode="after")
    def _suppress_says_why(self) -> DecisionCreate:
        if self.kind == "suppress" and not (self.reason and self.reason.strip()):
            raise ValueError("a suppression needs a reason, as #pragma warning disable does")
        return self


def _require_scope(scope: ScopeRef) -> None:
    if not valid_scope(scope):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{scope.key!r} is not a {scope.kind} scope key",
            type_=SCOPE_PROBLEM,
        )


def _subject(
    target: AnalysisTarget, outputs: OutputStore, catalogue: Catalogue
) -> tuple[str, dict[str, ParamValue], OutputMeta | None]:
    if target.output_id is not None:
        output = require_output(outputs, target.output_id)
        return output.slug, outputs.params(output.id), output
    assert target.slug is not None
    require_model_exists(catalogue, target.slug)
    params = target.params or {}
    meta = next(
        (row for row in outputs.list_for(target.slug) if outputs.params(row.id) == params),
        None,
    )
    return target.slug, params, meta


async def _context(
    target: AnalysisTarget,
    request: AnalysisRequest,
    outputs: OutputStore,
    catalogue: Catalogue,
    store: SettingsStore,
) -> AnalysisContext:
    slug, params, meta = await asyncio.to_thread(_subject, target, outputs, catalogue)
    return await gather_context(
        outputs=outputs,
        settings=store.load(),
        slug=slug,
        params=params,
        request=request,
        meta=meta,
    )


def _find_fix(context: AnalysisContext, body: FixRequest) -> tuple[AnalyzerDiagnostic, Fix]:
    diagnostics, _ = run_checks(context)
    diagnostic = next((row for row in diagnostics if row.key == body.diagnostic_key), None)
    if diagnostic is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"{body.diagnostic_key} is not reported for this print (any more)",
        )
    fix = next((row for row in diagnostic.fixes if row.id == body.fix_id), None)
    if fix is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"{body.diagnostic_key} offers no fix {body.fix_id!r}",
        )
    return diagnostic, fix


def _describe_change(fix: Fix) -> str:
    lines = []
    for item in fix.changes:
        slot = f" (slot {item.slot_id})" if item.slot_id is not None else ""
        base = repr(item.base) if item.base_known else "unknown"
        unit = f" {item.unit}" if item.unit else ""
        lines.append(f"{item.setting}{slot}: {base} \u2192 {item.proposed!r}{unit} [{item.target}]")
    return "; ".join(lines)


def _publish(events: EventBus, decision: Decision, action: Literal["recorded", "removed"]) -> None:
    emit(
        events,
        AnalyzerDecisionEvent(
            decision_id=decision.id,
            diagnostic_id=decision.diagnostic_id,
            scope=decision.scope.kind,
            scope_key=decision.scope.key,
            action=action,
        ),
    )


@router.get("", response_model=list[AnalyzerInfo], summary="Every analyzer, with its sources")
def list_analyzers() -> list[AnalyzerInfo]:
    """The bundled analyzers: id, severity, category, the inputs each needs, the fixes
    it can offer, and the sources it cites (URL and quoted line)."""
    return describe()


@router.post("/run", response_model=AnalysisReport, summary="Run the analyzers")
async def post_run(
    body: AnalysisRun,
    outputs: OutputsDep,
    catalogue: CatalogueDep,
    store: SettingsStoreDep,
    decisions: DecisionsDep,
) -> AnalysisReport:
    """Judge an output or a configuration against the print request it would go out
    with (the #84 base: pipeline, printer, filament plan, plate, options).

    Reads only: nothing is uploaded, sliced or queued. An input that cannot be read
    (no Bambuddy, no pipeline, an output not uploaded yet, a missing API-key scope) is
    listed in ``inputs`` with the reason, and the analyzers needing it in ``skipped``.

    ``detail=simple`` returns the headline and the open findings with their sources and
    fixes; ``advanced`` adds evidence, locations, explanations, and the suppressed,
    ignored and ``hidden`` findings with the decision behind each.
    """
    context = await _context(body.target, body.request, outputs, catalogue, store)
    stored = await asyncio.to_thread(decisions.list, scopes=context.scopes())
    return build_report(context, stored, detail=body.detail)


@router.post("/fixes/preview", response_model=FixPreview, summary="Preview a fix")
async def post_preview(
    body: FixRequest,
    outputs: OutputsDep,
    catalogue: CatalogueDep,
    store: SettingsStoreDep,
) -> FixPreview:
    """The fix's whole diff, where each line lands, whether it can be applied yet, and
    the fingerprint an apply confirms against. Changes nothing."""
    context = await _context(body.target, body.request, outputs, catalogue, store)
    diagnostic, fix = _find_fix(context, body)
    blockers = fix.blockers
    return FixPreview(
        diagnostic_id=diagnostic.id,
        diagnostic_key=diagnostic.key,
        fix=fix,
        fingerprint=fingerprint(diagnostic.key, fix),
        outward=fix.outward,
        applicable=not blockers,
        blockers=blockers,
        summary=_describe_change(fix),
    )


@router.post("/fixes/apply", response_model=Decision, summary="Apply a previewed fix")
async def post_apply(
    body: FixApply,
    outputs: OutputsDep,
    catalogue: CatalogueDep,
    store: SettingsStoreDep,
    decisions: DecisionsDep,
    events: EventsDep,
) -> Decision:
    """Accept the fix at ``scope``: its diff joins this print's effective diff, and
    every later print that falls in the same scope, until the diff changes.

    Refused, in this order: a diff that differs from the previewed ``fingerprint``
    (409, ``analyzer-fix-stale``); a change whose target is still unverified (409,
    ``analyzer-fix-unverified``, naming the §3.2 items in ``to_verify``); an outward
    change without ``confirm: true`` (428, ``confirmation-required``); a scope this
    print does not fall in (422).
    """
    if body.scope is not None:
        _require_scope(body.scope)
    context = await _context(body.target, body.request, outputs, catalogue, store)
    scopes = context.scopes()
    scope = body.scope or scopes[-1]
    diagnostic, fix = _find_fix(context, body)
    current = fingerprint(diagnostic.key, fix)
    if current != body.fingerprint:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "the fix's diff has changed since it was previewed; preview it again",
            # No new fingerprint here: confirming means confirming a diff that was shown.
            type_=STALE_PROBLEM,
        )
    blockers = fix.blockers
    if blockers:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "this fix cannot be applied until where its settings land is verified",
            type_=UNVERIFIED_PROBLEM,
            to_verify=blockers,
        )
    if fix.outward and not body.confirm:
        raise ApiError(
            428,
            "this fix changes what is sent to Bambuddy; confirm it to apply",
            title="Precondition Required",
            type_=CONFIRMATION_PROBLEM,
            summary=_describe_change(fix),
        )
    if scope not in scopes:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"this print is not in the {scope.kind} scope {scope.key!r}",
            type_=SCOPE_PROBLEM,
        )
    decision = Decision(
        id=new_decision_id(),
        diagnostic_id=diagnostic.id,
        instance=diagnostic.key,
        kind="accept",
        scope=scope,
        reason=body.reason,
        fix_id=fix.id,
        fingerprint=current,
        changes=fix.changes,
    )
    await asyncio.to_thread(decisions.put, decision)
    _publish(events, decision, "recorded")
    return decision


@router.get("/decisions", response_model=list[Decision], summary="Recorded decisions")
def list_decisions(
    decisions: DecisionsDep,
    scope: Annotated[ScopeKind | None, Query()] = None,
    scope_key: Annotated[str | None, Query(max_length=200)] = None,
    diagnostic_id: Annotated[str | None, Query(pattern=DIAGNOSTIC_ID_PATTERN)] = None,
) -> list[Decision]:
    """Every decision, or those at one scope kind (and key), or about one rule.
    Broadest scope first, then oldest first."""
    if scope_key is not None and scope is None:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "scope_key needs the scope it belongs to"
        )
    found = [
        row
        for row in decisions.list(diagnostic_id=diagnostic_id)
        if (scope is None or row.scope.kind == scope)
        and (scope_key is None or row.scope.key == scope_key)
    ]
    return sorted(found, key=lambda row: (SCOPE_ORDER.index(row.scope.kind), row.created_at))


@router.post(
    "/decisions",
    response_model=Decision,
    status_code=status.HTTP_201_CREATED,
    summary="Ignore or suppress a diagnostic at a scope",
)
def post_decision(body: DecisionCreate, decisions: DecisionsDep, events: EventsDep) -> Decision:
    """Replaces an earlier decision about the same rule and instance at the same scope.

    ScadBuddy's own state, reversible by deleting it, so no confirmation (AI spec
    §8.1's ``write`` tier). ``enforced`` makes a broad decision win over narrower ones.
    """
    _require_scope(body.scope)
    decision = Decision(
        id=new_decision_id(),
        diagnostic_id=body.diagnostic_id,
        instance=body.instance,
        kind=body.kind,
        scope=body.scope,
        reason=body.reason.strip() if body.reason else None,
        enforced=body.enforced,
    )
    decisions.put(decision)
    _publish(events, decision, "recorded")
    return decision


@router.delete(
    "/decisions/{decision_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a decision",
)
def delete_decision(
    decision_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    decisions: DecisionsDep,
    events: EventsDep,
) -> Response:
    gone = decisions.remove(decision_id)
    if gone is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no decision with id {decision_id!r}")
    _publish(events, gone, "removed")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
