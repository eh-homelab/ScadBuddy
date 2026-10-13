"""Filament profiles (#717): named, cited process changes the print dialog suggests for
a spool, such as Silk PLA's slower outer wall.

A profile is offered, never applied silently: the dialog suggests one when a chosen
spool matches it, shows each change against the value it replaces, and sends the ids
the user left on (``PrintRunRequest.filament_profiles``).

Every change goes out through ``SliceRequest.process_overrides``, which Bambuddy writes
into the process JSON it slices with (``services/process_overrides.py``), after the
preset and before nothing else. A change is written in the shape the preset stores
the setting: on an H2C, ``outer_wall_speed`` is a list with one entry per extruder
variant (``0.20mm Standard @BBL H2C``: ``["200", "500", "500", "200", "500"]``), and a
bare string there would collapse it. So the base preset's values are read first
(``GET /slicer/preset-values``), and a profile whose values cannot be read is not
applied: the print is refused, naming why, rather than sliced without it or with a
guessed shape.

Filament settings (nozzle temperature) have no verified channel yet (#284's
``UNVERIFIED_TARGETS``), so a profile states them as notes and never sends them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from scadbuddy.analyzers import sources as src
from scadbuddy.analyzers.model import Source
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import PresetRef, ProcessValue


class ProfileMatch(BaseModel):
    """Which spools a profile is suggested for: the material, and a word the spool's
    subtype or its slicer preset's name contains. Both, because the inventory records
    Bambu's tri-colour silk as subtype ``Tri Color`` with the preset ``Bambu PLA Silk``
    (``inventory-spools.json``), as the silk analyzer (``SB2001``) reads it too."""

    material: str
    word: str

    def matches(
        self, *, material: str | None, subtype: str | None, preset_name: str | None
    ) -> bool:
        if (material or "").strip().upper() != self.material.upper():
            return False
        word = self.word.lower()
        return word in (subtype or "").lower() or word in (preset_name or "").lower()


class ProfileSetting(BaseModel):
    """One process setting a profile changes, and why."""

    key: str
    label: str
    value: str
    unit: str | None = None
    sources: list[Source] = Field(default_factory=list)


class ProfileNote(BaseModel):
    """Advice a profile gives but cannot apply."""

    text: str
    sources: list[Source] = Field(default_factory=list)


class FilamentProfile(BaseModel):
    id: str
    name: str
    description: str
    match: ProfileMatch
    settings: list[ProfileSetting]
    notes: list[ProfileNote] = Field(default_factory=list)
    builtin: bool = True


SILK = FilamentProfile(
    id="silk-pla",
    name="Silk PLA",
    description=(
        "Bambu's silk guide: a slower, consistent outer wall is what gives silk its "
        "gloss. It raises print time."
    ),
    match=ProfileMatch(material="PLA", word="silk"),
    settings=[
        ProfileSetting(
            key="outer_wall_speed",
            label="Outer wall speed",
            value="50",
            unit="mm/s",
            sources=[src.SILK_OUTER_WALL, src.OUTER_WALL_SPEED],
        )
    ],
    notes=[
        ProfileNote(
            text=(
                "Bambu also recommends a 235 °C nozzle for silk. ScadBuddy cannot set "
                "a filament's temperature yet, so it is not changed here: set it in the "
                "spool's filament preset."
            ),
            sources=[src.SILK_TEMPERATURE, src.SILK_TEMPERATURE_RANGE],
        )
    ],
)

#: The built-in profiles, by id.
BUILTIN: dict[str, FilamentProfile] = {profile.id: profile for profile in (SILK,)}


class ProfileChange(BaseModel):
    """One setting as this print's process preset has it, and as the profile sets it."""

    key: str
    label: str
    unit: str | None = None
    #: The preset's value, a list for a per-extruder-variant option; ``None`` unknown.
    current: ProcessValue | None = None
    value: str


class ProfilePreview(BaseModel):
    profile: FilamentProfile
    #: The process preset the changes were read against.
    process_preset: PresetRef | None = None
    #: Whether the profile can be applied to this print. When false, ``reason`` says
    #: why, and printing with it on is refused.
    applicable: bool
    reason: str | None = None
    changes: list[ProfileChange] = Field(default_factory=list)


_REASONS = {
    "sidecar_unavailable": "Bambuddy's slicer is not reachable",
    "not_configured": "Bambuddy has no slicer configured",
    "preset_unresolved": "Bambuddy could not find the process preset",
}


def unknown_profiles(ids: list[str]) -> list[str]:
    return [profile_id for profile_id in ids if profile_id not in BUILTIN]


async def preview(
    client: BambuddyClient, process: PresetRef | None, profile: FilamentProfile
) -> ProfilePreview:
    """``profile``'s changes against ``process``'s values, in their stored shape."""
    if process is None:
        return ProfilePreview(
            profile=profile,
            applicable=False,
            reason="No process preset resolves for these choices.",
        )
    values = await client.preset_values(process)
    if not values.resolved:
        why = _REASONS.get(values.reason or "", f"Bambuddy answered {values.reason!r}")
        return ProfilePreview(
            profile=profile,
            process_preset=process,
            applicable=False,
            reason=f"The process preset's values could not be read ({why}).",
        )
    changes: list[ProfileChange] = []
    for setting in profile.settings:
        current = values.values.get(setting.key)
        if isinstance(current, list):
            stored: ProcessValue | None = [str(item) for item in current]
        elif current is None:
            stored = None
        else:
            stored = str(current)
        if stored is None:
            return ProfilePreview(
                profile=profile,
                process_preset=process,
                applicable=False,
                reason=f"The process preset has no {setting.key}.",
            )
        changes.append(
            ProfileChange(
                key=setting.key,
                label=setting.label,
                unit=setting.unit,
                current=stored,
                value=setting.value,
            )
        )
    return ProfilePreview(profile=profile, process_preset=process, applicable=True, changes=changes)


def overrides(preview: ProfilePreview) -> dict[str, ProcessValue]:
    """The ``process_overrides`` an applicable preview adds: each value in the shape
    the preset stores it, every extruder variant set alike."""
    return {
        change.key: (
            [change.value] * len(change.current)
            if isinstance(change.current, list)
            else change.value
        )
        for change in preview.changes
    }
