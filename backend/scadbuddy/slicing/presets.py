"""A slice plan's presets as the files the native slicer loads (#3822, spec 2026-10-16
§5): one machine, one process and one filament JSON per slot, each flattened.

Built in an activity, never in a workflow: the files are tens of kilobytes each, and
:class:`PresetFiles` never crosses a history (each is stored as a blob).

Where a preset comes from:

- ``standard`` and ``cloud`` presets are Bambu Studio's own, read from the image's BBL
  tree (:class:`~scadbuddy.slicing.profiles.Profiles`) by id, then by the name
  Bambuddy's catalogue gives the id.
- A process preset that is not in the tree (``local``, a cloud user preset, or a
  ``standard`` one this image's Bambu Studio does not ship) takes Bambuddy's flattened
  values (``GET /slicer/preset-values``). Bambuddy's slicer may carry other profiles
  than the image's (02.08.00.06 against 02.08.00.05 on 2026-10-18), so such a slice's
  key carries a digest of the process file it wrote (:attr:`PresetFiles.bambuddy_process`,
  :func:`native_preset_key`), never only the image's version.
- A printer or filament preset that is not in the tree is refused: Bambuddy answers
  ``GET /slicer/preset-values?slot=filament`` with ``400 "Only the 'process' slot is
  supported"`` (measured 2026-09-27, and again 2026-10-18 against the live
  ``ghcr.io/maziggy/bambuddy:daily@sha256:99684c2f…`` with ``source=cloud&id=GFSB00_22``).
- ``orca_cloud`` presets are OrcaSlicer's, not Bambu Studio's, and are refused.

The plate type goes in the process file's ``curr_bed_type``: the v02.08.02.61 CLI has
no plate-type option (``AppRun --help`` lists none), and a plate whose 3MF names no
bed type of its own takes ``curr_bed_type`` from the config the process file is
applied to (``BambuStudio.cpp``'s plate loop, "bed type: %2%, set from global";
https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/BambuStudio.cpp).
Measured 2026-10-18 with the worker's argv in the slicer image: MakerWorld's
``2154-plane-launcher-for-lightweight-glider.3mf`` (whose own settings say ``Textured
PEI Plate``), sliced with this module's files for an A1, heats the bed to 35 °C with
``Cool Plate`` and to 65 °C with ``Textured PEI Plate``.
The filament colours are not written: the laid-out 3MF carries them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel

from scadbuddy.bambuddy.catalogue import read_catalogue
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import SlicePlan, slice_request
from scadbuddy.bambuddy.models import PresetRef, ProcessValue
from scadbuddy.slicing.profiles import Flat, Kind, ProfileError, Profiles, flatten

Slot = Literal["printer", "process", "filament"]
_KINDS: dict[Slot, Kind] = {"printer": "machine", "process": "process", "filament": "filament"}


class PresetFiles(BaseModel):
    """The JSON documents a slice loads, in the plan's slot order. Never crosses a
    workflow history."""

    machine: bytes
    process: bytes
    filaments: list[bytes]
    #: The sha256 of :attr:`process` when Bambuddy's values built it, for
    #: :func:`native_preset_key`; ``None`` when the image's tree did.
    bambuddy_process: str | None = None


def apply_overrides(process: Flat, overrides: Mapping[str, ProcessValue]) -> Flat:
    """``process`` with ``overrides`` written over it, as Bambuddy writes a slice's
    ``process_overrides``: a string over a string; a list over a list position by
    position, the positions it does not name kept (#717); a string over a list sets
    every position.

    A list over a single value is refused: the preset stores that option as one value,
    and Bambu Studio's JSON loader joins a list with commas and reads the result as the
    option (``ConfigBase::load_from_json``, Config.cpp, v02.08.02.61), so ``["3", "3"]``
    would reach a single-value option as ``"3,3"``: refused, or misread. A process
    built from Bambuddy's values is checked the same way: ``/slicer/preset-values``
    answers a vector option as a list (measured 2026-10-18 on 30 cloud and standard
    process presets, 1330 list values, none of the image's vector options a string).

    A list for an option the preset leaves out is written whole: the preset takes Bambu
    Studio's default there, whose shape this side cannot see, and the override's shape
    is the only one there is."""
    out = dict(process)
    for key, value in overrides.items():
        current = out.get(key)
        if isinstance(value, str):
            out[key] = [value] * len(current) if isinstance(current, list) else value
        elif isinstance(current, list):
            out[key] = [*value, *current[len(value) :]]
        elif current is None:
            out[key] = list(value)
        else:
            raise ProfileError(
                f"the process override {key} is a list, but the preset stores {key} as a"
                " single value"
            )
    return out


class _Names:
    """Bambuddy's catalogue names, read once and only when a ref needs one."""

    def __init__(self, client: BambuddyClient | None) -> None:
        self._client = client
        self._names: dict[tuple[str, str], str] | None = None

    async def of(self, ref: PresetRef) -> str | None:
        if self._client is None:
            return None
        if self._names is None:
            self._names = (await read_catalogue(self._client)).names()
        return self._names.get((ref.source, ref.id))


async def _resolve(
    slot: Slot,
    ref: PresetRef,
    profiles: Profiles,
    client: BambuddyClient | None,
    names: _Names,
) -> tuple[Flat, bool]:
    """The ``slot`` preset ``ref`` names, and whether Bambuddy's values built it."""
    kind = _KINDS[slot]
    if ref.source == "orca_cloud":
        raise ProfileError(
            f"the native slicer cannot read orca_cloud {slot} preset {ref.id}:"
            " OrcaSlicer's presets are not Bambu Studio's"
        )
    if ref.source in ("standard", "cloud"):
        found = await asyncio.to_thread(profiles.find, kind, ref.id)
        if found is None:
            name = await names.of(ref)
            if name is not None:
                found = await asyncio.to_thread(profiles.find, kind, name)
        if found is not None:
            return await asyncio.to_thread(getattr(profiles, kind), found), False
    name = await names.of(ref) or ref.id
    if slot != "process":
        version = await asyncio.to_thread(lambda: profiles.version)
        raise ProfileError(
            f"the native slicer cannot read {ref.source} {slot} preset {name}: it is not"
            f" one of Bambu Studio {version}'s, and Bambuddy serves only process"
            " preset values"
        )
    if client is None:
        raise ProfileError(
            f"the native slicer cannot read {ref.source} process preset {name} without Bambuddy"
        )
    values = await client.preset_values(ref)
    if not values.resolved:
        raise ProfileError(
            f"Bambuddy could not read {ref.source} process preset {name}: {values.reason}"
        )
    # Already flattened by Bambuddy's slicer; flatten() checks every value is text.
    own = {key: value for key, value in values.values.items() if key != "inherits"}
    return flatten(name, {name: own}.get), True


def _document(flat: Flat) -> bytes:
    return json.dumps(flat, indent=4, ensure_ascii=False).encode()


async def build_presets(
    plan: SlicePlan, profiles: Profiles, client: BambuddyClient | None
) -> PresetFiles:
    """The files that slice ``plan``: its presets flattened, its process overrides and
    plate type written over the process. ``client`` names refs and reads a process
    preset only Bambuddy holds; without one, only the image's presets resolve."""
    names = _Names(client)
    machine, _ = await _resolve("printer", plan.printer_preset, profiles, client, names)
    process, from_bambuddy = await _resolve("process", plan.process_preset, profiles, client, names)
    filaments = [
        (await _resolve("filament", ref, profiles, client, names))[0]
        for ref in plan.filament_presets
    ]
    process = apply_overrides(process, plan.process_overrides)
    if plan.bed_type:
        process["curr_bed_type"] = plan.bed_type
    document = _document(process)
    return PresetFiles(
        machine=_document(machine),
        process=document,
        filaments=[_document(filament) for filament in filaments],
        bambuddy_process=hashlib.sha256(document).hexdigest() if from_bambuddy else None,
    )


def native_preset_key(
    plan: SlicePlan, plate_id: int, version: str, bambuddy_process: str | None = None
) -> str:
    """What makes two native slices the same: Bambuddy's preset key behind the image's
    Bambu Studio version, so a native slice never answers for a Bambuddy one, and a new
    slicer slices again (Ruling C7). ``bambuddy_process`` is
    :attr:`PresetFiles.bambuddy_process`: a process file Bambuddy's values built is
    keyed by what it held, since the image's version does not say which profiles
    Bambuddy's slicer flattened it from."""
    key = f"native@{version}/" + slice_request(plan, plate_id).preset_key
    if bambuddy_process is not None:
        key += f"/bambuddy-process={bambuddy_process}"
    return key
