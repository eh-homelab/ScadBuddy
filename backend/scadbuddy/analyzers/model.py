"""The analyzer vocabulary: sources, diagnostics, fixes, scopes and decisions (#284).

Named after Roslyn's: an *analyzer* is a rule with an id (``SB1001``); a
*diagnostic* is one finding of it, with evidence and a location; a *fix* is a
concrete, previewable change a diagnostic offers. Every analyzer, every diagnostic
and every proposed value carries the :class:`Source` it rests on (AI spec D10).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from scadbuddy.render.geometry import MeshEdge
from scadbuddy.render.glb import BoundingBox

if TYPE_CHECKING:
    from scadbuddy.analyzers.context import AnalysisContext, InputName

#: ``error`` blocks the print in the UI, ``hidden`` is reported only in advanced detail.
Severity = Literal["error", "warning", "info", "hidden"]
Category = Literal["geometry", "material", "profile", "plate", "ams", "history", "analyzer"]

#: Where a decision can be stored, broadest first (#284 "Scopes"). The order is the
#: resolution order: a narrower scope overrides a broader one unless the broader
#: decision is ``enforced``. Material and printer are orthogonal to the template
#: chain in #284; here they rank below it, so a template's decision wins over a
#: material's, which is the precedence #284 states.
ScopeKind = Literal[
    "global", "material", "printer", "template", "template_version", "configuration", "print"
]
SCOPE_ORDER: tuple[ScopeKind, ...] = (
    "global",
    "material",
    "printer",
    "template",
    "template_version",
    "configuration",
    "print",
)

SettingValue = str | int | float | bool | None

#: Where an accepted change lands (AI spec §11): filament-level settings in the queue
#: item's ``filament_overrides``, process-level settings in a derived local preset,
#: queue-level options in the #88 ``PrintOptions`` overlay, and the print request's
#: own fields (``bed_type``). ``project_settings_3mf`` is listed so a fixer can name it,
#: and is never verified until §3.2 is.
FixTarget = Literal[
    "print_options",
    "print_request",
    "filament_overrides",
    "derived_process_preset",
    "project_settings_3mf",
]

#: AI spec §3.2 items a target still waits on. A target listed here cannot be applied.
UNVERIFIED_TARGETS: dict[FixTarget, str] = {
    "filament_overrides": (
        "Which keys filament_overrides accepts on PrintQueueItemCreate (can it carry "
        "nozzle temperature and fan?) — AI spec §3.2, verified by #284"
    ),
    "derived_process_preset": (
        "Whether Bambuddy's /local-presets/ can create a process preset that inherits "
        "from a base preset plus a diff — AI spec §3.2, verified by #284"
    ),
    "project_settings_3mf": (
        "Whether a 3MF that claims Application: BambuStudio-… has its "
        "project_settings.config override the resolved process preset — AI spec §3.2, "
        "verified by #284"
    ),
}

#: Targets whose change reaches Bambuddy when the print is sent. Accepting one is an
#: outward action (AI spec §8.1), so it needs an explicit confirmation.
OUTWARD_TARGETS: frozenset[FixTarget] = frozenset(
    {
        "print_options",
        "print_request",
        "filament_overrides",
        "derived_process_preset",
        "project_settings_3mf",
    }
)


class Source(BaseModel):
    """A citation: where a rule or a value comes from, and the line that says so."""

    url: str
    title: str
    #: Copied verbatim from the page, so a reader can find it with the browser's search.
    quote: str
    accessed: date
    #: The settings or claims this source supports (a setting key, or a rule id).
    supports: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    """One measured or read fact a diagnostic rests on."""

    label: str
    value: SettingValue
    unit: str | None = None
    #: Where the figure came from: ``geometry``, ``bambuddy:<route>``, ``request`` …
    origin: str


class DiagnosticLocation(BaseModel):
    """Where the problem is, as precisely as the input allows."""

    kind: Literal["mesh", "plate", "filament_slot", "print_choices", "profile_setting", "analyzer"]
    #: 1-based extruder index of the part, as in ``OutputMeta.parts``.
    part: int | None = None
    colour: str | None = None
    #: In the model's own frame (mm, Z up), as the geometry analysis reports it.
    bbox: BoundingBox | None = None
    edges: list[MeshEdge] = Field(default_factory=list)
    edges_truncated: bool = False
    slot_id: int | None = None
    setting: str | None = None


class SettingChange(BaseModel):
    """One line of a fix's diff: a setting, its base value and the proposed one."""

    target: FixTarget
    setting: str
    #: ``None`` with ``base_known=False`` when the base cannot be read (Bambuddy exposes
    #: no preset contents); ``base_note`` then says what is known about it.
    base: SettingValue = None
    base_known: bool = False
    base_note: str | None = None
    proposed: SettingValue
    unit: str | None = None
    #: The plate slot a filament-level change applies to.
    slot_id: int | None = None
    sources: list[Source]
    #: False while the target is on the AI spec's §3.2 "to verify" list.
    verified: bool
    to_verify: str | None = None
    #: Reaches Bambuddy when the print is sent (AI spec §8.1).
    outward: bool


