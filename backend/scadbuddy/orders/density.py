"""A filament's density from Bambuddy's link to its slicer preset (#6136), for a
price-book record that has none.

A record's ``density_g_cm3`` is what the rough estimate weighs a volume with. A person
may leave it empty, and before #6136 the spool import never set it. Bambuddy holds the
link from a spool to the slicer preset it prints with, and the preset holds the
density. All of it is read at maziggy/bambuddy ``v1.3.2b1-daily.20261113``, the daily
build nearest to the ``daily`` digest prod runs (eh-homelab/clusters
``clusters/prod/bambuddy/kustomization.yaml``, which pins a digest, not a release):

- A spool's ``slicer_filament`` is its preset id (``GFL99`` in the model's own comment;
  ``backend/app/models/spool.py``), a Bambu ``filament_id`` or a numeric local preset id
  (``backend/app/services/slicer_filament_resolver.py``, the ``"38"`` example).
- Each spool's K-profiles (``backend/app/models/spool_k_profile.py``) carry the
  ``setting_id`` of the preset the printer calibrated it with; ``GET
  /inventory/spools`` answers them in ``k_profiles`` (``SpoolResponse``,
  ``backend/app/schemas/spool.py``).
- An AMS tray reports the ``tray_info_idx`` of what it holds, which is the preset's
  ``filament_id`` (``slicing/catalogue.py``).
- A local preset has ``filament_density`` (``backend/app/models/local_preset.py``).
- Bambuddy's filament catalogue has ``Filament.density`` in g/cm³
  (``backend/app/models/filament.py``), served by ``GET /filament-catalog/``
  (``backend/app/api/routes/filaments.py``).

A spool carries no density of its own (``SpoolBase`` has no such field). So the chain
is, for one filament of the inventory (:func:`resolve`):

1. Its preset id: ``slicer_filament``, then each K-profile's ``setting_id``, then the
   ``tray_info_idx`` of the AMS tray a spool of it is loaded in.
2. That preset's ``filament_density``: Bambuddy's local preset when the id names one,
   else the Bambu Studio vendor preset with that ``filament_id`` or ``setting_id`` in
   the image's tree (``slicing/profiles.py``, Bambu Studio v02.08.02.61).
3. Only then the catalogue's ``density`` for the same material, brand and colour,
   from entries whose ``name`` holds each word of the spool's subtype.

No preset is found by a subtype's name (the subtype only narrows the catalogue's
entries in step 3): Bambu's "Dual Color" and "Tri Color" PLA have no
preset of that name; the sliced files of Bambuddy archives 40 and 41 (read 2026-11-13)
show they print as ``GFA05``, Bambu PLA Silk, which is what the chain reads from their
spools' preset ids. When every step is empty there is no source, and the record keeps
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

from scadbuddy.orders.integrations import SpoolGroup
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


def density_text(value: str | float | None) -> str | None:
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
    ids: list[tuple[str, str]] = []
    for preset_id, how in [
        (group.slicer_filament, "spool preset"),
        *((setting_id, "K-profile preset") for setting_id in group.k_profile_settings),
        (group.tray_info_idx, "AMS tray preset"),
    ]:
        if preset_id and preset_id not in [i for i, _ in ids]:
            ids.append((preset_id, how))
    for preset_id, how in ids:
        local = group.local_preset if preset_id == group.slicer_filament else None
        if local is not None:
            return ProfileDensity(
                density_g_cm3=local.density_g_cm3,
                profile=f"Bambuddy local preset {local.name} ({how} {preset_id})",
            )
        found = None if table is None else table.for_preset(preset_id)
        if found is not None:
            return ProfileDensity(
                density_g_cm3=found.density_g_cm3, profile=f"{found.profile} ({how} {preset_id})"
            )
    if group.catalogue_density is not None:
        return ProfileDensity(
            density_g_cm3=group.catalogue_density, profile="Bambuddy filament catalogue"
        )
    return None


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
            " Bambuddy (POST /orders/price-book/fill-densities) or set it"
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
