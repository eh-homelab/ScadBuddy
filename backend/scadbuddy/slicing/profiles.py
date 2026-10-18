"""Bambu Studio's BBL vendor profiles, flattened into the files its CLI loads (#3822,
spec 2026-10-16 §5).

The image copies them from the slicer's AppImage at build time (the Dockerfile's
``slicer-dist`` stage) into ``settings.slicer_profiles_dir``, laid out as Bambu Studio's
own ``resources/profiles/`` is: ``BBL.json`` (the vendor file, which lists every preset
by name and ``sub_path``) beside ``BBL/{machine,process,filament}/``, plus ``VERSION``,
the image's ``BAMBU_STUDIO_VERSION``.

What Bambuddy's preset ids are, measured 2026-10-18 by comparing every ``standard`` and
``cloud`` row of ``tests/bambuddy/recordings/slicer-presets.json`` and
``slicer-presets-h2c.json`` (249 rows a tier) with the v02.08.02.61 image's
``resources/profiles/BBL``: a ``cloud`` id is the preset's ``setting_id`` (``GM041`` is
``Bambu Lab H2C 0.4 nozzle``'s, ``GFSB00_22`` ``Bambu ABS @BBL H2C``'s), every one of
them found; a ``standard`` id is the preset's ``name``, none of them a ``setting_id``.
So :meth:`Profiles.machine` and its siblings look an id up by ``setting_id``, then by
``name``, and both tiers resolve without asking Bambuddy.

How ``inherits`` is flattened follows ``PresetBundle::load_vendor_configs_from_json`` in
v02.08.02.61
(https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/PresetBundle.cpp,
the ``parse_subfile`` lambda):

- A preset's parent is found by ``name`` among the presets of its own kind loaded so far
  (``config_maps``), never by ``setting_id``; one missing is "Can not find inherits" and
  the preset is not loaded. :func:`flatten` refuses it, and a loop, naming the preset.
- The child starts from its parent's whole config and applies its own keys over it
  (``config = *default_config; ... config.apply(config_src)``): each option replaces
  the parent's as one value, so a child's list is never merged into a longer parent
  list position by position (``test_a_childs_list_replaces_the_parents_whole``).
- ``includes`` names presets whose non-default options are applied between the parent
  and the child. Telling which are non-default needs Bambu Studio's built-in defaults,
  which this side does not have, and no BBL preset in v02.08.02.61 uses it, so a preset
  naming it is refused rather than flattened wrong.
- A root (no ``inherits``) starts from Bambu Studio's built-in defaults; here it starts
  empty, and the CLI fills what a file leaves out from the same defaults when it loads
  ``--load-settings`` and ``--load-filaments``.
- ``filament_id`` and ``description`` are inherited from the parent when the child has
  none: the same as any other key here.
- ``Preset::normalize`` (Preset.cpp) then resizes each per-filament list of a preset
  that has ``filament_diameter`` to that list's length (:data:`_PER_FILAMENT`). In
  v02.08.02.61 that cuts 14 lists of BBL filament presets (``Fiberon PA12-CF @BBL
  H2D``'s ``nozzle_temperature_range_low`` has two values for one filament) and pads
  none, measured by flattening every preset of the image's tree on 2026-10-18; a short
  one would be padded with a built-in default, so it is refused. The rest of
  ``normalize`` is SLA's, or repairs ``nil`` values no BBL preset has.

Values are kept as Bambu Studio's JSON stores them, a string or a list of strings; any
other value is refused as a preset the native slicer cannot read.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

#: One flattened preset: every option, ``inherits`` gone.
Flat = dict[str, str | list[str]]
Kind = Literal["machine", "process", "filament"]

#: The vendor whose tree the image ships.
VENDOR = "BBL"
#: Where ``BBL.json`` lists each kind's presets.
_LISTS: dict[Kind, str] = {
    "machine": "machine_list",
    "process": "process_list",
    "filament": "filament_list",
}

#: The per-filament options ``Preset::normalize`` resizes to ``filament_diameter``'s
#: length: ``s_Preset_filament_options`` (Preset.cpp) less ``compatible_prints``,
#: ``compatible_printers``, ``filament_options_with_variant`` (PrintConfig.cpp) and
#: ``filament_dev_options``, plus ``filament_settings_id``, as of v02.08.02.61.
_PER_FILAMENT: frozenset[str] = frozenset(
    {
        "activate_air_filtration",
        "additional_cooling_fan_speed",
        "additional_fan_full_speed_layer",
        "bed_type",
        "chamber_temperatures",
        "circle_compensation_speed",
        "close_additional_fan_first_x_layers",
        "close_fan_the_first_x_layers",
        "compatible_printers_condition",
        "compatible_prints_condition",
        "complete_print_exhaust_fan_speed",
        "cool_plate_temp",
        "cool_plate_temp_initial_layer",
        "cooling_perimeter_transition_distance",
        "cooling_slowdown_logic",
        "counter_coef_1",
        "counter_coef_2",
        "counter_coef_3",
        "counter_limit_max",
        "counter_limit_min",
        "default_filament_colour",
        "diameter_limit",
        "during_print_exhaust_fan_speed",
        "enable_overhang_bridge_fan",
        "enable_pressure_advance",
        "eng_plate_temp",
        "eng_plate_temp_initial_layer",
        "fan_cooling_layer_time",
        "fan_max_speed",
        "fan_min_speed",
        "filament_adhesiveness_category",
        "filament_change_length",
        "filament_change_length_nc",
        "filament_colour",
        "filament_cost",
        "filament_density",
        "filament_diameter",
        "filament_end_gcode",
        "filament_extruder_compatibility",
        "filament_is_support",
        "filament_metal_stickiness",
        "filament_minimal_purge_on_wipe_tower",
        "filament_notes",
        "filament_prime_volume",
        "filament_prime_volume_nc",
        "filament_printable",
        "filament_scarf_gap",
        "filament_scarf_height",
        "filament_scarf_length",
        "filament_scarf_seam_type",
        "filament_settings_id",
        "filament_shrink",
        "filament_soluble",
        "filament_start_gcode",
        "filament_tower_interface_pre_extrusion_dist",
        "filament_tower_interface_pre_extrusion_length",
        "filament_tower_interface_print_temp",
        "filament_tower_interface_purge_volume",
        "filament_tower_ironing_area",
        "filament_type",
        "filament_velocity_adaptation_factor",
        "filament_vendor",
        "first_x_layer_fan_speed",
        "first_x_layer_part_fan_speed",
        "full_fan_speed_layer",
        "hole_coef_1",
        "hole_coef_2",
        "hole_coef_3",
        "hole_limit_max",
        "hole_limit_min",
        "hot_plate_temp",
        "hot_plate_temp_initial_layer",
        "impact_strength_z",
        "inherits",
        "ironing_fan_speed",
        "no_slow_down_for_cooling_on_outwalls",
        "nozzle_temperature_range_high",
        "nozzle_temperature_range_low",
        "overhang_fan_speed",
        "overhang_fan_threshold",
        "overhang_threshold_participating_cooling",
        "pre_start_fan_time",
        "pressure_advance",
        "reduce_fan_stop_start_freq",
        "slow_down_for_layer_cooling",
        "slow_down_layer_time",
        "supertack_plate_temp",
        "supertack_plate_temp_initial_layer",
        "temperature_vitrification",
        "textured_plate_temp",
        "textured_plate_temp_initial_layer",
    }
)


class ProfileError(ValueError):
    """A preset the native slicer cannot read: missing, with a missing parent, in an
    ``inherits`` loop, or one only Bambuddy holds. The message names it."""


def flatten(name: str, load: Callable[[str], dict[str, Any] | None]) -> Flat:
    """Preset ``name`` with its ``inherits`` chain applied, child over parent, as Bambu
    Studio loads it (the module's docstring); ``load`` answers a preset's own JSON by
    name, or ``None`` for none."""
    chain: list[tuple[str, dict[str, Any]]] = []
    current = name
    while True:
        seen = [n for n, _ in chain]
        if current in seen:
            raise ProfileError(f"preset {name} inherits in a loop: {' -> '.join([*seen, current])}")
        doc = load(current)
        if doc is None:
            if not chain:
                raise ProfileError(f"no such preset {name}")
            raise ProfileError(f"preset {seen[-1]} inherits {current}: no such preset")
        if "includes" in doc:
            raise ProfileError(
                f"the native slicer cannot read preset {current}: it names includes"
                f" {doc['includes']!r}, which only Bambu Studio's defaults can apply"
            )
        chain.append((current, doc))
        parent = doc.get("inherits")
        if not parent:
            break
        current = parent
    flat: Flat = {}
    for preset, doc in reversed(chain):
        for key, value in doc.items():
            if key == "inherits":
                continue
            if isinstance(value, str):
                flat[key] = value
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                flat[key] = list(value)
            else:
                raise ProfileError(
                    f"the native slicer cannot read preset {preset}: {key} is a"
                    f" {type(value).__name__}, not text"
                )
    _normalize(name, flat)
    return flat


def _normalize(name: str, flat: Flat) -> None:
    """``Preset::normalize``: each per-filament list cut to the preset's filament count.
    Padding a short one takes Bambu Studio's built-in default, which this side does not
    have, so a short one is refused."""
    diameters = flat.get("filament_diameter")
    if not isinstance(diameters, list):
        return
    for key in sorted(_PER_FILAMENT):
        value = flat.get(key)
        if not isinstance(value, list):
            continue
        if len(value) < len(diameters):
            raise ProfileError(
                f"the native slicer cannot read preset {name}: {key} has {len(value)}"
                f" values for {len(diameters)} filaments"
            )
        del value[len(diameters) :]


@dataclass(frozen=True)
class _Index:
    #: Preset name -> its file.
    paths: dict[str, Path]
    #: ``setting_id`` -> preset name.
    names: dict[str, str]


class Profiles:
    """The image's BBL presets, each kind indexed by name and ``setting_id`` on its
    first use. Every read is file I/O: an async caller runs it in a thread."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._indexes: dict[Kind, _Index] = {}

    @classmethod
    def installed(cls, root: Path) -> Profiles | None:
        """The tree under ``root``, or ``None`` without one (a dev run outside the image)."""
        return cls(root) if (root / f"{VENDOR}.json").is_file() else None

    @cached_property
    def version(self) -> str:
        """The image's ``BAMBU_STUDIO_VERSION``: what a native slice's key carries."""
        return (self.root / "VERSION").read_text(encoding="utf-8").strip()

    @cached_property
    def profile_version(self) -> str:
        """``BBL.json``'s own ``version`` (``02.08.00.05`` in v02.08.02.61): a check of
        the tree, never a key, since it need not move with the slicer."""
        return str(self._vendor["version"])

    @cached_property
    def _vendor(self) -> dict[str, Any]:
        vendor: dict[str, Any] = json.loads(
            (self.root / f"{VENDOR}.json").read_text(encoding="utf-8")
        )
        return vendor

    def machine(self, ref_or_name: str) -> Flat:
        return self._flat("machine", ref_or_name)

    def process(self, ref_or_name: str) -> Flat:
        return self._flat("process", ref_or_name)

    def filament(self, ref_or_name: str) -> Flat:
        return self._flat("filament", ref_or_name)

    def find(self, kind: Kind, ref_or_name: str) -> str | None:
        """The name of the ``kind`` preset whose ``setting_id``, then ``name``, is
        ``ref_or_name``."""
        index = self._index(kind)
        if ref_or_name in index.names:
            return index.names[ref_or_name]
        return ref_or_name if ref_or_name in index.paths else None

    def _flat(self, kind: Kind, ref_or_name: str) -> Flat:
        name = self.find(kind, ref_or_name)
        if name is None:
            raise ProfileError(f"no {kind} preset {ref_or_name} in Bambu Studio {self.version}")
        return flatten(name, lambda parent: self._load(kind, parent))

    def _load(self, kind: Kind, name: str) -> dict[str, Any] | None:
        path = self._index(kind).paths.get(name)
        if path is None:
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ProfileError(f"the native slicer cannot read preset {name}: not an object")
        return doc

    def _index(self, kind: Kind) -> _Index:
        index = self._indexes.get(kind)
        if index is None:
            # Built twice at worst, by two threads at once: the same index either way.
            index = self._indexes[kind] = self._build(kind)
        return index

    def _build(self, kind: Kind) -> _Index:
        paths: dict[str, Path] = {}
        names: dict[str, str] = {}
        for entry in self._vendor.get(_LISTS[kind], []):
            path = self.root / VENDOR / entry["sub_path"]
            doc = json.loads(path.read_text(encoding="utf-8"))
            paths[entry["name"]] = path
            setting_id = doc.get("setting_id") if isinstance(doc, dict) else None
            if isinstance(setting_id, str) and setting_id:
                names[setting_id] = entry["name"]
        return _Index(paths=paths, names=names)
