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
applied rather than sent in a guessed shape: the print goes ahead with the preset as it
is, and its Checks say the profile was not applied and why.

A profile's filament settings (Silk's 235 °C nozzle, #2799) go out only on the native
slicer (``slicer_backend`` ``native``), written into the filament file of each slot
whose spool matches the profile (:func:`slot_filament_overrides`,
``SlicePlan.filament_overrides``): the channel measured to change the G-code, where the
same key in the process file changes nothing (``slicing/presets.py``'s docstring).
Bambuddy's ``SliceRequest`` has no filament field, so on Bambuddy's slicer they are not
applied, and the run's warnings say so (:func:`filament_not_applied`).
"""

from __future__ import annotations

from typing import NamedTuple

from pydantic import BaseModel, Field

from scadbuddy.analyzers import sources as src
from scadbuddy.analyzers.model import Source
from scadbuddy.bambuddy.client import BambuddyClient, ReadPresetValues, preset_reader
from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, FilamentWarning
from scadbuddy.bambuddy.models import NOT_IN_PROFILES, PresetRef, ProcessValue
from scadbuddy.core.problems import ApiError


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
    """One setting a profile changes, and why: a process setting, or (in
    :attr:`FilamentProfile.filament_settings`) a setting of the matching slots' filament."""

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
    #: Settings of the matching slots' filament presets, such as the nozzle temperature
    #: (#2799): applied by the native slicer only (the module's docstring).
    filament_settings: list[ProfileSetting] = Field(default_factory=list)
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
    filament_settings=[
        ProfileSetting(
            key="nozzle_temperature",
            label="Nozzle temperature",
            value="235",
            unit="°C",
            sources=[src.SILK_TEMPERATURE, src.SILK_TEMPERATURE_RANGE],
        ),
        ProfileSetting(
            key="nozzle_temperature_initial_layer",
            label="Nozzle temperature (first layer)",
            value="235",
            unit="°C",
            sources=[src.SILK_TEMPERATURE],
        ),
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
    #: why, and a print with it on goes ahead without it, saying so in its warnings.
    applicable: bool
    reason: str | None = None
    changes: list[ProfileChange] = Field(default_factory=list)
    #: Whether the profile's ``filament_settings`` are applied on this slicer: only the
    #: native one has the channel (#2799). When false, ``filament_reason`` says why.
    filament_applied: bool = True
    filament_reason: str | None = None


#: Why a profile's filament settings are not applied on Bambuddy's slicer (#2799).
NO_FILAMENT_CHANNEL = (
    "Bambuddy's slicer has no filament setting override, so set them in the spool's "
    "filament preset. The native slicer applies them"
)


_REASONS = {
    "sidecar_unavailable": "Bambuddy's slicer is not reachable",
    "not_configured": "Bambuddy has no slicer configured",
    "preset_unresolved": "Bambuddy could not find the process preset",
    NOT_IN_PROFILES: "it is not one of Bambu Studio's own presets, which a native print reads",
}


def _settings(profile: FilamentProfile) -> str:
    """ "outer wall speed", "outer wall speed and inner wall speed"…"""
    labels = [setting.label.lower() for setting in profile.settings]
    return " and ".join([", ".join(labels[:-1]), labels[-1]] if len(labels) > 1 else labels)


def unknown_profiles(ids: list[str]) -> list[str]:
    return [profile_id for profile_id in ids if profile_id not in BUILTIN]


async def previews(
    client: BambuddyClient | None,
    process: PresetRef | None,
    profiles: list[FilamentProfile],
    *,
    read: ReadPresetValues | None = None,
    native_slicer: bool = False,
) -> list[ProfilePreview]:
    """Each of ``profiles``' changes against ``process``'s values, in their stored shape,
    and whether its filament settings apply (only on the ``native_slicer``, #2799).
    The preset's values are read once, whatever the number of profiles; ``read`` is a
    reader shared with the print's other overrides (#2804 review)."""
    shown = await _process_previews(client, process, profiles, read=read)
    if native_slicer:
        return shown
    return [
        preview.model_copy(
            update={"filament_applied": False, "filament_reason": NO_FILAMENT_CHANNEL}
        )
        if preview.profile.filament_settings
        else preview
        for preview in shown
    ]


async def _process_previews(
    client: BambuddyClient | None,
    process: PresetRef | None,
    profiles: list[FilamentProfile],
    *,
    read: ReadPresetValues | None,
) -> list[ProfilePreview]:
    if not profiles:
        return []
    if process is None:
        return [
            ProfilePreview(
                profile=profile,
                applicable=False,
                reason=(
                    "no process preset resolves for these choices, so there is no "
                    f"{_settings(profile)} to change"
                ),
            )
            for profile in profiles
        ]
    why: str | None
    try:
        values = await preset_reader(client, read)(process)
    except ApiError:
        # Unreachable or failing: skip the profiles, never refuse the print for them.
        # Not ``error.detail``, which repeats the operation the sentence already names;
        # the same fixed phrase supports.py uses (#3183, #2804 review).
        why = "the read failed (Bambuddy unreachable or answering with an error)"
    else:
        why = (
            None
            if values.resolved
            else _REASONS.get(values.reason or "", f"Bambuddy answered {values.reason!r}")
        )
    if why is not None:
        return [
            ProfilePreview(
                profile=profile,
                process_preset=process,
                applicable=False,
                reason=f"couldn't read the preset's {_settings(profile)} ({why})",
            )
            for profile in profiles
        ]
    return [_against(process, values.values, profile) for profile in profiles]


def _against(
    process: PresetRef, values: dict[str, object], profile: FilamentProfile
) -> ProfilePreview:
    changes: list[ProfileChange] = []
    for setting in profile.settings:
        current = values.get(setting.key)
        if isinstance(current, list):
            # An empty list has no shape to copy: the preset has no such setting.
            stored: ProcessValue | None = [str(item) for item in current] or None
        elif current is None:
            stored = None
        else:
            stored = str(current)
        if stored is None:
            return ProfilePreview(
                profile=profile,
                process_preset=process,
                applicable=False,
                reason=f"the preset has no {setting.label.lower()} ({setting.key})",
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


def not_applied(preview: ProfilePreview) -> FilamentWarning:
    """The Checks warning for a profile left on that cannot apply to this print."""
    return FilamentWarning(
        kind="profile-not-applied",
        message=(
            f"{preview.profile.name} profile not applied: {preview.reason}. The print uses "
            "the preset as it is."
        ),
    )


def filament_not_applied(profile: FilamentProfile) -> FilamentWarning:
    """The Checks warning for a profile whose filament settings Bambuddy's slicer cannot
    apply (#2799): its process changes still go out."""
    labels = " and ".join(setting.label.lower() for setting in profile.filament_settings)
    return FilamentWarning(
        kind="profile-not-applied",
        message=f"{profile.name} profile's {labels} not applied: {NO_FILAMENT_CHANNEL}.",
    )


class ProfileOverrides(NamedTuple):
    #: The process overrides of every profile that applies.
    process: dict[str, ProcessValue]
    warnings: list[FilamentWarning]
    #: The ids of the profiles that apply, whose filament settings
    #: :func:`slot_filament_overrides` writes on the native slicer.
    applied: list[str]


async def profile_overrides(
    client: BambuddyClient | None,
    process: PresetRef | None,
    profile_ids: list[str],
    *,
    read: ReadPresetValues | None = None,
    native_slicer: bool = False,
) -> ProfileOverrides:
    """The process overrides of every profile a print applies, each read against the
    print's own process preset in the shape it stores them. A profile that cannot apply
    is skipped, never guessed at, and a warning says so: the print is not refused for it,
    and goes ahead with the preset as it is. On Bambuddy's slicer (not ``native_slicer``)
    a profile's filament settings are not applied, and a warning says so (#2799)."""
    merged: dict[str, ProcessValue] = {}
    warnings: list[FilamentWarning] = []
    applied: list[str] = []
    for shown in await previews(
        client, process, [BUILTIN[i] for i in profile_ids], read=read, native_slicer=native_slicer
    ):
        if not shown.applicable:
            warnings.append(not_applied(shown))
            continue
        merged.update(overrides(shown))
        applied.append(shown.profile.id)
        if not shown.filament_applied:
            warnings.append(filament_not_applied(shown.profile))
    return ProfileOverrides(merged, warnings, applied)


def slot_filament_overrides(
    profile_ids: list[str], options: FilamentOptions, plan: FilamentPlan
) -> dict[int, dict[str, str]]:
    """``SlicePlan.filament_overrides`` for one plate (#2799): each applied profile's
    filament settings, for every slot of the plate whose chosen spool the profile
    matches (:meth:`ProfileMatch.matches`, as the dialog suggests it for that spool).
    A later profile's value for a key wins, as in the process overrides."""
    by_id = {option.spool_id: option for option in options.spools}
    out: dict[int, dict[str, str]] = {}
    for slot in options.slots:
        spool_id = plan.spool_for(slot.slot_id)
        spool = by_id.get(spool_id) if spool_id is not None else None
        if spool is None:
            continue
        for profile_id in profile_ids:
            profile = BUILTIN[profile_id]
            if profile.filament_settings and profile.match.matches(
                material=spool.material,
                subtype=spool.subtype,
                preset_name=spool.slicer_filament_name,
            ):
                out.setdefault(slot.slot_id, {}).update(
                    {setting.key: setting.value for setting in profile.filament_settings}
                )
    return out
