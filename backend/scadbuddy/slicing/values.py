"""A process preset's values from the image's BBL profiles (#3818 tier A Task 3): what
the dialog's override checks (filament profiles, supports, the prime tower) read on a
native run, in place of Bambuddy's ``GET /slicer/preset-values``.

The values are the preset flattened as the native slicer flattens it
(:meth:`~scadbuddy.slicing.profiles.Profiles.process`), in the shape Bambuddy's answer has
(a string, or a list of strings for a vector option: ``slicing/presets.py``
``apply_overrides``, measured 2026-10-18), so every caller reads either one the same way.
A preset the native catalogue does not offer is unresolved, never an error, as Bambuddy's
unresolved answers are (#717, #1899): a ``local`` or ``orca_cloud`` ref, a cloud user
preset, a base preset, or anything not a process.
"""

from __future__ import annotations

import asyncio

from scadbuddy.bambuddy.client import BambuddyClient, ReadPresetValues, read_once
from scadbuddy.bambuddy.models import NOT_IN_PROFILES, PresetRef, PresetValues
from scadbuddy.slicing.catalogue import catalogue_native_ref, native_catalogue
from scadbuddy.slicing.profiles import ProfileError, Profiles

__all__ = ["NOT_IN_PROFILES", "native_values", "values_for"]


def native_values(profiles: Profiles) -> ReadPresetValues:
    """A reader of process presets' values from ``profiles``, each preset flattened at
    most once by whoever shares it (as :func:`~scadbuddy.bambuddy.client.read_once`)."""
    read: dict[str, PresetValues] = {}

    async def values(ref: PresetRef) -> PresetValues:
        catalogue = await asyncio.to_thread(native_catalogue, profiles)
        native = catalogue_native_ref(catalogue, ref, "process")
        if native is None:
            return PresetValues(resolved=False, reason=NOT_IN_PROFILES)
        if native.id not in read:
            try:
                flat = await asyncio.to_thread(profiles.process, native.id)
            except ProfileError:
                return PresetValues(resolved=False, reason=NOT_IN_PROFILES)
            read[native.id] = PresetValues(resolved=True, values=flat)
        return read[native.id]

    return values


def values_for(client: BambuddyClient | None, *, native: Profiles | None) -> ReadPresetValues:
    """The reader a run's override checks share: the image's profiles when ``native`` is
    given (no Bambuddy call), else Bambuddy's, each preset read once."""
    if native is not None:
        return native_values(native)
    if client is None:
        raise ValueError("reading preset values needs a Bambuddy client or the native profiles")
    return read_once(client)
