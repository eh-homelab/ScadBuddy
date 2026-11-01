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
  key carries a digest of the process file it wrote (:attr:`PresetFiles.from_bambuddy`,
  :func:`native_preset_key`), never only the image's version.
- A ``local`` printer or filament preset (a user's own, imported into Bambuddy from an
  OrcaSlicer or Bambu Studio profile, as the spool intake does from 3dfilamentprofiles)
  is read from Bambuddy's ``GET /local-presets/{id}`` (#4233): its ``setting`` is the
  preset's own document, already merged over the OrcaSlicer BBL base its ``inherits``
  names, which Bambuddy's own slicer slices with verbatim
  (:class:`~scadbuddy.bambuddy.models.LocalPresetDetail`). Here it is flattened over the
  image's tree by that same ``inherits`` (:meth:`Profiles.flatten_over`), so a parent
  this Bambu Studio lacks refuses it, naming the parent, rather than slicing a document
  that the CLI would fill from its defaults. The slice's key carries the sha256 of each
  such file (:attr:`PresetFiles.from_bambuddy`): ``PUT /local-presets/{id}`` rewrites a
  preset under the same id. Bambuddy's values route serves no other slot: it answers
  ``GET /slicer/preset-values?slot=filament`` with ``400 "Only the 'process' slot is
  supported"`` (measured 2026-09-27, and again 2026-10-18 against the live
  ``ghcr.io/maziggy/bambuddy:daily@sha256:99684c2f…`` with ``source=cloud&id=GFSB00_22``),
  so a cloud user preset in the printer or filament slot is still refused.
