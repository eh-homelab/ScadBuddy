"""A preset of its own name for every filament slot of a Bambuddy slice (#3962).

Bambu Studio's CLI (v02.08.02.61, ``BambuStudio.cpp`` L2625) turns the prime tower off
when every ``--load-filaments`` profile has one name: "map all the filaments to the same
one". Bambuddy's sidecar builds that argv itself, from each ``filament_presets`` ref's
JSON, and patches only ``filament_colour`` into them, so a multi-colour slice whose slots
all ref ``Bambu PLA Basic @BBL H2C`` comes out with ``enable_prime_tower`` ``"0"``
whatever was asked. The native slicer passes ``--filament-colour`` instead (#4422); the
Bambuddy path cannot, so here every repeat of a preset after its first refs a Bambuddy
local preset that is the same preset under another name. Measured on the CLI (#3962's
comments, ``scadbuddy-slicer:dev``): slots 2 and 3 renamed keep ``enable_prime_tower``
``"1"`` and 250 lines of tower G-code.

What each one holds, by the shared preset's source:

- ``standard``, and a ``cloud`` ref whose name is a ``standard`` preset's: a stub,
  ``{name, inherits: <the preset>, from: "system", type: "filament"}``, the stub
  Bambuddy itself sends for a ``standard`` ref (``preset_resolver.py``
  ``_resolve_standard``) under the new name. The sidecar flattens it over its bundled
  tree (``profile-resolver.js`` ``resolveProfile``, child over parent); measured
  2026-10-24 against ``bambu-studio-api``'s resolver and bundle, the stub for
  ``Bambu PLA Basic @BBL H2C`` flattens to the same 143 keys as the shared ref's, only
  ``name`` differing (``filament_id`` ``GFA00`` kept).
- ``local``: a copy of the preset's own document (``GET /local-presets/{id}``) under the
  new name, which Bambuddy hands the sidecar as it handed the original.
- Anything else (a ``cloud`` user preset, ``orca_cloud``): none. Its content cannot be
  read from here, and a stub naming a preset the sidecar does not bundle would slice
  from the CLI's defaults. The slot keeps the shared ref, and the post-slice guard
  (``prime_tower.tower_dropped``) says if the tower went.

**Naming.** ``<preset name> (ScadBuddy slot <n>)``, ``n`` the 1-based slot: a name no
person's preset carries, which says whose it is. :func:`is_slot_preset` is how the
catalogue leaves them out of the dialog's pickers and the resolver's matches.

**Reuse, and why there is no cleanup.** Before creating one, Bambuddy's local presets
are listed and a preset of that exact name whose document is the one wanted is used
instead, so repeated slices never make a second copy. That bounds what is created: one
per shared preset and slot number for a stub, plus one per version of a ``local``
preset that is edited after a slice copied it (a copy whose document no longer matches
is left, never deleted, since a slice planned before the edit may still name it). Two
runs making the same one at once may each make it; the newest is used after. So no job
removes them.

**Failure.** Listing, reading or creating may fail (Bambuddy's auth on refuses every
API key ``SETTINGS_UPDATE``, which the create needs): the slot keeps the shared ref, the
slice goes ahead as it did before, and the guard warns. Nothing here fails a print.

The plan carries the new refs, so the slice's ``preset_key`` names them: a slice made
before this, which lost its tower, is never reused for the same choices.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from scadbuddy.bambuddy.models import LocalPreset, LocalPresetCatalogue, PresetRef

if TYPE_CHECKING:
    # Types only: ``catalogue.py`` imports this module for :func:`is_slot_preset`.
    from scadbuddy.bambuddy.catalogue import Catalogue
    from scadbuddy.bambuddy.client import BambuddyClient
    from scadbuddy.bambuddy.dispatch import SlicePlan

logger = logging.getLogger(__name__)

_SLOT_SUFFIX = re.compile(r" \(ScadBuddy slot \d+\)\Z")


def slot_preset_name(base: str, slot: int) -> str:
    """The name of ``base``'s own preset for ``slot`` (1-based)."""
    return f"{base} (ScadBuddy slot {slot})"


