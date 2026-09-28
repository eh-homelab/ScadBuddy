"""``/api/v1/analyzers/…`` — print analyzers, their fixes, and decisions (#284).

Run the bundled analyzers over an output or a configuration, preview a fix, apply
one, and record or remove a decision at a scope. Agent tools (#368) wrap these; the
UI is a later story.

**Applying records a decision; it sends nothing.** Apply stores an ``accept`` row in
ScadBuddy's own database, which is the ``write`` tier (AI spec §8.1): reversible by
deleting it. Nothing reads ``accepted_changes`` into a print yet. The send path that
eventually does must put the diff through §8.2's outward approval (a pending action
and a human approval in the UI); ``confirm`` here is not that approval. What apply
does guarantee is that it lands exactly what was previewed: the preview returns a
fingerprint of the diff, the scope it will be stored at, the subject (output or
configuration) and the base it was judged against, and the apply must carry that
fingerprint back. A target still on the AI spec's §3.2 "to verify" list cannot be
applied at all; the problem names the item it waits on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Path, Query, Response, status
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from scadbuddy.analyzers import builtin
from scadbuddy.analyzers.context import AnalysisContext, AnalysisRequest
from scadbuddy.analyzers.decisions import DecisionStore, new_decision_id, valid_scope
from scadbuddy.analyzers.gather import gather_context
from scadbuddy.analyzers.model import (
    SCOPE_ORDER,
    AnalyzerDiagnostic,
    Decision,
    Fix,
    ScopeKind,
    ScopeRef,
    diff_digest,
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
    OptionalDecisionsDep,
    OutputsDep,
    SettingsStoreDep,
    UploadsDep,
)
from scadbuddy.api.models import require_model_exists
from scadbuddy.api.outputs import require_output
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, DatabaseRequiredError
from scadbuddy.core.events import AnalyzerDecisionEvent, EventBus, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.outputs import OUTPUT_ID_PATTERN, OutputMeta, OutputStore
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.library.slugs import MAX_MODEL_ID_LENGTH, MODEL_ID_PATTERN
from scadbuddy.render.schema import ParamValue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analyzers", tags=["analyzers"])

#: Problem ``type`` URIs, as ``bambuddy/errors.py`` spells its own.
UNVERIFIED_PROBLEM = "https://scadbuddy.dev/problems/analyzer-fix-unverified"
STALE_PROBLEM = "https://scadbuddy.dev/problems/analyzer-fix-stale"
CONFIRMATION_PROBLEM = "https://scadbuddy.dev/problems/confirmation-required"
SCOPE_PROBLEM = "https://scadbuddy.dev/problems/analyzer-scope"
UNKNOWN_RULE_PROBLEM = "https://scadbuddy.dev/problems/analyzer-unknown-rule"
DATABASE_UNAVAILABLE_PROBLEM = "https://scadbuddy.dev/problems/database-unavailable"

NO_DATABASE = (
    "decisions are stored in Postgres and SCADBUDDY_DATABASE_URL is not set, so none "
    "were applied and none can be recorded"
)
DIAGNOSTIC_ID_PATTERN = r"^SB[0-9]{4}$"
#: Where an applied diff would land, and what applying does today.
ROUTE_NOTE = (
    "Applying records this diff as a decision at its scope; nothing sends it yet. A "
    "print that uses it will have to slice and queue with the diff, since a pipeline "
    "run carries no per-print settings (AI spec §11), and will go through the outward "
    "approval of AI spec §8.2 before it does."
)

#: What a database that cannot be reached raises through the store.
DATABASE_ERRORS = (psycopg.OperationalError, PoolTimeout)


class AnalysisTarget(BaseModel):
    """An output, or a configuration: a template and one parameter set.

    A configuration is judged on the newest output rendered with exactly those
    parameters when there is one; without one, the analyzers that need a render are
    skipped and say so.
    """

    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

    target: AnalysisTarget
    request: AnalysisRequest = Field(default_factory=AnalysisRequest)
    detail: Detail = "simple"


class FixRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: AnalysisTarget
    request: AnalysisRequest = Field(default_factory=AnalysisRequest)
    #: A diagnostic's ``key`` from the run (``SB2001``, ``SB3002:slot-2``).
    diagnostic_key: str = Field(max_length=100)
    fix_id: str = Field(max_length=100)
    #: Where the acceptance would be stored: one of the diagnostic's scopes. Omitted
    #: means the narrowest, this print (or this configuration, when it has no render
    #: yet), which is simple mode's default in #284. It is part of the fingerprint.
    scope: ScopeRef | None = None


class FixPreview(BaseModel):
    diagnostic_id: str
    diagnostic_key: str
    fix: Fix
    #: The scope this preview was made for; the apply must name the same one.
    scope: ScopeRef
    #: Binds the diff, the scope, the subject and the base; the apply carries it back.
    fingerprint: str
    #: Some change would reach Bambuddy once a send consumes accepted diffs.
    outward: bool
    #: False while any change waits on an AI spec §3.2 item; ``blockers`` names them.
    applicable: bool
    blockers: list[str]
    #: A human-readable account of the diff, for the confirmation card.
    summary: str
    route_note: str = ROUTE_NOTE


class FixApply(FixRequest):
    fingerprint: str = Field(min_length=64, max_length=64)
    #: The caller confirms the previewed diff. Strict: only JSON ``true`` confirms.
    confirm: StrictBool = False
    reason: str | None = Field(default=None, max_length=500)


class DecisionCreate(BaseModel):
    """Ignore or suppress a diagnostic at a scope. Accepting goes through a fix's
    apply, which is where its diff is confirmed."""

    model_config = ConfigDict(extra="forbid")

    diagnostic_id: str = Field(pattern=DIAGNOSTIC_ID_PATTERN)
    #: One instance (a diagnostic ``key``: the rule id, or the rule id, a colon and what
    #: tells its findings apart), or ``null`` for every instance of the rule.
    instance: str | None = Field(default=None, min_length=1, max_length=100)
    kind: Literal["ignore", "suppress"]
    scope: ScopeRef
    reason: str | None = Field(default=None, max_length=500)
    enforced: StrictBool = False
    #: Needed to enforce a suppression of an ``error`` rule over every narrower scope.
    confirm: StrictBool = False

    @model_validator(mode="after")
    def _well_formed(self) -> DecisionCreate:
        if self.kind == "suppress" and not (self.reason and self.reason.strip()):
            raise ValueError("a suppression needs a reason, as #pragma warning disable does")
        if self.instance is not None and not (
            self.instance == self.diagnostic_id
            or self.instance.startswith(f"{self.diagnostic_id}:")
        ):
            raise ValueError(
                f"an instance of {self.diagnostic_id} is {self.diagnostic_id!r} or starts "
                f"with {self.diagnostic_id + ':'!r}"
            )
        return self


def _require_scope(scope: ScopeRef) -> None:
    if not valid_scope(scope):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{scope.key!r} is not a {scope.kind} scope key",
            type_=SCOPE_PROBLEM,
        )


def _unavailable(error: Exception) -> ApiError:
    logger.warning("the analyzer decision store is unavailable", exc_info=error)
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        f"the analyzer decision store cannot be reached ({type(error).__name__})",
        type_=DATABASE_UNAVAILABLE_PROBLEM,
    )


def _stored[T](call: Callable[[], T]) -> T:
    """Run a store call, answering 503 rather than 500 when the database is down."""
    try:
        return call()
    except DATABASE_ERRORS as error:
        raise _unavailable(error) from None


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
    uploads: BambuddyUploadStore,
) -> AnalysisContext:
    slug, params, meta = await asyncio.to_thread(_subject, target, outputs, catalogue)
    library_file_id: int | None = None
    if meta is not None:
        # Any copy will do: every one is this output's 3MF, and the filament read only
        # needs the plate's slots. Without a database the analyzers still run, with the
        # inventory reported unavailable (#461).
        try:
            copies = await uploads.for_output(meta.id)
        except DatabaseRequiredError:
            copies = []
        library_file_id = copies[-1].id if copies else None
    return await gather_context(
        outputs=outputs,
        settings=store.load(),
        slug=slug,
        params=params,
        request=request,
        meta=meta,
        library_file_id=library_file_id,
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


def _fix_scope(
    context: AnalysisContext, diagnostic: AnalyzerDiagnostic, asked: ScopeRef | None
) -> ScopeRef:
    """The scope a fix is previewed and applied at: one this finding falls in."""
    if asked is not None:
        _require_scope(asked)
    scopes = context.scopes_for(diagnostic.slots)
    scope = asked or scopes[-1]
    if scope not in scopes:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{diagnostic.key} on this print is not in the {scope.kind} scope {scope.key!r}",
            type_=SCOPE_PROBLEM,
        )
    return scope


def _fingerprint(
    context: AnalysisContext, diagnostic: AnalyzerDiagnostic, fix: Fix, scope: ScopeRef
) -> str:
    return fingerprint(
        diagnostic.key,
        fix,
        scope=scope,
        subject=context.subject,
        base=context.base.identity(),
    )


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


def _record(store: DecisionStore, events: EventBus, decision: Decision) -> None:
    """Store ``decision``; the one it replaced is announced removed, then it recorded."""
    replaced = _stored(lambda: store.put(decision))
    if replaced is not None and replaced.id != decision.id:
        _publish(events, replaced, "removed")
    _publish(events, decision, "recorded")


@router.get("", response_model=list[AnalyzerInfo], summary="Every analyzer, with its sources")
def list_analyzers() -> list[AnalyzerInfo]:
    """The bundled analyzers: id, severity, category, the inputs each needs, the fixes
    it can offer, and the sources it cites (URL and quoted line)."""
    return describe()


@router.post("/run", response_model=AnalysisReport, summary="Run the analyzers")
async def post_run(
    body: AnalysisRun,
    outputs: OutputsDep,
    uploads: UploadsDep,
    catalogue: CatalogueDep,
    store: SettingsStoreDep,
    decisions: OptionalDecisionsDep,
) -> AnalysisReport:
    """Judge an output or a configuration against the print request it would go out
    with (the spool-first base, #335: printer, filament plan, nozzles, quality, plate).

    Reads only: nothing is uploaded, sliced or queued. An input that cannot be read
    (no Bambuddy, no choices yet, an output not uploaded yet, a missing API-key scope)
    is listed in ``inputs`` with the reason, and the analyzers needing it in
    ``skipped``.

    ``detail=simple`` returns the headline and the open findings with their sources and
    fixes; ``advanced`` adds evidence, locations, explanations, and the suppressed,
    ignored and ``hidden`` findings with the decision behind each.

    Without a database, or with one that cannot be reached, the analyzers still run;
    ``decisions_available`` is false and ``decisions_reason`` says why.
    """
    context = await _context(body.target, body.request, outputs, catalogue, store, uploads)
    if decisions is None:
        return build_report(context, [], detail=body.detail, decisions_unavailable=NO_DATABASE)
    try:
        stored = await asyncio.to_thread(decisions.list, scopes=context.scopes())
    except DATABASE_ERRORS as error:
        logger.warning("could not read analyzer decisions", exc_info=error)
        return build_report(
            context,
            [],
            detail=body.detail,
            decisions_unavailable=(
                f"the decision store cannot be reached ({type(error).__name__}), so no "
                "decisions were applied"
            ),
        )
    return build_report(context, stored, detail=body.detail)


@router.post("/fixes/preview", response_model=FixPreview, summary="Preview a fix")
async def post_preview(
    body: FixRequest,
    outputs: OutputsDep,
    uploads: UploadsDep,
    catalogue: CatalogueDep,
    store: SettingsStoreDep,
) -> FixPreview:
    """The fix's whole diff, where each line would land, whether it can be applied yet,
    and the fingerprint an apply confirms against (diff, scope, subject and base).
    Changes nothing."""
    context = await _context(body.target, body.request, outputs, catalogue, store, uploads)
    diagnostic, fix = _find_fix(context, body)
    scope = _fix_scope(context, diagnostic, body.scope)
    blockers = fix.blockers
    return FixPreview(
        diagnostic_id=diagnostic.id,
        diagnostic_key=diagnostic.key,
        fix=fix,
        scope=scope,
        fingerprint=_fingerprint(context, diagnostic, fix, scope),
        outward=fix.outward,
        applicable=not blockers,
        blockers=blockers,
        summary=_describe_change(fix),
    )


@router.post("/fixes/apply", response_model=Decision, summary="Apply a previewed fix")
async def post_apply(
    body: FixApply,
    outputs: OutputsDep,
    uploads: UploadsDep,
    catalogue: CatalogueDep,
    store: SettingsStoreDep,
    decisions: DecisionsDep,
    events: EventsDep,
) -> Decision:
    """Record the fix as accepted at ``scope``: its diff joins the effective diff
    (``accepted_changes``) of every later run in that scope while the diff is unchanged.

    Nothing is sent to Bambuddy: this is a ``write`` to ScadBuddy's own database,
    removable with ``DELETE /analyzers/decisions/{id}``. See the module docstring for
    what a send that consumes it must do.

    Refused, in this order: a scope this finding does not fall in (422); a fingerprint
    that differs from the one this apply computes, because the diff, the scope, the
    print or its base moved since the preview (409, ``analyzer-fix-stale``); a change
    whose target is still unverified (409, ``analyzer-fix-unverified``, naming the §3.2
    items in ``to_verify``); no ``confirm: true`` (428, ``confirmation-required``).
    """
    context = await _context(body.target, body.request, outputs, catalogue, store, uploads)
    diagnostic, fix = _find_fix(context, body)
    scope = _fix_scope(context, diagnostic, body.scope)
    if _fingerprint(context, diagnostic, fix, scope) != body.fingerprint:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "the diff, its scope, the print or its base differ from the preview; preview it again",
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
    if not body.confirm:
        raise ApiError(
            428,
            "confirm the previewed diff to record it",
            title="Precondition Required",
            type_=CONFIRMATION_PROBLEM,
            summary=_describe_change(fix),
        )
    decision = Decision(
        id=new_decision_id(),
        diagnostic_id=diagnostic.id,
        instance=diagnostic.key,
        kind="accept",
        scope=scope,
        reason=body.reason,
        fix_id=fix.id,
        fingerprint=body.fingerprint,
        diff_digest=diff_digest(fix),
        changes=fix.changes,
    )
    await asyncio.to_thread(_record, decisions, events, decision)
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
        for row in _stored(lambda: decisions.list(diagnostic_id=diagnostic_id))
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
    """Replaces an earlier decision about the same rule and instance at the same scope
    (announced as ``removed``, then this one as ``recorded``).

    ScadBuddy's own state, reversible by deleting it (AI spec §8.1's ``write`` tier).
    ``enforced`` makes a broad decision win over narrower ones; enforcing the
    suppression of an ``error`` rule also needs ``confirm: true``, since it hides that
    problem from every print in the scope.
    """
    _require_scope(body.scope)
    rule = next((row for row in builtin.BUILTIN if row.id == body.diagnostic_id), None)
    if rule is None:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"there is no analyzer {body.diagnostic_id}",
            type_=UNKNOWN_RULE_PROBLEM,
        )
    if body.kind == "suppress" and body.enforced and rule.severity == "error" and not body.confirm:
        raise ApiError(
            428,
            f"enforcing a suppression of {rule.id} ({rule.title}, an error) hides it from "
            "every print in the scope; confirm it",
            title="Precondition Required",
            type_=CONFIRMATION_PROBLEM,
        )
    decision = Decision(
        id=new_decision_id(),
        diagnostic_id=body.diagnostic_id,
        instance=body.instance,
        kind=body.kind,
        scope=body.scope,
        reason=body.reason.strip() if body.reason else None,
        enforced=body.enforced,
    )
    _record(decisions, events, decision)
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
    gone = _stored(lambda: decisions.remove(decision_id))
    if gone is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no decision with id {decision_id!r}")
    _publish(events, gone, "removed")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
