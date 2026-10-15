"""The prime tower for a print (#3628).

The print dialog can turn Bambu Studio's prime (wipe) tower on or off for one print,
and with it on, "No sparse layers": the tower is printed only on layers that change
filament. Both reach the slicer as process overrides over the template's
``print_settings`` (#770), the channel supports use (#1899).

The keys are Bambu Studio's, from ``src/libslic3r/PrintConfig.cpp``: ``enable_prime_tower``
(coBool, "Enable" under Others → Prime tower) and ``wipe_tower_no_sparse_layers``
(coBool, "No sparse layers (experimental)", a Develop-mode option). Both are single
values in every Bambu process preset (``fdm_process_common.json``: ``"1"`` and ``"0"``),
so they are sent as they are, never shaped per extruder variant. Bambu Studio itself
still turns the tower off for a plate that prints one filament.

A process preset that has no value for one of them (a custom or non-Bambu one) gets it
anyway, with a warning that the slicer may not take it (:func:`prime_tower_warnings`).
"""

from __future__ import annotations

from pydantic import BaseModel

from scadbuddy.bambuddy.client import BambuddyClient, ReadPresetValues
from scadbuddy.bambuddy.filaments import FilamentWarning
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.core.problems import ApiError

#: The Bambu Studio process keys a prime tower choice sets.
PRIME_TOWER_KEYS: tuple[str, ...] = ("enable_prime_tower", "wipe_tower_no_sparse_layers")


class PrimeTowerChoice(BaseModel):
    """The prime tower for one print, over whatever the template declares."""

    #: Whether the slicer prints a prime tower when the plate changes filament.
    enabled: bool
    #: With the tower on, print it only on layers that change filament; omitted keeps
    #: the template's, else the process preset's (off). Ignored with the tower off.
    no_sparse_layers: bool | None = None


def prime_tower_overrides(choice: PrimeTowerChoice | None) -> dict[str, str]:
    """The process overrides ``choice`` asks for: none for no choice, only
    ``enable_prime_tower`` with the tower off."""
    if choice is None:
        return {}
    if not choice.enabled:
        return {"enable_prime_tower": "0"}
    wanted = {"enable_prime_tower": "1"}
    if choice.no_sparse_layers is not None:
        wanted["wipe_tower_no_sparse_layers"] = "1" if choice.no_sparse_layers else "0"
    return wanted


async def prime_tower_warnings(
    client: BambuddyClient,
    process: PresetRef | None,
    choice: PrimeTowerChoice | None,
    *,
    read: ReadPresetValues | None = None,
) -> list[FilamentWarning]:
    """A warning naming each setting ``choice`` sends that the process preset has no
    value for, as supports warn (#1899). Never refuses, and the setting still goes.

    Nothing is said when the preset cannot be read: both keys are single values, so
    there is no shape to get wrong, and every Bambu preset has them."""
    wanted = prime_tower_overrides(choice)
    if not wanted or process is None:
        return []
    try:
        answer = await (read or client.preset_values)(process)
    except ApiError:
        return []
    if not answer.resolved:
        return []
    missing = [key for key in wanted if key not in answer.values]
    if not missing:
        return []
    return [
        FilamentWarning(
            kind="prime-tower-unconfirmed",
            message=(
                f"The process preset has no value for {', '.join(missing)}: it is sent "
                "anyway, but a slicer that does not know it prints the tower as the preset "
                "says."
            ),
        )
    ]