def is_slot_preset(name: str) -> bool:
    """Whether ``name`` is one :func:`slot_preset_name` made."""
    return _SLOT_SUFFIX.search(name) is not None


class SlotPresets:
    """Each repeated slot's own preset, for every plate of one run: Bambuddy's local
    presets are listed once, and only when a plate repeats a preset."""

    def __init__(self, client: BambuddyClient, catalogue: Catalogue) -> None:
        self._client = client
        self._names = catalogue.names()
        self._standard = {row.ref.id for row in catalogue.filament if row.ref.source == "standard"}
        self._listed: LocalPresetCatalogue | None = None
        self._made: dict[str, PresetRef] = {}

    def _name(self, ref: PresetRef) -> str:
        """What the CLI will see as the preset's name: Bambuddy's stub for a
        ``standard`` ref names it by its id."""
        if ref.source == "standard":
            return ref.id
        return self._names.get((ref.source, ref.id), f"{ref.source}:{ref.id}")

    async def planned(self, plan: SlicePlan) -> tuple[SlicePlan, bool]:
        """``plan`` with every repeat of a preset name after its first on its slot's own
        preset (``plan`` itself when nothing repeats or nothing could be made), and
        whether two of its slots may still reach the CLI under one name: a repeat whose
        own preset could not be made, or a ref the catalogue does not name, whose name
        cannot be compared. Only then can the CLI have dropped the tower, so only then
        is the slice read for it (``prime_tower.tower_dropped``, #4649, #4655 review).

        A catalogue that could not be read, or is empty, names no ``cloud`` or ``local``
        ref, so every multi-slot plate with one is read: on purpose, since a guard that
        cannot compare names reads rather than stays silent (#4694 review)."""
        seen: set[str] = set()
        refs: list[PresetRef] = []
        shared = False
        for slot, ref in enumerate(plan.filament_presets, start=1):
            name = self._name(ref)
            unnamed = ref.source != "standard" and (ref.source, ref.id) not in self._names
            shared = shared or unnamed
            if name not in seen:
                seen.add(name)
                refs.append(ref)
                continue
            own = await self._own(ref, name, slot)
            shared = shared or own is None
            refs.append(own or ref)
        shared = shared and len(refs) > 1
        if refs == plan.filament_presets:
            return plan, shared
        return plan.model_copy(update={"filament_presets": refs}), shared

    async def _own(self, ref: PresetRef, base: str, slot: int) -> PresetRef | None:
        name = slot_preset_name(base, slot)
        if name in self._made:
            return self._made[name]
        try:
            setting = await self._setting(ref, base, name)
            if setting is None:
                return None
            made = (
                await self._reused(name, setting)
                or (await self._client.create_local_preset(name, "filament", setting)).ref()
            )
        except Exception as error:
            # Never the print's failure: the slot keeps the shared preset (module doc).
            logger.warning(
                "no preset of its own for a filament slot; it keeps the shared one",
                extra={"slot": slot, "source": ref.source, "error": type(error).__name__},
            )
            return None
        self._made[name] = made
        return made

    async def _setting(self, ref: PresetRef, base: str, name: str) -> dict[str, Any] | None:
        """The document ``name`` holds, or ``None`` for a ref with none to give."""
        if ref.source in ("standard", "cloud") and base in self._standard:
            return {"name": name, "inherits": base, "from": "system", "type": "filament"}
        if ref.source == "local":
            row = await self._client.local_preset(int(ref.id))
            if row.preset_type != "filament" or not row.setting:
                return None
            return {**row.setting, "name": name}
        return None

    async def _reused(self, name: str, setting: Mapping[str, Any]) -> PresetRef | None:
        """The newest local filament preset named ``name`` that holds ``setting``."""
        if self._listed is None:
            self._listed = await self._client.local_presets()
        rows: list[LocalPreset] = sorted(
            (row for row in self._listed.filament if row.name == name),
            key=lambda row: row.id,
            reverse=True,
        )
        for row in rows:
            if (await self._client.local_preset(row.id)).setting == setting:
                return row.ref()
        return None
