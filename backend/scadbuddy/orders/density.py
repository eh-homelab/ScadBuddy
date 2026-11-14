"""A filament's density from the slicer's own profiles (#6136), for a price-book record
that has none.

A record's ``density_g_cm3`` is what the rough estimate weighs a volume with. A person
may leave it empty, and before #6136 the spool import never set it. Bambuddy's spools
carry no density: ``SpoolBase`` and ``SpoolResponse`` in Bambuddy v1.3.1 have no such
field
(https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/schemas/spool.py). They
carry ``slicer_filament``, the Bambu Studio ``filament_id`` of the spool's preset
(``GFG00`` is ``Bambu PETG Basic``'s, ``tests/bambuddy/recordings/inventory-spools.json``;
the same ids as an AMS tray's ``tray_info_idx``, ``slicing/catalogue.py``). The image's
Bambu Studio profiles (``slicing/profiles.py``) carry ``filament_density`` for each
preset. So this module reads the density from there, in this order, and from nothing
else:

1. The spool's ``slicer_filament``: the presets with that ``filament_id``.
2. The record's names: the preset family (a preset's name before `` @``) whose name,
   less its first word, is the record's material and subtype, of the vendor that is the
   record's brand. ``Bambu PLA Basic`` is PLA, Basic, ``Bambu Lab``. A subtype in
   :data:`SUBTYPE_FAMILIES` reads as the family it names there.
3. Only for a brand with no vendor profiles in the tree: the ``Generic`` family of the
   material and subtype (``Generic PLA Silk``), else of the material alone
   (``Generic PLA`` for an Inland PLA Plus, ``Generic TPU`` for an Insignia TPU 95A).
   Bambu Studio offers the ``Generic`` presets for a filament of another maker, and the
   book shows the preset's name, so a person sees that the density is the generic one.

In a family, the ``@base`` preset's density is the answer, else the one density every
preset of the family shares. A family whose presets disagree, a vendor's subtype that
names no family of that vendor (and is not in :data:`SUBTYPE_FAMILIES`), and a material
with no ``Generic`` family give no density: never a guess.

What the tree holds, measured 2026-11-13 on Bambu Studio v02.08.02.61's
``resources/profiles/BBL`` (the tag the image pins; the zip of
https://github.com/bambulab/BambuStudio/tree/v02.08.02.61, every listed filament preset
read): 2050 filament presets; ``Bambu PLA Basic @base`` 1.26, ``Bambu PLA Translucent
@base`` 1.22, ``Bambu PETG Basic @base`` 1.25, ``Bambu PETG Translucent @base`` 1.25,
``Generic PLA @base`` 1.24. ``fdm_filament_common`` holds ``0``, which is no density.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from scadbuddy.orders.models import FallbackDensity, Filament
from scadbuddy.slicing.profiles import Profiles

#: ``filaments.density_g_cm3``'s ``numeric(6,3)``.
_PLACES = Decimal("0.001")
_MAX = Decimal(1000)
_GENERIC = "GENERIC"

#: A vendor's subtype whose name is not its preset's: (brand, material, subtype) to the
#: family's name less its first word. Bambu's "Dual Color" and "Tri Color" PLA are its
#: "PLA Silk Multi-Color", which Bambu Studio slices with ``Bambu PLA Silk`` (``GFA05``,
#: 1.32): the sliced files of Bambuddy archives 40 and 41 (a Tri Color and a Dual Color
#: print, read 2026-11-13) name that preset for those slots. v02.08.02.61 has no preset
#: named Dual Color or Tri Color.
SUBTYPE_FAMILIES: dict[tuple[str, str, str], str] = {
    ("BAMBU LAB", "PLA", "DUAL COLOR"): "PLA SILK",
    ("BAMBU LAB", "PLA", "TRI COLOR"): "PLA SILK",
}


@dataclass(frozen=True)
class ProfileDensity:
    """A density the slicer's profiles give, and the preset it came from."""

    density_g_cm3: str
    profile: str