class Fix(BaseModel):
    id: str
    title: str
    description: str
    changes: list[SettingChange]

    @property
    def blockers(self) -> list[str]:
        """The §3.2 items that stop this fix being applied, deduplicated in order."""
        found: list[str] = []
        for change in self.changes:
            if not change.verified and change.to_verify and change.to_verify not in found:
                found.append(change.to_verify)
        return found

    @property
    def outward(self) -> bool:
        return any(change.outward for change in self.changes)


def change(
    target: FixTarget,
    setting: str,
    proposed: SettingValue,
    *,
    sources: Sequence[Source],
    base: SettingValue = None,
    base_known: bool = False,
    base_note: str | None = None,
    unit: str | None = None,
    slot_id: int | None = None,
) -> SettingChange:
    """A change whose verification and outwardness follow from its target."""
    to_verify = UNVERIFIED_TARGETS.get(target)
    return SettingChange(
        target=target,
        setting=setting,
        base=base,
        base_known=base_known,
        base_note=base_note,
        proposed=proposed,
        unit=unit,
        slot_id=slot_id,
        sources=list(sources),
        verified=to_verify is None,
        to_verify=to_verify,
        outward=target in OUTWARD_TARGETS,
    )


class ScopeRef(BaseModel):
    """A scope and the key that names one instance of it.

    Keys: ``""`` for global; ``pla`` or ``pla/silk`` for a material; ``id:<printer id>``
    or ``model:<printer model>`` for a printer; the slug for a template;
    ``<slug>@<model version>`` for a template version; ``<slug>#<params hash>`` for a
    configuration; the output id for a print.
    """

    kind: ScopeKind
    key: str = Field(default="", max_length=200)

    @property
    def rank(self) -> int:
        return SCOPE_ORDER.index(self.kind)


DecisionKind = Literal["accept", "ignore", "suppress"]


class Decision(BaseModel):
    """What a person decided about a diagnostic, at one scope."""

    id: str
    #: The rule id (``SB2001``).
    diagnostic_id: str
    #: One instance (a diagnostic's ``key``), or ``None`` for every instance of the rule.
    instance: str | None = None
    kind: DecisionKind
    scope: ScopeRef
    reason: str | None = None
    #: Wins over narrower scopes, like Roslyn's global config forcing a severity.
    enforced: bool = False
    #: ``accept`` only: the fix accepted, its fingerprint at the time, and its diff.
    fix_id: str | None = None
    fingerprint: str | None = None
    changes: list[SettingChange] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


DiagnosticStatus = Literal["open", "accepted", "ignored", "suppressed"]


class AppliedDecision(BaseModel):
    """The decision that decided a diagnostic's status, and whether it still holds."""

    decision: Decision
    #: An accepted fix whose diff has since changed (the base moved, or the rule did):
    #: the diagnostic is open again rather than carrying a stale diff over (#284).
    stale: bool = False


class AnalyzerDiagnostic(BaseModel):
    id: str
    #: Unique within one run: the rule id, plus what tells two findings of it apart.
    key: str
    title: str
    severity: Severity
    category: Category
    message: str
    #: The longer explanation; advanced detail only.
    why: str | None = None
    location: DiagnosticLocation | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    sources: list[Source]
    fixes: list[Fix] = Field(default_factory=list)
    status: DiagnosticStatus = "open"
    decision: AppliedDecision | None = None


def fingerprint(diagnostic_key: str, fix: Fix) -> str:
    """What a preview is confirmed against: the diagnostic, the fix and its whole diff.

    A confirmation carries it back, so an apply can only land the diff that was shown.
    """
    payload = json.dumps(
        {
            "diagnostic": diagnostic_key,
            "fix": fix.id,
            "changes": [
                {
                    "target": item.target,
                    "setting": item.setting,
                    "base": item.base,
                    "proposed": item.proposed,
                    "slot_id": item.slot_id,
                }
                for item in fix.changes
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


Check = Callable[["AnalysisContext", "Analyzer"], list[AnalyzerDiagnostic]]


@dataclass(frozen=True)
class Analyzer:
    """One rule. ``check`` returns its diagnostics for a context that has every input
    in ``needs``; the runner skips it, saying why, when one is missing."""

    id: str
    name: str
    title: str
    severity: Severity
    category: Category
    description: str
    sources: tuple[Source, ...]
    check: Check
    needs: frozenset[InputName] = field(default_factory=frozenset)
    #: Where the rule is defined. Every bundled analyzer is global.
    scope: ScopeKind = "global"
    #: The fixes this analyzer can offer, by id, for the catalogue.
    fix_ids: tuple[str, ...] = ()

    def diagnose(
        self,
        *,
        message: str,
        key: str | None = None,
        severity: Severity | None = None,
        why: str | None = None,
        location: DiagnosticLocation | None = None,
        evidence: Sequence[Evidence] = (),
        sources: Sequence[Source] | None = None,
        fixes: Sequence[Fix] = (),
    ) -> AnalyzerDiagnostic:
        return AnalyzerDiagnostic(
            id=self.id,
            key=f"{self.id}:{key}" if key else self.id,
            title=self.title,
            severity=severity or self.severity,
            category=self.category,
            message=message,
            why=why,
            location=location,
            evidence=list(evidence),
            sources=list(sources if sources is not None else self.sources),
            fixes=list(fixes),
        )
