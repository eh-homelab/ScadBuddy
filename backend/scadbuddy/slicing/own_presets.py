"""ScadBuddy's own filament presets as the native slicer reads them (#7176, plan
2026-11-23-scadbuddy-filament-presets.md Decisions 1 and 9).

A binding (a type's or a spool's preset row) names one by the ref ``sb-<id>`` in its
``slicer_filament``: text, as every other ref is, and never digits, so no reader takes it
for a Bambuddy local preset id. A catalogue offers each as a ``local`` choice with that
id. The store is `filament/sb_presets.py` ``FilamentPresetStore`` in the API, and
`crud/print.py` ``RemoteFilamentPresets`` in the print worker. This module imports
neither, so the resolver and the slicer read refs without the filament package.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

#: What a ScadBuddy preset's ref starts with.
PREFIX: Final = "sb-"
_REF = re.compile(r"sb-([1-9][0-9]{0,17})")


def sb_ref(preset_id: int) -> str:
    """The ref a binding names preset ``preset_id`` by."""
    return f"{PREFIX}{preset_id}"


def sb_id(ref: str | None) -> int | None:
    """The ScadBuddy preset id ``ref`` names, or ``None`` when it names none (the same
    form the binding tables' generated ``preset_id`` column reads)."""
    found = _REF.fullmatch(ref or "")
    return None if found is None else int(found.group(1))


@dataclass(frozen=True)
class OwnRow:
    """A ScadBuddy preset as a catalogue lists it, without its document. ``sha256`` is
    the current document's, which moves with each new version; ``bb_preset_id`` is the
    Bambuddy local preset that is this one (its mirror, or its origin), so a catalogue
    lists the two once."""

    id: int
    name: str
    sha256: str
    filament_type: str | None = None
    filament_colour: str | None = None
    compatible_printers: list[str] = field(default_factory=list)
    bb_preset_id: int | None = None


class OwnPresets(Protocol):
    """What a native run reads of ScadBuddy's filament presets."""

    async def rows(self) -> list[OwnRow]:
        """Every preset, without its document."""
        ...

    async def documents(self, ids: Collection[int]) -> dict[int, dict[str, Any]]:
        """The current documents of ``ids``, each without print-host credential keys; an
        id with no preset is left out."""
        ...


def facts(doc: dict[str, Any]) -> tuple[str | None, str | None, list[str]]:
    """A document's ``filament_type`` and ``default_filament_colour`` (the first of
    each) and its ``compatible_printers``: what a catalogue row shows."""

    def first(value: Any) -> str | None:
        if isinstance(value, list):
            value = value[0] if value else None
        return value if isinstance(value, str) and value else None

    printers = doc.get("compatible_printers")
    return (
        first(doc.get("filament_type")),
        first(doc.get("default_filament_colour")),
        [p for p in printers if isinstance(p, str)] if isinstance(printers, list) else [],
    )
