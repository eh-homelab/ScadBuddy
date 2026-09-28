"""What an analyzer sees: the print dialog's request and everything read to judge it.

The base is the request ``POST /print/outputs/{id}/run`` takes since the spool-first
flow (#335, spec 2026-09-27 §2/§4): the printer, one spool per plate slot, and the
choices (nozzles, quality tier or process, plate type, per-slot preset overrides) the
resolver derives every Bambu preset from. There is no pipeline any more. Every input
is read with calls ScadBuddy already makes; one that cannot be read is recorded in
:attr:`AnalysisContext.unavailable` with the reason, and the analyzers that need it
are skipped, saying why, rather than guessing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

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
InputName = Literal["output", "geometry", "plate", "printer", "choices", "filaments", "inventory"]
INPUT_NAMES: tuple[InputName, ...] = (
    "output",
    "geometry",
    "plate",
    "printer",
    "choices",
    "filaments",
    "inventory",
)


class AnalysisRequest(BaseModel):
    """The base print request, as ``POST /print/outputs/{id}/run`` takes it (#335).

    Every field means what it means on
    :class:`~scadbuddy.bambuddy.pipelines.PrintRunRequest`, but the plan and the
    choices are optional here: an analysis can run before the dialog has them.
    ``printer_id`` omitted is the model's remembered printer, then the configured one,
    then the first active one, as the dialog's choices route picks it; ``choices``
    omitted is what the dialog reopens with for this model (its remembered nozzles,
    tier and process, on the plate remembered for that printer). With neither, the
    analyzers that need choices say so.
    """

    printer_id: int | None = None
    filament_plan: FilamentPlan | None = None
    choices: PrintChoices | None = None
    plate_id: int = Field(default=1, ge=1)
    copies: int | None = Field(default=None, ge=1, le=1000)
    options: PrintOptions = Field(default_factory=PrintOptions)


class FilamentSlot(BaseModel):
    """One plate slot's spool, as the plan chose it and the inventory describes it."""

    slot_id: int
    spool_id: int
    material: str | None = None
    subtype: str | None = None
    brand: str | None = None
    #: The slicer preset the spool names (``slicer_filament_name``).
    preset_name: str | None = None


class BaseSlot(BaseModel):
    slot_id: int
    spool_id: int | None = None
    #: What the resolver slices this slot with, as far as it can be named without the
    #: catalogue: an Advanced override's ``source:id``, else the spool's own preset.
    preset: str | None = None


class BaseProfile(BaseModel):
    """What the diffs are against: the presets the resolver derives from the choices.

    Names follow the resolver's own derivation (``resolver.TIERS`` and
    ``printer_preset_name``), so they are the presets a run would slice with.
    """

    printer_id: int | None = None
    printer_model: str | None = None
    #: ``request`` when the caller sent the choices, ``remembered`` when they are
    #: what the dialog reopens with for this model, ``None`` when there are none.
    choices_origin: Literal["request", "remembered"] | None = None
    nozzle_sizes: list[str] = Field(default_factory=list)
    high_flow: bool = False
    printer_preset_name: str | None = None
    process_preset_name: str | None = None
    bed_type: str | None = None
    plate_id: int = 1
    plate_model: str | None = None
    slots: list[BaseSlot] = Field(default_factory=list)
    copies: int = 1

    def identity(self) -> dict[str, Any]:
        """What a diff is judged against: moving any of it makes a preview stale."""
        return {
            "printer": self.printer_id,
            "printer_preset": self.printer_preset_name,
            "process_preset": self.process_preset_name,
            "bed_type": self.bed_type,
            "plate": self.plate_id,
            "slots": [(slot.slot_id, slot.spool_id, slot.preset) for slot in self.slots],
        }


def base_profile(
    request: AnalysisRequest,
    choices: PrintChoices | None,
    printer: Printer | None,
    filaments: list[FilamentSlot],
    plate: PlateGeometry | None,
) -> BaseProfile:
    sizes: list[str] = [str(nozzle.size) for nozzle in choices.nozzles] if choices else []
    process = None
    if choices is not None:
        process = choices.process_name or TIERS.get(sizes[0], {}).get(choices.tier or "standard")
    overrides = choices.filament_overrides if choices else {}
    slots = []
    for slot in filaments:
        ref = overrides.get(slot.slot_id)
        slots.append(
            BaseSlot(
                slot_id=slot.slot_id,
                spool_id=slot.spool_id,
                preset=f"{ref.source}:{ref.id}" if ref is not None else slot.preset_name,
            )
        )
    return BaseProfile(
        printer_id=printer.id if printer else request.printer_id,
        printer_model=printer.model if printer else None,
        nozzle_sizes=sizes,
        high_flow=any(nozzle.flow == "high_flow" for nozzle in choices.nozzles)
        if choices
        else False,
        printer_preset_name=printer_preset_name(choices.nozzles) if choices else None,
        process_preset_name=process,
        bed_type=choices.bed_type if choices else None,
        plate_id=request.plate_id,
        plate_model=plate.model if plate else None,
        slots=slots,
        copies=request.copies or request.options.quantity or 1,
    )


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
    printer: Printer | None = None
    filaments: list[FilamentSlot] = field(default_factory=list)
    #: The filament step's own payload (inventory joined to assignments), when the
    #: output already has a Bambuddy library file to read the plate's slots from.
    filament_options: FilamentOptions | None = None
    #: The nozzles, quality and plate the presets are derived from: the request's, else
    #: the ones the dialog reopens with for this model (``choices_origin`` says which).
    choices: PrintChoices | None = None
    choices_origin: Literal["request", "remembered"] = "request"
    unavailable: dict[InputName, str] = field(default_factory=dict)
    base: BaseProfile = field(default_factory=BaseProfile)

    def has(self, name: InputName) -> bool:
        return name not in self.unavailable and getattr(self, _ATTRIBUTE[name]) not in (None, [])

    @property
    def bed_type(self) -> str | None:
        return self.choices.bed_type if self.choices else None

    @property
    def copies(self) -> int:
        return self.base.copies

    @property
    def subject(self) -> str:
        """What a decision or a preview is about: the output, else the configuration."""
        return self.output.id if self.output else configuration_key(self.slug, self.params)

    def scopes(self) -> list[ScopeRef]:
        """Every scope a decision about this print can be stored at, broadest first.

        A material's own key ranks below its material/subtype key, so ``pla/silk``
        beats ``pla``.
        """
        found: list[ScopeRef] = [ScopeRef(kind="global")]
        bare = [key for slot in self.filaments for key in material_keys(slot)[:1]]
        typed = [key for slot in self.filaments for key in material_keys(slot)[1:]]
        for key in dict.fromkeys([*bare, *typed]):
            found.append(ScopeRef(kind="material", key=key))
        model = self.printer.model if self.printer and self.printer.model else None
        if model:
            found.append(ScopeRef(kind="printer", key=f"model:{scope_token(model)}"))
        if self.printer is not None:
            found.append(ScopeRef(kind="printer", key=f"id:{self.printer.id}"))
        found.append(ScopeRef(kind="template", key=self.slug))
        if self.output is not None and self.output.model_version:
            found.append(
                ScopeRef(kind="template_version", key=f"{self.slug}@{self.output.model_version}")
            )
        found.append(ScopeRef(kind="configuration", key=configuration_key(self.slug, self.params)))
        if self.output is not None:
            found.append(ScopeRef(kind="print", key=self.output.id))
        return found

    def scopes_for(self, slots: list[int]) -> list[ScopeRef]:
        """:meth:`scopes` for a finding about ``slots`` (every slot when empty): a
        material scope applies only when one of those slots is that material."""
        concerned = [slot for slot in self.filaments if not slots or slot.slot_id in slots]
        allowed = {key for slot in concerned for key in material_keys(slot)}
        return [
            scope for scope in self.scopes() if scope.kind != "material" or scope.key in allowed
        ]


_ATTRIBUTE: dict[InputName, str] = {
    "output": "output",
    "geometry": "geometry",
    "plate": "plate",
    "printer": "printer",
    "choices": "choices",
    "filaments": "filaments",
    "inventory": "filament_options",
}


def configuration_key(slug: str, params: dict[str, ParamValue]) -> str:
    """``<slug>#<hash>``: one parameter set of one template, whatever order it came in."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return f"{slug}#{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


_UNSAFE = re.compile(r"[^a-z0-9 +._-]+")


def scope_token(raw: str) -> str:
    """``raw`` spelled so a scope key accepts it: lower case, anything else a ``-``."""
    token = _UNSAFE.sub("-", raw.strip().lower()).strip(" +._-")
    return token or "unknown"


def material_keys(slot: FilamentSlot) -> list[str]:
    """``pla`` then ``pla/silk`` for a slot, in the form the material scope takes."""
    if not slot.material:
        return []
    material = scope_token(slot.material)
    subtype = scope_token(slot.subtype) if slot.subtype and slot.subtype.strip() else None
    return [material, f"{material}/{subtype}"] if subtype else [material]
