"""What an analyzer sees: the #84 print request and everything read to judge it.

The base is the request the print dialog already submits (print-flow spec §1): the
pipeline and its presets, the printer, the per-slot filament plan, the plate and the
options overlay. Every input is read with the calls ScadBuddy already makes; one that
cannot be read is recorded in :attr:`AnalysisContext.unavailable` with the reason, and
the analyzers that need it are skipped, saying why, rather than guessing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.analyzers.model import ScopeRef
from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan
from scadbuddy.bambuddy.models import EligibilityReport, Printer
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.pipelines import PipelineView
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.render.geometry import GeometryAnalysis
from scadbuddy.render.plate import PlateGeometry
from scadbuddy.render.schema import ParamValue

#: The inputs an analyzer can declare it needs.
InputName = Literal[
    "output", "geometry", "plate", "pipeline", "printer", "filaments", "inventory", "eligibility"
]
INPUT_NAMES: tuple[InputName, ...] = (
    "output",
    "geometry",
    "plate",
    "pipeline",
    "printer",
    "filaments",
    "inventory",
    "eligibility",
)


class AnalysisRequest(BaseModel):
    """The base print request, as ``POST /print/outputs/{id}/run`` takes it (#84).

    Every field means what it means on :class:`~scadbuddy.bambuddy.pipelines.PrintRunRequest`;
    ``pipeline_id`` omitted is the model's default, then the global one.
    """

    pipeline_id: int | None = None
    printer_id: int | None = None
    filament_plan: FilamentPlan | None = None
    plate_id: int = Field(default=1, ge=1)
    bed_type: str | None = Field(default=None, max_length=64)
    copies: int | None = Field(default=None, ge=1, le=1000)
    options: PrintOptions = Field(default_factory=PrintOptions)


class FilamentSlot(BaseModel):
    """One plate slot's filament, as far as the plan and the pipeline name it."""

    slot_id: int
    spool_id: int | None = None
    material: str | None = None
    subtype: str | None = None
    brand: str | None = None
    #: The slicer preset the spool names, or the pipeline's own preset for this slot.
    preset_name: str | None = None
    #: ``spool`` when the plan chose it, ``pipeline`` when only the pipeline names it.
    origin: Literal["spool", "pipeline"]


@dataclass
class AnalysisContext:
    slug: str
    params: dict[str, ParamValue]
    request: AnalysisRequest
    output: OutputMeta | None = None
    #: The output's ``model.3mf`` on disk, for checks that read the file itself.
    model_3mf: Path | None = None
    geometry: GeometryAnalysis | None = None
    plate: PlateGeometry | None = None
    pipeline: PipelineView | None = None
    printer: Printer | None = None
    filaments: list[FilamentSlot] = field(default_factory=list)
    #: The filament step's own payload (inventory joined to assignments), when the
    #: output already has a Bambuddy library file to read the plate's slots from.
    filament_options: FilamentOptions | None = None
    eligibility: EligibilityReport | None = None
    unavailable: dict[InputName, str] = field(default_factory=dict)

    def has(self, name: InputName) -> bool:
        return name not in self.unavailable and getattr(self, _ATTRIBUTE[name]) not in (None, [])

    @property
    def bed_type(self) -> str | None:
        """The plate type this print slices for: the request's, else the pipeline's."""
        if self.request.bed_type:
            return self.request.bed_type
        return self.pipeline.bed_type if self.pipeline else None

    @property
    def copies(self) -> int:
        return self.request.copies or self.request.options.quantity or 1

    def scopes(self) -> list[ScopeRef]:
        """Every scope a decision about this print can be stored at, broadest first."""
        found: list[ScopeRef] = [ScopeRef(kind="global")]
        for key in material_keys(self.filaments):
            found.append(ScopeRef(kind="material", key=key))
        printer_id = self.printer.id if self.printer else None
        model = self.printer.model if self.printer and self.printer.model else None
        if model is None and self.pipeline is not None:
            model = self.pipeline.target_model_class
        if model:
            found.append(ScopeRef(kind="printer", key=f"model:{model}"))
        if printer_id is not None:
            found.append(ScopeRef(kind="printer", key=f"id:{printer_id}"))
        found.append(ScopeRef(kind="template", key=self.slug))
        if self.output is not None and self.output.model_version:
            found.append(
                ScopeRef(kind="template_version", key=f"{self.slug}@{self.output.model_version}")
            )
        found.append(ScopeRef(kind="configuration", key=configuration_key(self.slug, self.params)))
        if self.output is not None:
            found.append(ScopeRef(kind="print", key=self.output.id))
        return found


_ATTRIBUTE: dict[InputName, str] = {
    "output": "output",
    "geometry": "geometry",
    "plate": "plate",
    "pipeline": "pipeline",
    "printer": "printer",
    "filaments": "filaments",
    "inventory": "filament_options",
    "eligibility": "eligibility",
}


def configuration_key(slug: str, params: dict[str, ParamValue]) -> str:
    """``<slug>#<hash>``: one parameter set of one template, whatever order it came in."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return f"{slug}#{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def material_keys(filaments: list[FilamentSlot]) -> list[str]:
    """``pla`` and ``pla/silk`` for each distinct material on the plate, in slot order."""
    keys: list[str] = []
    for slot in filaments:
        if not slot.material:
            continue
        material = slot.material.strip().lower()
        for key in (
            material,
            f"{material}/{slot.subtype.strip().lower()}" if slot.subtype else None,
        ):
            if key and key not in keys:
                keys.append(key)
    return keys
