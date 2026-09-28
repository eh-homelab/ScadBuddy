"""What an analyzer sees: the #84 print request and everything read to judge it.

The base is the request the print dialog already submits (spool-first print spec
2026-09-27 §2): the printer, the per-slot spool plan, the nozzles, quality and plate
type the slicer presets are derived from, the plate index and the options overlay.
Every input is read with the calls ScadBuddy already makes; one that cannot be read is
recorded in :attr:`AnalysisContext.unavailable` with the reason, and the analyzers
that need it are skipped, saying why, rather than guessing.
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
from scadbuddy.bambuddy.models import Printer
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.resolver import TIERS, PrintChoices, printer_preset_name
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.render.geometry import GeometryAnalysis
from scadbuddy.render.plate import PlateGeometry
from scadbuddy.render.schema import ParamValue

#: The inputs an analyzer can declare it needs.
InputName = Literal["output", "geometry", "plate", "choices", "printer", "filaments", "inventory"]
INPUT_NAMES: tuple[InputName, ...] = (
    "output",
    "geometry",
    "plate",
    "choices",
    "printer",
    "filaments",
    "inventory",
)


class AnalysisRequest(BaseModel):
    """The base print request, as ``POST /print/outputs/{id}/run`` takes it (#84, #335).

    Every field means what it means on :class:`~scadbuddy.bambuddy.pipelines.PrintRunRequest`.
    ``printer_id`` omitted is the model's remembered printer, then the configured one,
    then the first active one, as the dialog's choices route picks it. ``choices``
    omitted is what the dialog reopens with: the model's remembered nozzles, tier and
    process, on the plate type remembered for that printer.
    """

    printer_id: int | None = None
    filament_plan: FilamentPlan | None = None
    choices: PrintChoices | None = None
    plate_id: int = Field(default=1, ge=1)
    copies: int | None = Field(default=None, ge=1, le=1000)
    options: PrintOptions = Field(default_factory=PrintOptions)


class FilamentSlot(BaseModel):
    """One plate slot's filament: the spool the plan chose for it."""

    slot_id: int
    spool_id: int
    material: str | None = None
    subtype: str | None = None
    brand: str | None = None
    #: The slicer filament the spool names in Bambuddy's inventory.
    preset_name: str | None = None


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
    #: The nozzles, quality and plate type the slicer presets are derived from.
    choices: PrintChoices | None = None
    #: ``request`` when the caller sent them, ``remembered`` when they are the model's.
    choices_origin: Literal["request", "remembered"] = "request"
    printer: Printer | None = None
    filaments: list[FilamentSlot] = field(default_factory=list)
    #: The filament step's own payload (inventory joined to assignments), when the
    #: output already has a Bambuddy library file to read the plate's slots from.
    filament_options: FilamentOptions | None = None
    unavailable: dict[InputName, str] = field(default_factory=dict)

    def has(self, name: InputName) -> bool:
        return name not in self.unavailable and getattr(self, _ATTRIBUTE[name]) not in (None, [])

    @property
    def bed_type(self) -> str | None:
        """The plate type this print slices for."""
        return self.choices.bed_type if self.choices else None

    @property
    def nozzle_size(self) -> str | None:
        """The one nozzle size the job slices for (spool-first spec §4.1: both sides)."""
        return self.choices.nozzles[0].size if self.choices else None

    @property
    def printer_preset_name(self) -> str | None:
        """Bambu's printer preset the resolver names for the chosen nozzle (§4.1)."""
        return printer_preset_name(self.choices.nozzles) if self.choices else None

    @property
    def process_name(self) -> str | None:
        """The Advanced process, else the quality tier's for the size (§4.2)."""
        if self.choices is None:
            return None
        size = self.choices.nozzles[0].size
        return self.choices.process_name or TIERS[size][self.choices.tier or "standard"]

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
    "choices": "choices",
    "printer": "printer",
    "filaments": "filaments",
    "inventory": "filament_options",
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