- A ``local`` preset of any slot that the kept copy of Bambuddy's local presets holds
  (:attr:`Profiles.held`, `slicing/held_presets.py`, #4645) is built from that copy's
  document, flattened over the tree as above, and never read from Bambuddy, with a
  client or without: so a native run, which has none, slices it, and a slice's start and
  its prepare build it alike. A process preset is then the tree's flattening of the
  user's document, not Bambuddy's values. Its key carries the built file's digest as a
  read from Bambuddy does (the same ``from_bambuddy`` slot), so an edit, once kept,
  slices again. A ``local`` ref the copy lacks is read from Bambuddy as above.
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

A slot's filament settings (:attr:`SlicePlan.filament_overrides`, a filament profile's
nozzle temperature, #2799) are written into that slot's filament file, never the
process file: the CLI takes a filament option from the filament files it loads and
ignores the same key in a process file. Measured 2026-11-01 with the slicer image
``ghcr.io/eh-homelab/scadbuddy-slicer:main@sha256:098ac9e6…`` (Bambu Studio
02.08.02.61), the worker's argv, and this module's files for an A1 (``0.20mm Standard
@BBL A1``, ``Bambu PLA Basic @BBL A1`` in both slots), slicing MakerWorld's
``448-articulated-pumpkin.3mf`` (two filaments, one plate): plain, every print
temperature set under either tool is 220 °C (beside the flush's 240 °C).
``nozzle_temperature`` and ``nozzle_temperature_initial_layer`` at 235 in the process
file change nothing: the sliced ``project_settings.config`` still says
``["220", "220"]``, and the ``M104``/``M109`` lines are the plain slice's. The same two
in slot 2's filament file give ``["220", "235"]``, and every print temperature set
under ``T1`` is 235 °C while ``T0``'s stay 220 °C (spec 2026-10-13 §5, #2799).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.catalogue import read_preset_names
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import SlicePlan, slice_request
from scadbuddy.bambuddy.models import LocalPresetDetail, PresetRef, ProcessValue
from scadbuddy.core.problems import ApiError
from scadbuddy.slicing.profiles import (
    Flat,
    Kind,
    ProfileError,
    ProfileImageError,
    Profiles,
    flatten,
)

Slot = Literal["printer", "process", "filament"]
_KINDS: dict[Slot, Kind] = {"printer": "machine", "process": "process", "filament": "filament"}


class PresetFiles(BaseModel):
    """The JSON documents a slice loads, in the plan's slot order. Never crosses a
    workflow history."""

    machine: bytes
    process: bytes
    filaments: list[bytes]
    #: The sha256 of each file built from what Bambuddy holds, by slot (``process``: its
    #: flattened values; ``printer`` and ``filament_<n>``, ``n`` the slot from 1: a local
    #: preset's document), for :func:`native_preset_key`. A file the image's tree built
    #: is not in it.
    from_bambuddy: dict[str, str] = Field(default_factory=dict)


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
            self._names = await read_preset_names(self._client)
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
    if ref.source == "local":
        held = await _held(profiles, ref)
        if held is not None or slot != "process":
            return await _local(slot, ref, profiles, client, held), True
    name = await names.of(ref) or ref.id
    if slot != "process":
        version = await asyncio.to_thread(lambda: profiles.version)
        if client is None:
            raise ProfileError(
                f"the native slicer cannot find {ref.source} {slot} preset {name} among Bambu"
                f" Studio {version}'s by its id: finding it by name needs Bambuddy, and none"
                " is configured"
            )
        raise ProfileError(
            f"the native slicer cannot read {ref.source} {slot} preset {name}: it is not"
            f" one of Bambu Studio {version}'s, and Bambuddy serves only process"
            " preset values"
        )
    if client is None:
        raise ProfileError(
            f"the native slicer cannot read {ref.source} process preset {name}: it needs"
            " Bambuddy, and none is configured"
        )
    values = await client.preset_values(ref)
    if not values.resolved:
        raise ProfileError(
            f"Bambuddy could not read {ref.source} process preset {name}: {values.reason}"
        )
    # Already flattened by Bambuddy's slicer, its parent and includes applied (#4714);
    # flatten() checks every value is text.
    own = {key: value for key, value in values.values.items() if key not in ("inherits", "include")}
    return flatten(name, {name: own}.get), True


def _local_id(ref: PresetRef) -> int | None:
    """A Bambuddy local preset's id: a number (ASCII digits only)."""
    return int(ref.id) if ref.id.isascii() and ref.id.isdigit() else None


async def _held(profiles: Profiles, ref: PresetRef) -> LocalPresetDetail | None:
    """``local`` ``ref`` from the kept copy of Bambuddy's local presets (#4645), or
    ``None`` when it holds none (or there is no copy)."""
    preset_id = _local_id(ref)
    if profiles.held is None or ref.source != "local" or preset_id is None:
        return None
    return (await profiles.held.documents([preset_id])).get(preset_id)


async def held_flat(profiles: Profiles, slot: Slot, ref: PresetRef) -> Flat | None:
    """``local`` ``ref``'s ``slot`` preset flattened from the kept copy, as a slice builds
    it; ``None`` when the copy holds none. A :class:`ProfileError` when it cannot be read."""
    held = await _held(profiles, ref)
    return None if held is None else await _local(slot, ref, profiles, None, held)


async def _local(
    slot: Slot,
    ref: PresetRef,
    profiles: Profiles,
    client: BambuddyClient | None,
    held: LocalPresetDetail | None = None,
) -> Flat:
    """A ``local`` preset's file (#4233): Bambuddy's document for it, flattened over the
    image's tree by its ``inherits`` (the module's docstring). The document is the kept
    copy's (``held``, #4645) when it has one, else read from Bambuddy (``GET
    /local-presets/{id}``). The row names the preset, so the catalogue is not read."""
    what = f"the native slicer cannot read local {slot} preset"
    row = held
    if row is None:
        if client is None:
            version = await asyncio.to_thread(lambda: profiles.version)
            raise ProfileError(
                f"{what} {ref.id}: it is not one of Bambu Studio {version}'s, nor in"
                " ScadBuddy's copy of Bambuddy's local presets (kept with the spools), and"
                " reading it from Bambuddy needs Bambuddy, and none is configured"
            )
        preset_id = _local_id(ref)
        if preset_id is None:
            raise ProfileError(f"{what} {ref.id}: a Bambuddy local preset's id is a number")
        try:
            row = await client.local_preset(preset_id)
        except ApiError as error:
            if error.status != status.HTTP_404_NOT_FOUND:
                raise
            raise ProfileError(
                f"{what} {ref.id}: Bambuddy has no local preset {preset_id} any more"
            ) from None
    preset_id = row.id
    if row.preset_type != slot:
        raise ProfileError(
            f"{what} {row.name}: Bambuddy's local preset {preset_id} is a {row.preset_type} preset"
        )
    if not row.setting:
        raise ProfileError(f"{what} {row.name}: Bambuddy holds no settings for it")
    try:
        return await asyncio.to_thread(profiles.flatten_over, _KINDS[slot], row.name, row.setting)
    except ProfileImageError:
        raise
    except ProfileError as error:
        version = await asyncio.to_thread(lambda: profiles.version)
        raise ProfileError(
            f"{what} {row.name} from Bambuddy over Bambu Studio {version}'s presets: {error}"
        ) from None


def _document(flat: Flat) -> bytes:
    return json.dumps(flat, indent=4, ensure_ascii=False).encode()


def _digest(document: bytes) -> str:
    return hashlib.sha256(document).hexdigest()


async def needs_bambuddy(plan: SlicePlan, profiles: Profiles) -> bool:
    """Whether building ``plan``'s presets reads Bambuddy: a ref the image's tree does
    not hold by its id that Bambuddy could resolve (by name, a process preset's values,
    or a local preset's document the kept copy lacks, #4645). A ref refused either way
    (``orca_cloud``) does not count."""
    refs: list[tuple[Slot, PresetRef]] = [
        ("printer", plan.printer_preset),
        ("process", plan.process_preset),
        *(("filament", ref) for ref in plan.filament_presets),
    ]
    for slot, ref in refs:
        if ref.source == "orca_cloud":
            continue
        if ref.source in ("standard", "cloud"):
            if await asyncio.to_thread(profiles.find, _KINDS[slot], ref.id) is None:
                return True
        elif await _held(profiles, ref) is None:
            return True
    return False


async def build_presets(
    plan: SlicePlan, profiles: Profiles, client: BambuddyClient | None
) -> PresetFiles:
    """The files that slice ``plan``: its presets flattened, its process overrides and
    plate type written over the process. ``client`` names refs and reads a preset only
    Bambuddy holds (a process preset's values, a local preset's document); without one,
    only the image's presets resolve."""
    names = _Names(client)
    machine, machine_held = await _resolve("printer", plan.printer_preset, profiles, client, names)
    process, process_held = await _resolve("process", plan.process_preset, profiles, client, names)
    filaments = [
        await _resolve("filament", ref, profiles, client, names) for ref in plan.filament_presets
    ]
    # #2799: a slot's filament settings go into its own file, which the CLI honours; the
    # same keys in the process file are ignored (the module's docstring).
    filaments = [
        (apply_overrides(filament, plan.filament_overrides.get(slot, {})), held)
        for slot, (filament, held) in enumerate(filaments, start=1)
    ]
    process = apply_overrides(process, plan.process_overrides)
    if plan.bed_type:
        process["curr_bed_type"] = plan.bed_type
    machine_document = _document(machine)
    process_document = _document(process)
    filament_documents = [_document(filament) for filament, _ in filaments]
    from_bambuddy: dict[str, str] = {}
    if machine_held:
        from_bambuddy["printer"] = _digest(machine_document)
    if process_held:
        from_bambuddy["process"] = _digest(process_document)
    for slot, ((_, held), document) in enumerate(
        zip(filaments, filament_documents, strict=True), start=1
    ):
        if held:
            from_bambuddy[f"filament_{slot}"] = _digest(document)
    return PresetFiles(
        machine=machine_document,
        process=process_document,
        filaments=filament_documents,
        from_bambuddy=from_bambuddy,
    )


#: How :func:`~scadbuddy.slicing.profiles.flatten` reads the image's presets, in every
#: native slice's key: raised when a change to it changes the files a plan slices with.
#: 2 applies ``include`` (#4714).
FLATTEN_REVISION = 2


def native_preset_key(
    plan: SlicePlan, plate_id: int, version: str, from_bambuddy: Mapping[str, str] | None = None
) -> str:
    """What makes two native slices the same: Bambuddy's preset key behind the image's
    Bambu Studio version, so a native slice never answers for a Bambuddy one, and a new
    slicer slices again (Ruling C7). ``from_bambuddy`` is
    :attr:`PresetFiles.from_bambuddy`: a file built from what Bambuddy holds is keyed by
    what it held, since the image's version does not say which profiles Bambuddy's
    slicer flattened a process preset's values from, nor what a local preset's document
    is, which an edit in Bambuddy changes under the same id (#4233). The process's digest
    goes first, where it went before the other slots were keyed, so a slice keyed then
    still answers.

    The slots' colours are in it too (#3962): the CLI takes them as ``--filament-colour``,
    which sets each slot's colour in the sliced file and decides whether slots of one
    preset keep the prime tower, so a recolour slices again rather than reuse a file
    that names the old colours. Keys of slices from before then never match, and those
    slices, without the tower, are not reused.

    A slot's filament overrides (#2799) follow, when it has any: they change that slot's
    file, and a plan without them keys as it did before.

    :data:`FLATTEN_REVISION` ends it (#4714): the image's version does not change when
    :func:`~scadbuddy.slicing.profiles.flatten` does, and a slice flattened before
    ``include`` was applied carries another machine's G-code, so it is never reused."""
    key = f"native@{version}/" + slice_request(plan, plate_id).preset_key
    held = from_bambuddy or {}
    slots = [
        "process",
        "printer",
        *(f"filament_{n}" for n in range(1, len(plan.filament_presets) + 1)),
    ]
    for slot in slots:
        if slot in held:
            key += f"/bambuddy-{slot}={held[slot]}"
    if plan.filament_colours:
        key += "/colours=" + ",".join(colour.upper() for colour in plan.filament_colours)
    for number, values in sorted(plan.filament_overrides.items()):
        if values:
            # Each value as JSON, as the process overrides are (#2804).
            key += f"/filament_{number}=" + ",".join(
                f"{k}={json.dumps(v)}" for k, v in sorted(values.items())
            )
    return key + f"/flatten={FLATTEN_REVISION}"