@dataclass(frozen=True)
class _Preset:
    name: str
    #: The name before `` @``, upper-cased, as words.
    family: tuple[str, ...]
    base: bool
    filament_id: str | None
    vendor: str | None
    density: str | None


def _words(text: str | None) -> tuple[str, ...]:
    return tuple((text or "").upper().split())


def _first(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, str) else None


def _density(value: str | None) -> str | None:
    """A ``filament_density`` as a record's density, or ``None`` for one that is not."""
    if value is None:
        return None
    try:
        number = Decimal(value).quantize(_PLACES)
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
    """Every filament preset of a tree with its density, vendor and ``filament_id``."""

    def __init__(self, presets: list[_Preset]) -> None:
        self._presets = presets
        self._vendors = {p.vendor for p in presets if p.vendor}

    @classmethod
    def build(cls, profiles: Profiles) -> DensityTable:
        """Read once per tree: blocking file I/O, run it in a thread."""
        docs = profiles.documents("filament")
        presets = [
            _Preset(
                name=name,
                family=_words(name.split(" @", 1)[0]),
                base=name.endswith("@base"),
                filament_id=_resolved(name, "filament_id", docs),
                vendor=(_resolved(name, "filament_vendor", docs) or "").upper() or None,
                density=_density(_resolved(name, "filament_density", docs)),
            )
            for name in sorted(docs)
        ]
        return cls(presets)

    def lookup(
        self,
        material: str,
        subtype: str | None,
        brand: str | None,
        slicer_filament: str | None = None,
    ) -> ProfileDensity | None:
        """The density for a filament, in the module's order; ``None`` when no source
        gives exactly one."""
        if slicer_filament:
            found = self._pick([p for p in self._presets if p.filament_id == slicer_filament])
            if found is not None:
                return found
        maker = " ".join(_words(brand))
        alias = SUBTYPE_FAMILIES.get((maker, " ".join(_words(material)), " ".join(_words(subtype))))
        want = sorted(alias.split() if alias else _words(material) + _words(subtype))
        if maker and maker in self._vendors:
            return self._pick(
                [p for p in self._presets if p.vendor == maker and sorted(p.family[1:]) == want]
            )
        return self._generic(want) or self._generic(sorted(_words(material)))

    def _generic(self, want: list[str]) -> ProfileDensity | None:
        return self._pick(
            [
                p
                for p in self._presets
                if p.vendor == _GENERIC
                and p.family[:1] == (_GENERIC,)
                and sorted(p.family[1:]) == want
            ]
        )

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


class UsedDensity(BaseModel):
    """The density an estimate weighed a filament with (#6136)."""

    filament_id: str
    density_g_cm3: str
    #: ``book``: the record's own. ``profile``: the record has none, so the slicer
    #: preset's for its material, subtype and brand.
    source: Literal["book", "profile"]
    #: The preset it came from: the fallback's, or the one a filled record names
    #: (``density_profile``); ``None`` for a density a person set.
    profile: str | None = None


def fallback(record: Filament, table: DensityTable | None) -> FallbackDensity | None:
    """What an estimate weighs ``record`` with when it has no density: the preset
    density for its names; ``None`` for one with a density, or no source."""
    if record.density_g_cm3 is not None or table is None:
        return None
    found = table.lookup(record.material, record.subtype, record.brand)
    if found is None:
        return None
    return FallbackDensity(density_g_cm3=found.density_g_cm3, profile=found.profile)


def used_density(record: Filament, table: DensityTable | None) -> UsedDensity:
    """The record's density, else its :func:`fallback`. ``ValueError`` when neither
    gives one."""
    if record.density_g_cm3 is not None:
        return UsedDensity(
            filament_id=record.id,
            density_g_cm3=record.density_g_cm3,
            source="book",
            profile=record.density_profile,
        )
    found = fallback(record, table)
    if found is None:
        raise ValueError(
            f"filament {record.id} has no density, and no slicer profile matches its"
            " material, subtype and brand, so there is no estimate"
        )
    return UsedDensity(
        filament_id=record.id,
        density_g_cm3=found.density_g_cm3,
        source="profile",
        profile=found.profile,
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
