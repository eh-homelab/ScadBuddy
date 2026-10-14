"""Support structures for a print (#1899).

A template declares the supports it prints best with in its ``print_settings``
(``model.json``, #770), and the print dialog or the agent can choose them per print.
Either way they reach the slicer as process overrides, the channel #717's filament
profiles use, each written in the shape the print's process preset stores it: on an
H2C some process settings are one entry per extruder variant, and a bare value would
collapse that list.

Supports are a nicety, not a correctness condition: a value that cannot be written in
the preset's shape is left out with a warning, and the print goes ahead without it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.filaments import FilamentWarning
from scadbuddy.bambuddy.models import PresetRef, ProcessValue
from scadbuddy.core.problems import ApiError

#: The Bambu Studio process keys a support choice sets, in the order they are listed.
SUPPORT_KEYS: tuple[str, ...] = (
    "enable_support",
    "support_type",
    "support_threshold_angle",
    "support_on_build_plate_only",
)

#: Bambu Studio's ``support_type`` families. ScadBuddy asks for the automatic variant
#: of each: a manual one needs support painted in Studio, which a template cannot do.
SupportType = Literal["tree", "normal"]


#: The support keys every Bambu process preset stores as one value, not one per extruder
#: variant (``fdm_process_common``): sent as they are when the preset cannot be read,
#: as a template's were before #1899, rather than dropped (review of #2835).
SCALAR_KEYS = frozenset({"enable_support", "support_type", "support_on_build_plate_only"})


class SupportsChoice(BaseModel):
    """Supports for one print, over whatever the template declares. A field left out
    keeps the template's value, else the process preset's."""

    #: Whether the slicer generates supports at all.
    enabled: bool
    #: Tree supports (Bambu Studio's default) or normal ones; omitted keeps the
    #: template's type, else tree.
    type: SupportType | None = None
    #: Overhangs steeper than this many degrees from vertical get supports; omitted is
    #: the template's angle, else the process preset's (Bambu's default is 30).
    threshold_angle: int | None = Field(default=None, ge=0, le=90)
    #: Supports only from the build plate, never from the model itself; omitted keeps
    #: the template's, else the preset's.
    on_build_plate_only: bool | None = None


def wanted_settings(template: Mapping[str, str], choice: SupportsChoice | None) -> dict[str, str]:
    """The support settings a print asks for: the template's, then the print's own."""
    wanted = {key: template[key] for key in SUPPORT_KEYS if key in template}
    if choice is None:
        return wanted
    if not choice.enabled:
        return {"enable_support": "0"}
    wanted["enable_support"] = "1"
    if choice.type is not None:
        wanted["support_type"] = f"{choice.type}(auto)"
    elif "support_type" not in wanted:
        wanted["support_type"] = "tree(auto)"
    if choice.on_build_plate_only is not None:
        wanted["support_on_build_plate_only"] = "1" if choice.on_build_plate_only else "0"
    if choice.threshold_angle is not None:
        wanted["support_threshold_angle"] = str(choice.threshold_angle)
    return {key: wanted[key] for key in SUPPORT_KEYS if key in wanted}


def _not_applied(message: str) -> FilamentWarning:
    return FilamentWarning(kind="supports-not-applied", message=message)


async def support_overrides(
    client: BambuddyClient, process: PresetRef | None, wanted: Mapping[str, str]
) -> tuple[dict[str, ProcessValue], list[FilamentWarning]]:
    """``wanted`` as process overrides in the shape ``process`` stores each setting,
    and a warning for what had to be left out. Never refuses.

    When the preset cannot be read, the keys every preset stores as one value
    (:data:`SCALAR_KEYS`) still go as they are, so a template's supports print as they
    did before #1899; only a setting whose shape is in doubt is left out."""
    if not wanted:
        return {}, []
    reason: str | None = None
    values: Mapping[str, object] | None = None
    if process is None:
        reason = "no process preset was resolved for this print"
    else:
        try:
            read = await client.preset_values(process)
        except ApiError:
            # Not ``error.detail``: that repeats the operation ("read the values of
            # process preset …"), which the sentence around it already says. Nor
            # ``error.status``: that is the status ScadBuddy maps the failure to, not
            # necessarily one Bambuddy sent (#3183).
            reason = "the read failed (Bambuddy unreachable or answering with an error)"
        else:
            if read.resolved:
                values = read.values
            else:
                reason = f"Bambuddy could not read them ({read.reason or 'no reason given'})"
    if values is None:
        sent: dict[str, ProcessValue] = {k: v for k, v in wanted.items() if k in SCALAR_KEYS}
        withheld = [key for key in wanted if key not in SCALAR_KEYS]
        said: list[str] = []
        if withheld:
            said.append(
                f"Printing without {', '.join(withheld)}: the process preset's values could "
                "not be read, so it could not be written in the preset's shape"
            )
        if sent:
            # Said, though it is the usual shape: a preset that stores one as a list
            # would take it unshaped (#3183).
            said.append(
                f"{', '.join(sent)} sent as single values, unchecked against the preset's shape"
            )
        if not said:
            return sent, []
        return sent, [_not_applied(f"{'; '.join(said)}: {reason}.")]
    overrides: dict[str, ProcessValue] = {}
    problems: list[str] = []
    for key, value in wanted.items():
        current = values.get(key)
        if isinstance(current, list) and current:
            overrides[key] = [value] * len(current)
        elif current is None and key in SCALAR_KEYS:
            # As when the read fails: one value, whatever the preset leaves out.
            overrides[key] = value
        elif current is None:
            problems.append(f"{key} (the process preset has no value to write it over)")
        elif isinstance(current, list | dict):
            problems.append(f"{key} (the process preset stores it as an empty or nested value)")
        else:
            overrides[key] = value
    if not problems:
        return overrides, []
    return overrides, [_not_applied(f"Printing without {'; '.join(problems)}.")]
