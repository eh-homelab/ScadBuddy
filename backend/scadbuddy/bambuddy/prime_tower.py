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
"""

from __future__ import annotations

from pydantic import BaseModel

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
