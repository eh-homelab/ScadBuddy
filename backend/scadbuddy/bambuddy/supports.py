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


class SupportsChoice(BaseModel):
    """Supports for one print, over whatever the template declares."""

    #: Whether the slicer generates supports at all.
    enabled: bool
    #: Tree supports (Bambu Studio's default) or normal ones.
    type: SupportType = "tree"
    #: Overhangs steeper than this many degrees from vertical get supports; omitted is
    #: the template's angle, else the process preset's (Bambu's default is 30).
    threshold_angle: int | None = Field(default=None, ge=0, le=90)
    #: Supports only from the build plate, never from the model itself.
    on_build_plate_only: bool = False


def wanted_settings(template: Mapping[str, str], choice: SupportsChoice | None) -> dict[str, str]:
    """The support settings a print asks for: the template's, then the print's own."""
    wanted = {key: template[key] for key in SUPPORT_KEYS if key in template}
    if choice is None:
        return wanted
    if not choice.enabled:
        return {"enable_support": "0"}
    wanted.update(
        {
            "enable_support": "1",
            "support_type": f"{choice.type}(auto)",
            "support_on_build_plate_only": "1" if choice.on_build_plate_only else "0",
        }
    )
    if choice.threshold_angle is not None:
        wanted["support_threshold_angle"] = str(choice.threshold_angle)
    return {key: wanted[key] for key in SUPPORT_KEYS if key in wanted}


def _not_applied(message: str) -> FilamentWarning:
    return FilamentWarning(kind="supports-not-applied", message=message)


async def support_overrides(
    client: BambuddyClient, process: PresetRef | None, wanted: Mapping[str, str]
) -> tuple[dict[str, ProcessValue], list[FilamentWarning]]:
    """``wanted`` as process overrides in the shape ``process`` stores each setting,
    and a warning for what had to be left out. Never refuses: a setting whose shape
    cannot be read is not sent, and the print goes ahead without it."""
    if not wanted:
        return {}, []
    reason: str | None = None
    values: Mapping[str, object] | None = None
    if process is None:
        reason = "no process preset was resolved for this print"
    else:
        try:
            read = await client.preset_values(process)
        except ApiError as error:
            reason = error.detail
        else:
            if read.resolved:
                values = read.values
            else:
                reason = f"Bambuddy could not read them ({read.reason or 'no reason given'})"
    if values is None:
        return {}, [
            _not_applied(
                "Printing without the support settings: the process preset's values "
                f"could not be read, so they could not be written in its shape: {reason}."
            )
        ]
    overrides: dict[str, ProcessValue] = {}
    missing: list[str] = []
    for key, value in wanted.items():
        current = values.get(key)
        if isinstance(current, list) and current:
            overrides[key] = [value] * len(current)
        elif current is None or isinstance(current, list | dict):
            missing.append(key)
        else:
            overrides[key] = value
    if not missing:
        return overrides, []
    return overrides, [
        _not_applied(
            f"Printing without {', '.join(missing)}: the process preset has no value to "
            "write it over."
        )
    ]
