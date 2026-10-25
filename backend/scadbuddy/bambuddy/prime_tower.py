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

import asyncio
import io
import logging
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from typing import Protocol

from pydantic import BaseModel, model_validator

from scadbuddy.bambuddy.client import BambuddyClient, ReadPresetValues, preset_reader
from scadbuddy.bambuddy.filaments import FilamentWarning
from scadbuddy.bambuddy.filaments3mf import read_filaments
from scadbuddy.bambuddy.models import PresetRef, PresetValues
from scadbuddy.core.problems import ApiError

logger = logging.getLogger(__name__)

#: The Bambu Studio process keys a prime tower choice sets. ``frontend/src/lib/primeTower.ts``
#: copies them (``PRIME_TOWER_KEYS``) to strike them in the template's slicer defaults;
#: a key added here goes there too, and both tests pin the list.
PRIME_TOWER_KEYS: tuple[str, ...] = ("enable_prime_tower", "wipe_tower_no_sparse_layers")


class PrimeTowerChoice(BaseModel):
    """The prime tower for one print, over whatever the template declares."""

    #: Whether the slicer prints a prime tower when the plate changes filament.
    enabled: bool
    #: With the tower on, print it only on layers that change filament; omitted keeps
    #: the template's, else the process preset's (off). Ignored with the tower off.
    no_sparse_layers: bool | None = None

    @model_validator(mode="after")
    def _off_is_only_off(self) -> PrimeTowerChoice:
        # With the tower off, "No sparse layers" means nothing: dropped, so the two ways
        # of saying "off" are one print with one run key (#3648 review).
        if not self.enabled:
            self.no_sparse_layers = None
        return self


def template_keys_dropped(choice: PrimeTowerChoice | None) -> frozenset[str]:
    """The template's ``print_settings`` keys ``choice`` leaves unsent: its "No sparse
    layers" with the tower off, as the dialog strikes it (#3648 review)."""
    if choice is not None and not choice.enabled:
        return frozenset({"wipe_tower_no_sparse_layers"})
    return frozenset()


def not_resliced(choice: PrimeTowerChoice | None) -> list[FilamentWarning]:
    """A sliced 3MF queues as it is (#2671), so a tower choice for it is said not to
    apply rather than silently ignored (#3648 review)."""
    if choice is None:
        return []
    return [
        FilamentWarning(
            kind="prime-tower-not-applied",
            message=(
                "This file is already sliced, so it prints with the prime tower it was "
                "sliced with: the tower chosen here is not applied."
            ),
        )
    ]


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
    client: BambuddyClient | None,
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
        answer = await preset_reader(client, read)(process)
    except ApiError:
        return []
    if not answer.resolved:
        return []
    missing = [key for key in wanted if key not in answer.values]
    if not missing:
        return []
    effects = "; ".join(f"{key} ({_UNKNOWN_EFFECT[key][wanted[key] == '1']})" for key in missing)
    return [
        FilamentWarning(
            kind="prime-tower-unconfirmed",
            message=(
                "The process preset has no value for these, so they are sent anyway, and a "
                f"slicer that does not know one prints as if it were not: {effects}."
            ),
        )
    ]


#: What a slicer that does not know a key prints instead, by whether it was asked on.
_UNKNOWN_EFFECT: dict[str, tuple[str, str]] = {
    "enable_prime_tower": ("the tower may still be printed", "no tower may be printed"),
    "wipe_tower_no_sparse_layers": (
        "the tower may skip layers with no filament change",
        "the tower keeps its sparse layers",
    ),
}


#: A sliced file past this is not read for its prime tower, as the dialog bounds it.
MAX_SLICED_BYTES = 128 * 1024 * 1024


class SlicedFiles(Protocol):
    """The reads :func:`tower_dropped` makes; ``BambuddyClient`` is one."""

    def download_library_file(self, file_id: int) -> AsyncGenerator[bytes]: ...

    async def preset_values(self, ref: PresetRef) -> PresetValues: ...


async def tower_dropped(
    client: SlicedFiles,
    *,
    sliced: int,
    plate_id: int,
    process: PresetRef,
    process_overrides: Mapping[str, object],
) -> list[FilamentWarning]:
    """Say so when Bambuddy's slice of ``plate_id`` came back with no prime tower that
    was asked for, on a plate printing two or more filaments (#3962). Bambu Studio's CLI
    turns the tower off when every slot's preset has one name, and per-slot presets
    (``slot_presets``) are what keeps it; this is how a slot that kept the shared one
    shows. A warning, never a refusal, and it never raises: an unread file says nothing.

    Asked for means ``enable_prime_tower`` ``"1"`` among the plan's process overrides,
    or none at all, the slice's value not its system parent's
    (:attr:`SlicedProfile.prime_tower_changed`) and ``process``'s own value ``"1"``: a
    user preset that turns the tower off differs from its parent too (#4648), so the
    preset is read, and one that cannot be read says nothing. A plate asked to print no
    tower is not read."""
    asked = process_overrides.get("enable_prime_tower")
    if asked == "0":
        return []
    try:
        data = bytearray()
        async with aclosing(client.download_library_file(sliced)) as chunks:
            async for chunk in chunks:
                data += chunk
                if len(data) > MAX_SLICED_BYTES:
                    return []
        read = await asyncio.to_thread(read_filaments, io.BytesIO(bytes(data)), sliced=True)
    except Exception as error:
        logger.info(
            "the sliced file's prime tower could not be read",
            extra={"library_file_id": sliced, "error": type(error).__name__},
        )
        return []
    profile = read.profile
    if profile is None or profile.prime_tower is not False:
        return []
    if asked != "1" and not profile.prime_tower_changed:
        return []
    plate = read.sliced.get(plate_id)
    if plate is None and len(read.sliced) == 1:
        plate = next(iter(read.sliced.values()))
    filaments = len({filament.slot_id for filament in plate or []})
    if filaments < 2:
        return []
    if asked != "1" and not await _process_tower_on(client, process):
        return []
    return [
        FilamentWarning(
            kind="prime-tower-not-applied",
            message=(
                f"Bambuddy's slicer dropped the prime tower from plate {plate_id}, which"
                f" prints {filaments} filaments: the tower was asked for, and the sliced"
                " file has none. Its slicer usually does this when the filament slots"
                " share one preset."
            ),
        )
    ]


async def _process_tower_on(client: SlicedFiles, process: PresetRef) -> bool:
    """Whether ``process``'s own ``enable_prime_tower`` is ``"1"``; ``False`` when it
    cannot be read. A second Bambuddy call (``GET /slicer/preset-values``, scope
    ``MANAGE_LIBRARY``), made only for a tower dropped with no override: a key without
    that scope, or a sidecar that cannot flatten the preset, says nothing rather than a
    warning that may be false (#4648)."""
    try:
        answer = await client.preset_values(process)
    except Exception as error:
        logger.info(
            "the process preset's prime tower could not be read",
            extra={"source": process.source, "error": type(error).__name__},
        )
        return False
    value = answer.values.get("enable_prime_tower")
    if isinstance(value, list):
        value = value[0] if value else None
    return answer.resolved and value == "1"
