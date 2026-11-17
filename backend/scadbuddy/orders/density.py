"""A filament's density from its spools' slicer preset (#6136), for a price-book record
that has none.

A record's ``density_g_cm3`` is what the rough estimate weighs a volume with. A person
may leave it empty, and before #6136 the spool import never set it. The import reads
ScadBuddy's own spools (#6262 Task 9, `orders/spool_import.py` `spool_groups`), and each
SB spool has effective presets (``filament/presets.py``): the Bambuddy pull carries a
linked Bambuddy spool's ``slicer_filament`` into them (#6262 Task 7 ruling R1), which
is a Bambu ``filament_id`` (``GFA00``) or a numeric Bambuddy local preset id (the
``"38"`` example of ``backend/app/services/slicer_filament_resolver.py`` at maziggy/bambuddy
``v1.3.2b1-daily.20261113``). A Bambuddy local preset has ``filament_density``
(``backend/app/models/local_preset.py``, same tag).

So the chain is, for one filament of the inventory (:func:`resolve`, Task 9 review
ruling):

1. The newest spool's own effective density (the spool's, else its type's, subtype's
   or material's, `filament/types.py` `effective`) that is set.
2. Its preset id: the newest spool's effective preset that names one. That preset's
   ``filament_density``: Bambuddy's local preset when the id names one (read only while
   Bambuddy is linked), else the Bambu Studio vendor preset with that ``filament_id`` or
   ``setting_id`` in the image's tree (``slicing/profiles.py``, Bambu Studio
   v02.08.02.61).
3. The preset id (``tray_info_idx``) of the tray a registry printer's latest report
   holds the newest held spool in, read the same way.

The Bambuddy K-profile and filament-catalogue steps went with Bambuddy's inventory read
(#6262 Task 9): a record they alone filled keeps its density (one is never overwritten),
and a new record they alone could have filled has none until a person types it. No
preset is found by a subtype's name: Bambu's "Dual Color" and "Tri Color" PLA have no
preset of that name; the sliced files of Bambuddy archives 40 and 41 (read 2026-11-13)
show they print as ``GFA05``, Bambu PLA Silk, the preset id their spools and the trays
holding them carry. When every step is empty there is no source, and the record keeps
no density.

In the vendor tree, the presets that share a ``filament_id`` (one per printer) give
their ``@base`` preset's density, else the one density they all share; presets that
disagree give none. Measured 2026-11-13 on v02.08.02.61's ``resources/profiles/BBL``
(https://github.com/bambulab/BambuStudio/tree/v02.08.02.61, every listed filament
preset read): ``GFA00`` Bambu PLA Basic 1.26, ``GFA05`` Bambu PLA Silk 1.32, ``GFG00``
Bambu PETG Basic 1.25; ``fdm_filament_common`` holds ``0``, which is no density.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from scadbuddy.orders.integrations import LocalDensity, SpoolGroup
from scadbuddy.orders.models import Filament
from scadbuddy.slicing.profiles import Profiles

#: ``filaments.density_g_cm3``'s ``numeric(6,3)``.
_PLACES = Decimal("0.001")
_MAX = Decimal(1000)


@dataclass(frozen=True)
class ProfileDensity:
    """A density, and where it came from, as the book shows it."""

    density_g_cm3: str
    profile: str


@dataclass(frozen=True)
class _Preset:
    name: str
    base: bool
    filament_id: str | None
    setting_id: str | None
    density: str | None


def _first(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, str) else None


def density_text(value: str | float | Decimal | None) -> str | None:
    """A density as a record holds it (``numeric(6,3)``, above 0), or ``None`` for one
    that is not."""
    if value is None:
        return None
    try:
        number = Decimal(str(value)).quantize(_PLACES)
    except (InvalidOperation, ValueError):
        return None
    if not Decimal(0) < number < _MAX:
        return None
    return format(number.normalize(), "f")


def _resolved(name: str, key: str, docs: dict[str, dict[str, Any]]) -> str | None:
    """``key`` of preset ``name`` as :func:`~scadbuddy.slicing.profiles.flatten` would
    give it (the child's own value, then its ``include`` presets last first, then its
    parent's), for the first value only, without copying the preset."""
    seen: set[str] = set()
    current: str | None = name
    while current and current not in seen:
        seen.add(current)
        doc = docs.get(current)
        if doc is None:
            return None
        if key in doc:
            return _first(doc[key])
        included = doc.get("include") or []
        if isinstance(included, str):
            included = [included]
        for other in reversed(included if isinstance(included, list) else []):
            options = docs.get(other) if isinstance(other, str) else None
            if options is not None and key in options:
                return _first(options[key])
        parent = doc.get("inherits")
        current = parent if isinstance(parent, str) else None
    return None


class DensityTable:
    """Every filament preset of the image's tree with its density, by ``filament_id``
    and ``setting_id``."""

    def __init__(self, presets: list[_Preset]) -> None:
        self._by_filament_id: dict[str, list[_Preset]] = {}
        self._by_setting_id: dict[str, _Preset] = {}
        for preset in presets:
            if preset.filament_id:
                self._by_filament_id.setdefault(preset.filament_id, []).append(preset)
            if preset.setting_id:
                self._by_setting_id[preset.setting_id] = preset

    @classmethod
    def build(cls, profiles: Profiles) -> DensityTable:
        """Read once per tree: blocking file I/O, run it in a thread."""
        docs = profiles.documents("filament")
        return cls(
            [
                _Preset(
                    name=name,
                    base=name.endswith("@base"),
                    filament_id=_resolved(name, "filament_id", docs),
                    setting_id=_first(docs[name].get("setting_id")),
                    density=density_text(_resolved(name, "filament_density", docs)),
                )
                for name in sorted(docs)
            ]
        )

    def for_preset(self, preset_id: str) -> ProfileDensity | None:
        """The density of the vendor preset ``preset_id`` names: a ``filament_id`` (the
        module's rule for its presets), else a ``setting_id``."""
        presets = self._by_filament_id.get(preset_id)
        if presets:
            return self._pick(presets)
        preset = self._by_setting_id.get(preset_id)
        if preset is None or preset.density is None:
            return None
        return ProfileDensity(density_g_cm3=preset.density, profile=preset.name)

    @staticmethod
    def _pick(presets: list[_Preset]) -> ProfileDensity | None:
        """The ``@base`` presets' one density, else the one all of them share."""
        for group in ([p for p in presets if p.base], presets):
            densities = {p.density for p in group if p.density is not None}
            if len(densities) == 1:
                density = densities.pop()
                name = next(p.name for p in group if p.density == density)
                return ProfileDensity(density_g_cm3=density, profile=name)
            if densities:
                return None
        return None


def resolve(group: SpoolGroup, table: DensityTable | None) -> ProfileDensity | None:
    """The density of one filament of the inventory, by the module's chain; ``None``
    when no step gives one."""
    if group.spool_density is not None:
        return ProfileDensity(
            density_g_cm3=group.spool_density.density_g_cm3,
            profile=f"ScadBuddy spool #{group.spool_density.number}",
        )
    return _preset_density(
        group.slicer_filament, group.local_preset, table, "spool preset"
    ) or _preset_density(group.tray_filament, group.tray_local_preset, table, "tray preset")


def _preset_density(
    preset_id: str | None, local: LocalDensity | None, table: DensityTable | None, where: str
) -> ProfileDensity | None:
    """The density preset ``preset_id`` gives: its Bambuddy local preset ``local``, else
    the vendor preset in ``table``; ``where`` names the id's source in the profile."""
    if not preset_id:
        return None
    if local is not None:
        return ProfileDensity(
            density_g_cm3=local.density_g_cm3,
            profile=f"Bambuddy local preset {local.name} ({where} {preset_id})",
        )
    found = None if table is None else table.for_preset(preset_id)
    if found is None:
        return None
    return ProfileDensity(
        density_g_cm3=found.density_g_cm3, profile=f"{found.profile} ({where} {preset_id})"
    )


class UsedDensity(BaseModel):
    """The density an estimate weighed a filament with (#6136)."""

    filament_id: str
    density_g_cm3: str
    #: Where the record's density came from (``density_profile``); ``None`` for one a
    #: person set.
    profile: str | None = None


def used_density(record: Filament) -> UsedDensity:
    """The record's density. ``ValueError`` for one with none."""
    if record.density_g_cm3 is None:
        raise ValueError(
            f"filament {record.id} has no density, so there is no estimate: fill it from"
            " its spools (POST /orders/price-book/fill-densities) or set it"
        )
    return UsedDensity(
        filament_id=record.id, density_g_cm3=record.density_g_cm3, profile=record.density_profile
    )


#: Built tables, by the tree's root and version: the tree is fixed in an image.
_built: dict[tuple[Path, str], DensityTable] = {}
_lock = threading.Lock()


def density_table(root: Path) -> DensityTable | None:
    """The table of the tree under ``root``, built once per process; ``None`` without a
    tree (a dev run outside the image). Blocking: call it in a thread."""
    profiles = Profiles.installed(root)
    if profiles is None:
        return None
    key = (root, profiles.version)
    with _lock:
        table = _built.get(key)
        if table is None:
            table = _built[key] = DensityTable.build(profiles)
    return table
