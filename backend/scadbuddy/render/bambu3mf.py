from __future__ import annotations

import io
import json
import math
import os
import re
import tempfile
import uuid
import zipfile
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any, Literal
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

import numpy as np
import trimesh

from scadbuddy.render.paint import MAX_CODE_DIGITS, states
from scadbuddy.render.plate import (
    DEFAULT_PLATE,
    Placement,
    PlateFitError,
    PlateGeometry,
    centre_on_plate,
    place_on_plate,
)
from scadbuddy.render.split import PAINT_ATTRIBUTE, ColourPart
from scadbuddy.render.thumbnail import PlateThumbnails
from scadbuddy.render.zipcopy import ENCRYPTED, copy_raw, raw_copy_checked

#: Bambu Studio reads ``Metadata/project_settings.config`` only when the root
#: model's ``Application`` metadata starts with ``BambuStudio-``; otherwise
#: ``_load_model_from_file`` sets ``dont_load_config`` and drops the whole file,
#: which is why the prime-tower position we compute used to be ignored (#110).
#:
#: The version claimed is the *oldest* one that triggers none of the importer's
#: compatibility paths: ``BambuStudio.cpp`` translates old configs below 1.5.9,
#: regenerates thumbnails below 1.5.9, keeps old params below 2.0.0, disables
#: wrapping detection below 2.2.0 and resets ``skirt_per_object`` below 2.7.0.
#: Claiming 2.7.0 exactly clears all five, and claiming no more than that means
#: a CLI older than the one we tested still accepts the file — the newer-file
#: check at ``BambuStudio.cpp:1951`` only rejects files *ahead* of the reader.
BAMBU_APPLICATION = "BambuStudio-02.07.00.00"

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PRODUCTION_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
MODEL_RELATIONSHIP = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
MODEL_CONTENT_TYPE = "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"
RELS_CONTENT_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
PNG_CONTENT_TYPE = "image/png"

# The cover-image relationships Bambu Studio writes into `_rels/.rels`. The
# first is OPC's own; the other two are Bambu's, and are what the printer and
# the handheld app read. Ids 1, 2, 4, 5 with 3 skipped is Studio's own
# numbering (`_add_relationships_file_to_archive` in `bbs_3mf.cpp`) — kept
# because a reader that pattern-matches on them should find what it expects.
THUMBNAIL_RELATIONSHIP = (
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"
)
COVER_MIDDLE_RELATIONSHIP = "http://schemas.bambulab.com/package/2021/cover-thumbnail-middle"
COVER_SMALL_RELATIONSHIP = "http://schemas.bambulab.com/package/2021/cover-thumbnail-small"

# Plate 1's cover images. These names are Bambu Studio's formats
# (`THUMBNAIL_FILE_FORMAT` and friends in `bbs_3mf.hpp`) with the plate index
# substituted (`cover_names` for any other plate), and `PLATE_THUMBNAIL` is the
# entry Bambuddy's `ThreeMFParser` reads for a library file's `thumbnail_path`.
PLATE_THUMBNAIL = "Metadata/plate_1.png"
PLATE_THUMBNAIL_SMALL = "Metadata/plate_1_small.png"
PLATE_TOP = "Metadata/top_1.png"
PLATE_PICK = "Metadata/pick_1.png"

#: ``LOGICAL_PART_PLATE_GAP`` in Bambu Studio's ``PartPlate.cpp``: each plate of a
#: project sits a fifth of a bed beyond the one before it (spec §6.4).
PLATE_GAP = 1.0 / 5.0


def cover_names(index: int) -> tuple[str, str, str, str]:
    """Plate ``index``'s cover entries: the image, its small copy, the top view and
    the pick map, in :class:`~scadbuddy.render.thumbnail.PlateThumbnails` order."""
    return (
        f"Metadata/plate_{index}.png",
        f"Metadata/plate_{index}_small.png",
        f"Metadata/top_{index}.png",
        f"Metadata/pick_{index}.png",
    )


def plate_columns(count: int) -> int:
    """How many plates wide Bambu Studio lays ``count`` plates out.

    ``compute_colum_count`` in ``PartPlate.hpp``, transcribed: the square root,
    rounded, and one more when rounding went down."""
    value = math.sqrt(count)
    rounded = math.floor(value + 0.5)
    return rounded + 1 if value > rounded else rounded


def plate_origin(index: int, count: int, plate: PlateGeometry) -> tuple[float, float]:
    """Where plate ``index`` (1-based) of ``count`` sits in the project's coordinates.

    ``PartPlateList::compute_shape_position``: columns to the right, rows towards
    -Y, one bed plus :data:`PLATE_GAP` apart. The bed is ``printable_area``'s
    extent, which the CLI holds as whole millimetres (``int``). Bambu Studio puts
    an object on the plate whose area it overlaps, so this is what decides which
    plate a part prints on — the ``<plate>`` list does not (spec §6.4).
    """
    columns = plate_columns(count)
    row, column = divmod(index - 1, columns)
    width, depth = int(plate.size[0]), int(plate.size[1])
    return (column * width * (1.0 + PLATE_GAP), -row * depth * (1.0 + PLATE_GAP))


UUID_NAMESPACE = uuid.UUID("2f0c5f8e-6c1a-5d3b-9a7f-4f2d8b1c6e30")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
IDENTITY = "1 0 0 0 1 0 0 0 1 0 0 0"
ROOT_MODEL_NAME = "3D/3dmodel.model"
PROJECT_SETTINGS_NAME = "Metadata/project_settings.config"


def _uuid(model_name: str, tag: str) -> str:
    return str(uuid.uuid5(UUID_NAMESPACE, f"scadbuddy/{model_name}/{tag}"))


def _number(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def object_model(part: ColourPart, object_id: int) -> str:
    vertices = "".join(
        f'\n     <vertex x="{_number(v[0])}" y="{_number(v[1])}" z="{_number(v[2])}"/>'
        for v in part.mesh.vertices
    )
    codes = part.paint.codes if part.paint is not None else ("",) * len(part.mesh.faces)
    if len(codes) != len(part.mesh.faces):
        raise ValueError(f"part {part.name!r} has paint for another number of faces")
    triangles = "".join(
        f'\n     <triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}"'
        + (f' {PAINT_ATTRIBUTE}="{code}"' if code else "")
        + "/>"
        for f, code in zip(part.mesh.faces, codes, strict=True)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}" xmlns:p="{PRODUCTION_NS}">\n'
        " <resources>\n"
        f'  <object id="{object_id}" name={quoteattr(part.name)} type="model">\n'
        f"   <mesh>\n    <vertices>{vertices}\n    </vertices>\n"
        f"    <triangles>{triangles}\n    </triangles>\n"
        "   </mesh>\n"
        "  </object>\n"
        " </resources>\n"
        " <build/>\n"
        "</model>\n"
    )


def model_bounds(parts: Sequence[ColourPart]) -> np.ndarray:
    """The ``(2, 3)`` min/max box every part shares, in the meshes' coordinates: those
    that print, since a negative part is cut out."""
    printed = [part for part in parts if not part.negative] or list(parts)
    bounds = np.array([part.mesh.bounds for part in printed])
    return np.array([bounds[:, 0, :].min(axis=0), bounds[:, 1, :].max(axis=0)])


@dataclass(frozen=True)
class PlateParts:
    """One plate of a 3MF: its colour parts, and the 1-based extruder each prints
    with — an index into the file's one filament list, which every plate shares
    (spec §6.4)."""

    parts: tuple[ColourPart, ...]
    extruders: tuple[int, ...]
    #: The 1-based object each part belongs to (#956): a template's declared object, or
    #: a packed copy. Empty means one object, every part in it.
    objects: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.parts:
            raise ValueError("a plate needs at least one colour part")
        if len(self.parts) != len(self.extruders):
            raise ValueError("a plate needs one extruder per colour part")
        if not self.objects:
            object.__setattr__(self, "objects", (1,) * len(self.parts))
        if len(self.objects) != len(self.parts):
            raise ValueError("a plate needs one object number per colour part")

    @property
    def tower(self) -> bool:
        """Whether this plate needs a prime tower: only when it uses more than one
        filament. One colour prints without one, so reserving room would refuse
        plates that are perfectly printable. A painted part prints with its painting's
        filaments too (#1965), but a painting only in filaments the plate already uses
        adds none (#2108). The painting must be numbered against the file's filament
        list, as `write_plates_3mf` renumbers it before asking."""
        used = set(self.extruders)
        for part in self.parts:
            if part.paint is not None:
                used.update(state for code in part.paint.codes if code for state in states(code))
        return len(used) > 1


def single_plate(parts: Sequence[ColourPart]) -> PlateParts:
    """``parts`` as the one plate of an ordinary render: extruder N is part N."""
    return PlateParts(tuple(parts), tuple(range(1, len(parts) + 1)))


def _placement(bounds: np.ndarray, plate: PlateGeometry, *, tower: bool) -> Placement:
    try:
        return place_on_plate(bounds, plate, tower=tower)
    except PlateFitError:
        # "No printer chosen" is a property of the plate, not of which object was
        # passed. An identity test against the shared singleton would silently
        # take the hard-refuse branch for an equivalent fallback built any other
        # way, with neither a type error nor a failing test to catch it.
        if plate.model is not None:
            raise
        # Nobody chose the fallback plate, so it is not grounds for refusing a
        # render. ``replate_3mf`` re-checks against the printer that is chosen.
        return centre_on_plate(bounds, plate)


def _numbered(tag: str, plate_index: int) -> str:
    """A per-plate UUID tag. Plate 1 keeps the bare tag, so a one-plate file is
    byte-for-byte what it was before plates were numbered."""
    return tag if plate_index == 1 else f"{tag}-{plate_index}"


def _assembly_name(model_name: str, plate_index: int, count: int) -> str:
    return model_name if count == 1 else f"{model_name} (plate {plate_index})"


def _object_ids(plates: Sequence[PlateParts]) -> list[range]:
    """The ``object_N`` ids each plate's parts take, numbered across plates."""
    ranges: list[range] = []
    first = 1
    for plate in plates:
        ranges.append(range(first, first + len(plate.parts)))
        first += len(plate.parts)
    return ranges


@dataclass(frozen=True)
class _Assembly:
    """One object of a plate as the writer emits it (#956): its id, its 1-based number
    on the plate, and the ``object_N`` ids, parts and extruders of its colour parts."""

    id: int
    plate_index: int
    number: int
    count: int
    part_ids: tuple[int, ...]
    parts: tuple[ColourPart, ...]
    extruders: tuple[int, ...]

    def tag(self, tag: str) -> str:
        """Its UUID tag: a plate's first object keeps the plate's, so a file of one
        object per plate is byte-for-byte what it was before objects (#956)."""
        numbered = _numbered(tag, self.plate_index)
        return numbered if self.number == 1 else f"{numbered}-object-{self.number}"

    def name(self, model_name: str, plates: int) -> str:
        name = _assembly_name(model_name, self.plate_index, plates)
        return name if self.count == 1 else f"{name} (object {self.number})"


def _assemblies(plates: Sequence[PlateParts]) -> list[list[_Assembly]]:
    """Each plate's objects, one assembly per distinct ``PlateParts.objects`` number,
    in first-seen order. Assembly ids follow the part ids, counted across plates."""
    next_id = sum(len(plate.parts) for plate in plates) + 1
    result: list[list[_Assembly]] = []
    for plate_index, (plate, ids) in enumerate(
        zip(plates, _object_ids(plates), strict=True), start=1
    ):
        numbers = list(dict.fromkeys(plate.objects))
        assemblies: list[_Assembly] = []
        for position, number in enumerate(numbers, start=1):
            members = [
                (part_id, part, extruder)
                for part_id, part, extruder, owner in zip(
                    ids, plate.parts, plate.extruders, plate.objects, strict=True
                )
                if owner == number
            ]
            assemblies.append(
                _Assembly(
                    id=next_id,
                    plate_index=plate_index,
                    number=position,
                    count=len(numbers),
                    part_ids=tuple(m[0] for m in members),
                    parts=tuple(m[1] for m in members),
                    extruders=tuple(m[2] for m in members),
                )
            )
            next_id += 1
        result.append(assemblies)
    return result


def root_model(
    plates: Sequence[PlateParts], model_name: str, offsets: Sequence[Sequence[float]]
) -> str:
    objects: list[str] = []
    items: list[str] = []
    for assemblies, offset in zip(_assemblies(plates), offsets, strict=True):
        translation = " ".join(_number(value) for value in offset)
        for assembly in assemblies:
            components = "".join(
                f'\n    <component p:path="/3D/Objects/object_{index}.model" objectid="{index}"'
                f' p:UUID="{_uuid(model_name, f"component-{index}")}" transform="{IDENTITY}"/>'
                for index in assembly.part_ids
            )
            name = assembly.name(model_name, len(plates))
            objects.append(
                f'  <object id="{assembly.id}" name={quoteattr(name)} type="model"'
                f' p:UUID="{_uuid(model_name, assembly.tag("assembly"))}">\n'
                f"   <components>{components}\n   </components>\n"
                "  </object>\n"
            )
            # Every object of a plate is moved by the plate's one offset: the template
            # drew them where they sit relative to each other.
            items.append(
                f'  <item objectid="{assembly.id}" transform="1 0 0 0 1 0 0 0 1 {translation}"'
                f' printable="1" p:UUID="{_uuid(model_name, assembly.tag("item"))}"/>\n'
            )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}" xmlns:p="{PRODUCTION_NS}">\n'
        f' <metadata name="Application">{BAMBU_APPLICATION}</metadata>\n'
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
        ' <metadata name="Origin">ScadBuddy</metadata>\n'
        f' <metadata name="Title">{escape(model_name)}</metadata>\n'
        " <resources>\n"
        f"{''.join(objects)}"
        " </resources>\n"
        f' <build p:UUID="{_uuid(model_name, "build")}">\n'
        f"{''.join(items)}"
        " </build>\n"
        "</model>\n"
    )


def _subtype(part: ColourPart) -> str:
    """Bambu Studio's part subtype: a negative part is cut out of its object (#1863)."""
    return "negative_part" if part.negative else "normal_part"


def model_settings(plates: Sequence[PlateParts], model_name: str, *, covers: bool) -> str:
    objects: list[str] = []
    plate_entries: list[str] = []
    for plate_index, assemblies in enumerate(_assemblies(plates), start=1):
        instances: list[str] = []
        for assembly in assemblies:
            entries = "".join(
                f'  <part id="{index}" subtype="{_subtype(part)}">\n'
                f'   <metadata key="name" value={quoteattr(part.name)}/>\n'
                f'   <metadata key="extruder" value="{extruder}"/>\n'
                "  </part>\n"
                for index, part, extruder in zip(
                    assembly.part_ids, assembly.parts, assembly.extruders, strict=True
                )
            )
            name = assembly.name(model_name, len(plates))
            objects.append(
                f' <object id="{assembly.id}">\n'
                f'  <metadata key="name" value={quoteattr(name)}/>\n'
                f'  <metadata key="extruder" value="{assembly.extruders[0]}"/>\n'
                f"{entries}"
                " </object>\n"
            )
            instances.append(
                "  <model_instance>\n"
                f'   <metadata key="object_id" value="{assembly.id}"/>\n'
                '   <metadata key="instance_id" value="0"/>\n'
                "  </model_instance>\n"
            )
        plate_entries.append(
            " <plate>\n"
            f'  <metadata key="plater_id" value="{plate_index}"/>\n'
            '  <metadata key="plater_name" value=""/>\n'
            '  <metadata key="locked" value="false"/>\n'
            f"{_cover_metadata(plate_index) if covers else ''}"
            f"{''.join(instances)}"
            " </plate>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<config>\n"
        f"{''.join(objects)}"
        f"{''.join(plate_entries)}"
        "</config>\n"
    )


def _cover_metadata(plate_index: int) -> str:
    thumbnail, _small, top, pick = cover_names(plate_index)
    return (
        f'  <metadata key="thumbnail_file" value="{thumbnail}"/>\n'
        f'  <metadata key="top_file" value="{top}"/>\n'
        f'  <metadata key="pick_file" value="{pick}"/>\n'
    )


#: Options ``BambuStudio.cpp`` dereferences without a null check once a 3MF
#: identifies as a BambuStudio project. ``printer_settings_id`` and
#: ``print_settings_id`` are read at line 2009-2010, ``filament_settings_id``
#: and ``nozzle_diameter`` right after, and ``printable_height`` through
#: ``config.opt_float`` at line 2095. A missing one is not a validation error —
#: it is a segfault before slicing starts, which is what made the naive "just
#: set the Application tag" attempt look like a dead end (#110).
#:
#: The CLI reads them into ``current_*``/``old_*`` locals used for reporting and
#: compatibility comparisons only; the settings actually sliced with come from
#: ``--load-settings``/``--load-filaments``. Measured: placeholder ids, a
#: deliberately wrong nozzle diameter (0.4 while slicing 0.2) and a wrong
#: printable height all produce byte-identical results. So ScadBuddy states what
#: it knows and names itself for the rest — it owns no slicer settings. What it
#: knows includes the printer a file is laid out for, named as Bambu Studio names
#: it (:func:`printer_preset`, #2639), since Bambuddy's slice dialog pre-picks the
#: printer by that name; the process stays this placeholder, and marks the layout
#: as ScadBuddy's (:func:`layout_of`).
PRESET_PLACEHOLDER = "ScadBuddy"
#: Only the arity-free presence of this option matters; 1 and 3 entries were both
#: measured to slice identically on a two-extruder H2C. The print run replaces it
#: with the nozzle it chose (#126): slicing never reads it, but a person deciding
#: whether to start a print does.
PLACEHOLDER_NOZZLE_DIAMETER = ["0.4"]


def printer_preset(plate: PlateGeometry, nozzle: str) -> str:
    """The printer preset a 3MF laid out for ``plate`` names, as Bambu Studio names
    it (``"Bambu Lab A1 0.4 nozzle"``): Bambuddy's slice dialog pre-picks the printer
    by this name, and with :data:`PRESET_PLACEHOLDER` it took the first printer it
    lists. The fallback plate is no printer, so it keeps the placeholder.

    Before a print chooses a nozzle, ``nozzle`` is the file's stated diameter,
    0.4 as written: a printer fitted with another size is pre-picked at 0.4, as
    Bambu Studio itself opens a new project, and the dialog lets that be changed.
    A print renames it for the nozzle it chose (:func:`replate_3mf`)."""
    return f"{plate.model} {nozzle} nozzle" if plate.model else PRESET_PLACEHOLDER


def one_extruder_map(filaments: int) -> dict[str, str | list[str]]:
    """Every filament on extruder 1, as Bambu Studio 02.08.02.61 saved the maintainer's
    two-colour H2C keychain at 0.2 mm, which printed through the one 0.2 mm nozzle
    (2026-09-29, #768): ``filament_map`` ``["1", "1"]`` under ``"Auto For Flush"``.

    Not measured to steer Bambuddy's headless slicer: on 2026-09-28 it kept a
    full-length ``["1", "1"]`` map, Auto or Manual, and still spread two filaments onto
    two nozzles (#745). Only the project-level map is written; one at plate level in
    ``model_settings.config`` crashed that slicer the same day. Nor is it where Bambu
    Studio's filaments went: that file's ``slice_info.config`` says ``"2 2"``, and
    extruder 1 is the left (#834). ``extruder_nozzle_stats`` is what steers the slicer."""
    return {"filament_map": ["1"] * filaments, "filament_map_mode": "Auto For Flush"}


def project_settings(
    colours: Sequence[str],
    placements: Sequence[Placement],
    plate: PlateGeometry,
    materials: Mapping[str, str] | None = None,
) -> str:
    """``Metadata/project_settings.config``, in Bambu Studio's own JSON shape.

    ``ConfigBase::save_to_json`` writes every vector option as an array of
    *stringified* values, and ``wipe_tower_x``/``wipe_tower_y`` are per-plate
    ``coFloats`` holding the tower's front-left corner — hence ``["145"]`` rather
    than ``[145.0]``.

    Everything here is read only because the root model claims to be a
    BambuStudio project (see :data:`BAMBU_APPLICATION`). Without that claim
    ``bbs_3mf.cpp`` sets ``dont_load_config`` and drops the file wholesale, which
    is why the tower position sat here inert until #110 — and with the claim, the
    keys in :data:`PRESET_PLACEHOLDER`'s comment above stop being optional.

    ``colours`` is the one filament list every plate shares; ``placements`` has one
    entry per plate, in plate order (spec §6.4).

    ``materials`` maps a colour to the material its source stated (an arranged library
    file's ``filament_type``). ``filament_type`` is written only when every slot has
    one: Bambuddy's slice dialog picks each slot's filament preset by it, and with none
    it takes the first preset it lists, Bambu ABS, for a file whose sources were PLA. A
    partial list is not written: a slot with ``""`` would get that same guess, and
    what Bambu Studio makes of one was never measured.

    Sources: the dialog's pick is ``pickFilamentForSlot`` in Bambuddy's
    ``frontend/src/utils/slicePresetPicker.ts``, fed by the ``filament_type`` its
    ``library.py`` reads from this file (read 2026-10-12 at maziggy/bambuddy main).
    A Bambu Studio project states it beside ``filament_colour`` (Bambuddy's filament
    requirements read both there, as :mod:`scadbuddy.bambuddy.filaments3mf` does), and
    ScadBuddy copies the strings a library project stated. Not yet measured: a
    ScadBuddy-written 3MF carrying it, sliced by Bambuddy's headless slicer.
    """
    settings: dict[str, str | list[str]] = {
        "filament_colour": list(colours),
        # Required-or-segfault; see above.
        "printer_settings_id": printer_preset(plate, PLACEHOLDER_NOZZLE_DIAMETER[0]),
        # Also what marks the layout as ScadBuddy's (`layout_of`): ScadBuddy owns no
        # process, and Bambu Studio names one when it saves the file.
        "print_settings_id": PRESET_PLACEHOLDER,
        "filament_settings_id": [PRESET_PLACEHOLDER for _ in colours],
        "nozzle_diameter": PLACEHOLDER_NOZZLE_DIAMETER,
        "printable_height": _number(plate.height),
    }
    stated = [(materials or {}).get(colour.upper(), "") for colour in colours]
    if stated and all(stated):
        settings["filament_type"] = stated
    _set_towers(settings, [placement.tower for placement in placements])
    return json.dumps(settings, indent=4) + "\n"


def _set_towers(settings: dict[str, Any], towers: Sequence[tuple[float, float] | None]) -> None:
    """``wipe_tower_x``/``wipe_tower_y``, one entry per plate (Bambu Studio's
    per-plate ``coFloats``). A plate without a tower repeats another plate's corner:
    the slicer never reads it there, and a short array would make it fall back to
    entry 0 anyway. No tower anywhere, and the keys are left out."""
    settings.pop("wipe_tower_x", None)
    settings.pop("wipe_tower_y", None)
    known = [tower for tower in towers if tower is not None]
    if not known:
        return
    filled = [tower if tower is not None else known[0] for tower in towers]
    settings["wipe_tower_x"] = [_number(tower[0]) for tower in filled]
    settings["wipe_tower_y"] = [_number(tower[1]) for tower in filled]


def _content_types(*, covers: bool) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        f' <Default Extension="rels" ContentType="{RELS_CONTENT_TYPE}"/>\n'
        f' <Default Extension="model" ContentType="{MODEL_CONTENT_TYPE}"/>\n'
        + (f' <Default Extension="png" ContentType="{PNG_CONTENT_TYPE}"/>\n' if covers else "")
        + "</Types>\n"
    )


def _package_rels(*, covers: bool) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        f' <Relationship Id="rel-1" Type="{MODEL_RELATIONSHIP}" Target="/3D/3dmodel.model"/>\n'
        + (_cover_rels() if covers else "")
        + "</Relationships>\n"
    )


def _cover_rels() -> str:
    return (
        f' <Relationship Id="rel-2" Type="{THUMBNAIL_RELATIONSHIP}"'
        f' Target="/{PLATE_THUMBNAIL}"/>\n'
        f' <Relationship Id="rel-4" Type="{COVER_MIDDLE_RELATIONSHIP}"'
        f' Target="/{PLATE_THUMBNAIL}"/>\n'
        f' <Relationship Id="rel-5" Type="{COVER_SMALL_RELATIONSHIP}"'
        f' Target="/{PLATE_THUMBNAIL_SMALL}"/>\n'
    )


def _model_rels(count: int) -> str:
    entries = "".join(
        f' <Relationship Id="rel-{index}" Type="{MODEL_RELATIONSHIP}"'
        f' Target="/3D/Objects/object_{index}.model"/>\n'
        for index in range(1, count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        f"{entries}"
        "</Relationships>\n"
    )


MODEL_SETTINGS_NAME = "Metadata/model_settings.config"


@dataclass(frozen=True)
class PlateEntry:
    """One ``<plate>`` of ``model_settings.config``: its 1-based index, which is the
    ``plate``/``plate_id`` Bambuddy slices and queues, and its cover image if the
    package carries one."""

    index: int
    thumbnail: str | None
    #: What the plate holds, for a label (#929): its ``plater_name``, else the names of
    #: the objects on it joined with " + ", else ``None``.
    name: str | None = None


def _metadata(node: ET.Element) -> dict[str, str]:
    """A ``model_settings.config`` node's ``<metadata key= value=>`` children."""
    return {entry.get("key") or "": entry.get("value") or "" for entry in node.findall("metadata")}


@dataclass(frozen=True)
class PlateSettings:
    """One ``<plate>`` of ``model_settings.config`` as written: its ``plater_id``,
    its metadata, and the object id its ``model_instance`` names (``None`` when it
    has none). The one reader of that structure, for :func:`plates_of` and
    :func:`laid_out_plates` alike."""

    index: int
    metadata: dict[str, str]
    object_id: str | None
    #: Every ``model_instance``'s object id, in file order (#929).
    object_ids: tuple[str, ...] = ()


def plate_settings(config: ET.Element) -> list[PlateSettings]:
    """Every ``<plate>`` of a parsed ``model_settings.config``, in file order.

    Raises `ValueError` for a plate with no ``plater_id``.
    """
    plates: list[PlateSettings] = []
    for plate in config.iter("plate"):
        metadata = _metadata(plate)
        if "plater_id" not in metadata:
            raise ValueError("a <plate> in the 3MF's model settings has no plater_id")
        instance = plate.find("model_instance")
        object_id = _metadata(instance).get("object_id") if instance is not None else None
        object_ids = tuple(
            _metadata(instance).get("object_id", "") for instance in plate.findall("model_instance")
        )
        plates.append(
            PlateSettings(int(metadata["plater_id"] or 0), metadata, object_id, object_ids)
        )
    return plates


def plates_of(path: Path | IO[bytes]) -> list[PlateEntry]:
    """Every plate the 3MF lays out, in index order (#83).

    ScadBuddy's own writer produces one unless the template asks for more (#289); a
    Bambu Studio project can hold more too.
    """
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        config = ET.fromstring(archive.read(MODEL_SETTINGS_NAME))
    # An <object> without an id is named by nothing, so a missing object_id ("") on a
    # plate's instance can never meet one.
    object_names = {
        object_id: _metadata(obj).get("name", "")
        for obj in config.iter("object")
        if (object_id := obj.get("id"))
    }
    plates: list[PlateEntry] = []
    for plate in plate_settings(config):
        cover = plate.metadata.get("thumbnail_file")
        on_plate = (object_names.get(object_id, "") for object_id in plate.object_ids)
        name = plate.metadata.get("plater_name") or " + ".join(n for n in on_plate if n) or None
        plates.append(PlateEntry(plate.index, cover if cover in names else None, name))
    return sorted(plates, key=lambda plate: plate.index)


def write_bambu_3mf(
    parts: Sequence[ColourPart],
    out_path: Path,
    *,
    thumbnails: PlateThumbnails | None,
    model_name: str = "model",
    plate: PlateGeometry = DEFAULT_PLATE,
) -> None:
    """Write the one-plate 3MF of an ordinary render, laid out for ``plate``.

    The render pipeline has no printer yet, so it writes against
    :data:`~scadbuddy.render.plate.DEFAULT_PLATE`; :func:`replate_3mf` moves the
    result onto the real one when the send path learns which printer it is for.

    ``thumbnails`` is a required keyword with no default on purpose: ``None``
    means the cover images are absent, and then the ``png`` content type, the
    three cover relationships and the plate's
    ``thumbnail_file``/``top_file``/``pick_file`` all have to come out WITH
    them. Leaving a reference to an entry that is not in the package is a silent
    failure, so the caller has to say which package it wants rather than inherit
    one.
    """
    if not parts:
        raise ValueError("a 3MF needs at least one colour part")
    write_plates_3mf(
        [single_plate(parts)],
        [part.colour for part in parts],
        out_path,
        thumbnails=[thumbnails] if thumbnails is not None else None,
        model_name=model_name,
        plate=plate,
    )


def write_plates_3mf(
    plates: Sequence[PlateParts],
    colours: Sequence[str],
    out_path: Path,
    *,
    thumbnails: Sequence[PlateThumbnails] | None,
    model_name: str = "model",
    plate: PlateGeometry = DEFAULT_PLATE,
    materials: Mapping[str, str] | None = None,
) -> None:
    """Write a 3MF with one Bambu plate per entry of ``plates`` (spec §6.4).

    ``colours`` is the filament list every plate's extruder numbers index into.
    ``thumbnails`` is one cover set per plate, or ``None`` for none on any plate
    (the same all-or-nothing rule as :func:`write_bambu_3mf`). Each plate's parts
    are placed on ``plate`` as a one-plate render would be, then moved to that
    plate's origin in the project (:func:`plate_origin`).
    """
    if not plates:
        raise ValueError("a 3MF needs at least one plate")
    if thumbnails is not None and len(thumbnails) != len(plates):
        raise ValueError("cover images are all or nothing: one set per plate")
    if any(not 1 <= extruder <= len(colours) for each in plates for extruder in each.extruders):
        raise ValueError("every part's extruder must name one of the filament colours")
    # A painted part's codes name extruders by its own numbering: renumber them into
    # the file's one filament list, which is what the slicer reads them against (#1965).
    plates = [
        replace(
            each,
            parts=tuple(
                replace(part, paint=part.paint.renumbered(colours))
                if part.paint is not None
                else part
                for part in each.parts
            ),
        )
        for each in plates
    ]
    covers = thumbnails is not None
    placements = [_placement(model_bounds(each.parts), plate, tower=each.tower) for each in plates]
    offsets = [
        _plate_offset(placement, index, len(plates), plate)
        for index, placement in enumerate(placements, start=1)
    ]
    ids = _object_ids(plates)
    entries: list[tuple[str, bytes]] = [
        (name, payload.encode("utf-8"))
        for name, payload in (
            ("[Content_Types].xml", _content_types(covers=covers)),
            ("_rels/.rels", _package_rels(covers=covers)),
            (ROOT_MODEL_NAME, root_model(plates, model_name, offsets)),
            ("3D/_rels/3dmodel.model.rels", _model_rels(ids[-1].stop - 1)),
            *(
                (f"3D/Objects/object_{index}.model", object_model(part, index))
                for each, numbers in zip(plates, ids, strict=True)
                for index, part in zip(numbers, each.parts, strict=True)
            ),
            (MODEL_SETTINGS_NAME, model_settings(plates, model_name, covers=covers)),
            (PROJECT_SETTINGS_NAME, project_settings(colours, placements, plate, materials)),
        )
    ]
    if thumbnails is not None:
        for index, images in enumerate(thumbnails, start=1):
            names = cover_names(index)
            entries += list(
                zip(names, (images.plate, images.plate_small, images.top, images.pick), strict=True)
            )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Written aside and swapped in whole (#867): a timed-out attempt's thread that is
    # still writing can never leave a half-written archive at `out_path`. A hard kill
    # before the replace leaves the dot-named staging file: `pack_dir` never publishes
    # dot files, and it goes when the piece's directory is evicted or swept.
    staging = out_path.with_name(f".{out_path.name}.{uuid.uuid4().hex}")
    try:
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in entries:
                info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
                # PNG is already a deflate stream; re-deflating it is pure CPU for
                # nothing, and Studio stores its own thumbnails uncompressed too.
                info.compress_type = (
                    zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
                )
                archive.writestr(info, payload)
        os.replace(staging, out_path)
    finally:
        staging.unlink(missing_ok=True)


def _plate_offset(
    placement: Placement, index: int, count: int, plate: PlateGeometry
) -> tuple[float, float, float]:
    """The build-item translation of plate ``index``: its placement on a plate,
    moved to where that plate sits in the project."""
    origin = plate_origin(index, count, plate)
    x, y, z = placement.offset
    return (origin[0] + x, origin[1] + y, z)


_ITEM = re.compile(r"<item\b[^>]*>")
_ATTRIBUTE = r'\b{name}="([^"]*)"'


def _attribute(tag: str, name: str) -> str | None:
    match = re.search(_ATTRIBUTE.format(name=name), tag)
    return match.group(1) if match else None


#: The longest coordinate :func:`object_model` writes (``_number``: six decimals) is far
#: under this; a longer one is not that writer's, and is parsed instead. It bounds what
#: one vertex costs as an array row, whatever the file holds (#2877).
_MAX_WRITTEN_NUMBER = 32
#: A ``<vertex>`` exactly as :func:`object_model` writes one, and any tag named so: the
#: two counted against each other, so a file written any other way is parsed instead.
_WRITTEN_VERTEX = re.compile(
    rb'<vertex x="([^"]{1,%d})" y="([^"]{1,%d})" z="([^"]{1,%d})"/>' % ((_MAX_WRITTEN_NUMBER,) * 3)
)
_ANY_VERTEX = re.compile(rb"<(?:[\w.-]+:)?vertex\b")
#: How far into a ``<triangle>`` tag its paint code is looked for: the writer puts it after
#: three vertex indices, well inside this.
_MAX_TRIANGLE_LEAD = 256
#: A triangle's paint code, as the parse reads it (a ``<triangle>``'s attribute), and
#: the attribute anywhere: the two counted against each other. Both runs are bounded, so
#: a file of unterminated tags or quotes costs a pass, not a pass per tag (quadratic: 200
#: KB took 14 s); a code they miss leaves the counts unequal, and the file to the parse.
_PAINT_CODE = re.compile(
    rb"<triangle\b[^>]{0,%d}?\s%s=\"([^\"]{0,%d})\""
    % (_MAX_TRIANGLE_LEAD, PAINT_ATTRIBUTE.encode(), MAX_CODE_DIGITS + 1)
)
_ANY_PAINT = re.compile(rb"\s" + PAINT_ATTRIBUTE.encode() + rb'="')
#: The elements :func:`object_model` writes exactly one of; another count is the shape
#: the parse refuses or reads otherwise (#2024), so it is left to the parse.
_ONE_EACH = tuple(
    re.compile(rb"<(?:[\w.-]+:)?%s\b" % tag)
    for tag in (b"object", b"mesh", b"vertices", b"triangles")
)
#: Vertices turned into numbers at a time, so the copy never costs more than a batch.
_VERTEX_BATCH = 65536


def _written_extents(data: bytes) -> tuple[np.ndarray, frozenset[int]] | None:
    """The ``(2, 3)`` box and painted filaments of an object file :func:`object_model`
    wrote, read off its bytes by pattern rather than parsed vertex by vertex (#2877):
    a re-placed print only needs the box, and the parse of an 86 MB file held a worker
    for 39 s. ``None`` when the file is not in that writer's shape (a vertex written
    otherwise or with a coordinate past :data:`_MAX_WRITTEN_NUMBER`, a comment, an
    entity, a processing instruction, no vertex at all, other than one object, mesh,
    ``<vertices>`` and ``<triangles>``, a paint code off a triangle or past its cap),
    which :func:`_plate_geometry` then parses as it always has.

    Every value is bounded before it is copied: no entity is expanded (none is
    accepted), coordinates are read a batch at a time into rows of at most
    :data:`_MAX_WRITTEN_NUMBER` bytes, and a paint code is measured before it is kept."""
    # The writer's one processing instruction is its XML declaration; any other, like a
    # comment, CDATA or an entity, is something the pattern does not read as XML does.
    body = data[data.index(b"?>") + 2 :] if data.startswith(b"<?xml ") and b"?>" in data else data
    if any(marker in body for marker in (b"<?", b"<!--", b"<![CDATA[", b"&")):
        return None
    if any(sum(1 for _ in tag.finditer(data)) != 1 for tag in _ONE_EACH):
        return None
    low = np.full(3, np.inf)
    high = np.full(3, -np.inf)
    count = 0
    batch: list[tuple[bytes, bytes, bytes]] = []

    def reduce() -> None:
        points = np.array(batch, dtype=f"S{_MAX_WRITTEN_NUMBER}").astype(np.float64)
        np.minimum(low, points.min(axis=0), out=low)
        np.maximum(high, points.max(axis=0), out=high)
        batch.clear()

    try:
        for match in _WRITTEN_VERTEX.finditer(data):
            batch.append((match[1], match[2], match[3]))
            count += 1
            if len(batch) == _VERTEX_BATCH:
                reduce()
        if batch:
            reduce()
    except ValueError:
        return None
    if not count or count != sum(1 for _ in _ANY_VERTEX.finditer(data)):
        return None
    codes: set[bytes] = set()
    found = 0
    for match in _PAINT_CODE.finditer(data):
        code = match.group(1)
        if len(code) > MAX_CODE_DIGITS or not code.isascii():
            return None
        codes.add(code)
        found += 1
    if found != sum(1 for _ in _ANY_PAINT.finditer(data)):
        return None
    painted: set[int] = set()
    for code in codes:
        if code:
            painted.update(states(code.decode("ascii")))
    return np.array([low, high]), frozenset(painted)


def _plate_geometry(
    archive: zipfile.ZipFile, names: Sequence[str], entries: Mapping[str, bytes]
) -> tuple[np.ndarray, frozenset[int]]:
    """The ``(2, 3)`` box of the object files ``names``, read back out of an archive
    whose entries' bytes are ``entries``, and the filaments their painting uses
    (#1965): what a plate's tower counts beside its parts' extruders (#2108), which
    ``model_settings.config`` does not state.

    The component transforms this writer emits are the identity, so the vertex
    extents *are* the assembly's extents; the build item carries the whole
    placement. Reading them back rather than trusting a recorded plate size keeps
    :func:`replate_3mf` correct for files written before plates were per printer.
    A file this writer wrote is read by pattern (:func:`_written_extents`, #2877);
    any other is parsed.
    """
    lows: list[np.ndarray] = []
    highs: list[np.ndarray] = []
    painted: set[int] = set()
    for name in names:
        written = _written_extents(entries[name])
        if written is not None:
            box, codes = written
            lows.append(box[0])
            highs.append(box[1])
            painted.update(codes)
            continue
        for mesh in parse_model(archive, name).meshes.values():
            points = mesh.vertices
            if points.size:
                lows.append(points.min(axis=0))
                highs.append(points.max(axis=0))
            for code in set(mesh.paint or ()):
                if code:
                    painted.update(states(code))
    if not lows:
        raise ValueError("the 3MF has no object geometry to place")
    return np.array([np.min(lows, axis=0), np.max(highs, axis=0)]), frozenset(painted)


@dataclass(frozen=True)
class LaidOutPlate:
    """What :func:`replate_3mf` needs of one plate of a written archive."""

    index: int
    assembly_id: str
    object_files: tuple[str, ...]
    #: Every build item's assembly on the plate (#956): one object per declared object,
    #: all moved by the plate's one offset. ``assembly_id`` is the first.
    assembly_ids: tuple[str, ...]
    #: The extruders its parts print with, where ``model_settings.config`` states them;
    #: ``None`` when it does not. A painting's filaments are not among them.
    extruders: frozenset[int] | None = None


def laid_out_plates(archive: zipfile.ZipFile) -> list[LaidOutPlate]:
    """Each plate of a ScadBuddy archive: its index, its build item's assembly and
    the object files that assembly is made of.

    Plates are read from ``model_settings.config`` (``plater_id`` → the assembly its
    ``model_instance`` names), and a file with no such list is one plate holding
    every build item — the shape of every 3MF written before plates were numbered.
    """
    names = set(archive.namelist())
    root = parse_model(archive, ROOT_MODEL_NAME).root
    components: dict[str, tuple[str, ...]] = {}
    for obj in root.iter(f"{{{CORE_NS}}}object"):
        paths = tuple(
            (component.get(f"{{{PRODUCTION_NS}}}path") or "").lstrip("/")
            for component in obj.iter(f"{{{CORE_NS}}}component")
        )
        components[obj.get("id", "")] = tuple(path for path in paths if path in names)
    items = [item.get("objectid", "") for item in root.iter(f"{{{CORE_NS}}}item")]
    if not items:
        raise ValueError("the 3MF has no build item to place")

    plates: list[tuple[int, tuple[str, ...]]] = []
    extruders: dict[str, set[int]] = {}
    if MODEL_SETTINGS_NAME in names:
        config = ET.fromstring(archive.read(MODEL_SETTINGS_NAME))
        for obj in config.iter("object"):
            stated = {
                _metadata(part).get("extruder", "")
                for part in obj.findall("part")
                if part.get("subtype", "normal_part") == "normal_part"
            }
            extruders[obj.get("id", "")] = {int(e) for e in stated if e.isdigit()}
        for each in plate_settings(config):
            on_plate = tuple(object_id for object_id in each.object_ids if object_id in items)
            if on_plate:
                plates.append((each.index, on_plate))
    if sum(len(ids) for _, ids in plates) != len(items):
        # Not a layout this writer produced plate by plate: one plate, placed by its
        # first build item, holding every object file.
        every = tuple(sorted(name for name in names if name.startswith("3D/Objects/")))
        return [LaidOutPlate(1, items[0], every, (items[0],))]
    return [
        LaidOutPlate(
            index,
            ids[0],
            tuple(path for object_id in ids for path in components.get(object_id, ())),
            ids,
            frozenset().union(*(extruders[i] for i in ids))
            if all(i in extruders for i in ids)
            else None,
        )
        for index, ids in sorted(plates)
    ]


def replate_3mf(
    payload: bytes,
    plate: PlateGeometry,
    *,
    nozzle_diameter: str | None = None,
    nozzle_stats: Sequence[str] | None = None,
    nozzle_volume_type: Sequence[str] | None = None,
    filament_map: FilamentMap | None = None,
) -> bytes:
    """Return ``payload`` laid out for ``plate``.

    A 3MF is written at render time, before anyone has chosen a printer, so the
    send path re-places it once the target is known: each plate's build item is
    re-centred on the area every extruder reaches, moved to where that plate sits
    on this printer's project (the plate stride follows the bed, spec §6.4), and
    its prime tower is moved with it. Raises
    :class:`~scadbuddy.render.plate.PlateFitError` when a plate cannot fit that
    printer — before the upload, rather than after Bambuddy's slicer has spent a
    minute finding out.

    ``nozzle_diameter``, when the target names one, replaces the placeholder in
    ``project_settings.config`` (#126), and every filament is mapped to one extruder
    (:func:`one_extruder_map`, #768); ``None`` leaves whatever the file states.

    ``nozzle_stats``, when given, is written as ``extruder_nozzle_stats`` and its newer
    twin ``extruder_nozzle_stats_new``, which the slicer reads first (#834). It is what
    the slicer's "Auto For Flush" grouping actually follows: an extruder stated with no
    nozzle gets no filament. See
    :func:`scadbuddy.bambuddy.extruders.slicer_nozzle_stats`.

    ``nozzle_volume_type``, when given, is each extruder's flow in the slicer's order
    ("Standard" or "High Flow"), the key Bambu Studio writes for a High Flow project
    (#484); the slicer keeps it, since the H2C printer preset states none. See
    :func:`scadbuddy.bambuddy.extruders.slicer_volume_types`. ``default_nozzle_volume_type``
    is left alone, as Bambu Studio leaves it, and ``slice_info.config`` is never written:
    it is the slicer's output.
    """
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
        laid_out = laid_out_plates(archive)
        contents = dict(entries)
        geometry = {
            each.assembly_id: _plate_geometry(archive, each.object_files, contents)
            for each in laid_out
            if each.object_files
        }

    settings: dict[str, Any] = next(
        (json.loads(data) for name, data in entries if name == PROJECT_SETTINGS_NAME), {}
    )
    count = len({each.index for each in laid_out})
    placements: list[Placement] = []
    transforms: dict[str, str] = {}
    rotation = IDENTITY.rsplit(" ", 3)[0]
    for each in laid_out:
        if each.assembly_id not in geometry:
            continue
        bounds, painted = geometry[each.assembly_id]
        try:
            # A tower only for more than one filament, a painting's counted as
            # `PlateParts.tower` counts them (#2108). One object goes by its parts, as
            # it always has; several objects in one colour need none (#956).
            if len(each.assembly_ids) > 1 and each.extruders is not None:
                tower = len(each.extruders | painted) > 1
            else:
                tower = len(each.object_files) > 1
            placement = place_on_plate(bounds, plate, tower=tower)
        except PlateFitError as error:
            if count == 1:
                raise
            raise PlateFitError(f"plate {each.index}: {error}") from error
        placements.append(placement)
        offset = _plate_offset(placement, each.index, count, plate)
        moved = rotation + " " + " ".join(_number(v) for v in offset)
        for assembly_id in each.assembly_ids:
            transforms[assembly_id] = moved

    def _replaced(match: re.Match[str]) -> str:
        tag = match.group(0)
        transform = transforms.get(_attribute(tag, "objectid") or "")
        if transform is None:
            return tag
        if _attribute(tag, "transform") is None:
            raise ValueError("the 3MF's build item carries no transform to re-place")
        return re.sub(r'\btransform="[^"]*"', f'transform="{transform}"', tag, count=1)

    changed: dict[str, bytes] = {}
    for name, data in entries:
        if name == ROOT_MODEL_NAME:
            changed[name] = _ITEM.sub(_replaced, data.decode("utf-8")).encode("utf-8")
        elif name == PROJECT_SETTINGS_NAME:
            # The plate changed, so restate its Z. The other keys the BBL loader
            # needs do not vary by printer and are already in the file.
            settings["printable_height"] = _number(plate.height)
            # Named for this printer and the nozzle the file states, as the writer
            # names it, so a re-placed file is the one written for this printer.
            stated = next(iter(settings.get("nozzle_diameter") or []), None)
            settings["printer_settings_id"] = printer_preset(
                plate, nozzle_diameter or stated or PLACEHOLDER_NOZZLE_DIAMETER[0]
            )
            if nozzle_diameter is not None:
                settings["nozzle_diameter"] = [nozzle_diameter]
                settings.update(one_extruder_map(len(settings.get("filament_colour", []))))
            _state_nozzles(settings, nozzle_stats, nozzle_volume_type, filament_map)
            _set_towers(settings, [placement.tower for placement in placements])
            changed[name] = (json.dumps(settings, indent=4) + "\n").encode("utf-8")
    return _rezipped(payload, changed)


@dataclass(frozen=True)
class FilamentMap:
    """ScadBuddy's own filament→extruder map (#2166, ``bambuddy.nozzle_plan``): per
    filament of the file, its extruder in the slicer's numbering ("1" the left on the
    H2C) and its flow as ``NozzleVolumeType`` ("0" Standard, "1" High Flow)."""

    extruders: tuple[str, ...]
    volumes: tuple[str, ...]


def _state_nozzles(
    settings: dict[str, Any],
    nozzle_stats: Sequence[str] | None,
    nozzle_volume_type: Sequence[str] | None,
    filament_map: FilamentMap | None = None,
) -> None:
    """Write which extruders have the nozzle and each one's flow into ``settings``
    (#834, #484); see :func:`replate_3mf`. ``None`` leaves a key as it is.

    ``filament_map`` is written as ``filament_map`` under ``filament_map_mode``
    "Manual", with ``filament_volume_map`` beside it (#2166): only when it names one
    extruder per filament of the file, since the slicer indexes it by filament."""
    if nozzle_stats is not None:
        settings["extruder_nozzle_stats"] = list(nozzle_stats)
        settings["extruder_nozzle_stats_new"] = list(nozzle_stats)
    if nozzle_volume_type is not None:
        settings["nozzle_volume_type"] = list(nozzle_volume_type)
    if filament_map is not None and len(filament_map.extruders) == len(
        settings.get("filament_colour") or filament_map.extruders
    ):
        settings["filament_map"] = list(filament_map.extruders)
        settings["filament_map_mode"] = "Manual"
        settings["filament_volume_map"] = list(filament_map.volumes)


#: Bambu Studio's full project_settings.config is ~80 KB; more is not one, but a zip bomb.
MAX_SETTINGS_BYTES = 1024 * 1024
#: An untrusted library 3MF (#484) past this is not read at all. The largest real one
#: measured, a sliced multi-plate archive, inflates to 73 MB; a rewrite holds the file
#: and its copy at once, so this is about the most one print may cost in memory.
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
#: A plate's gcode in a 3MF: the file is sliced already.
_GCODE = re.compile(r"Metadata/plate_\d+\.gcode")


def nozzles_statable(payload: bytes) -> bool:
    """Whether :func:`state_nozzles` can rewrite ``payload``: a 3MF whose
    ``project_settings.config`` is Bambu's JSON, and which holds no gcode, so the slicer
    will read those settings (#484). A sliced file is printed as it was sliced, and a 3MF
    from another slicer has no such settings to write into.

    A library file is untrusted, so nothing is inflated past a cap: the declared sizes
    are checked first (the settings against :data:`MAX_SETTINGS_BYTES`, the archive
    against :data:`MAX_UNCOMPRESSED_BYTES`), then every entry is read in bounded chunks,
    which :mod:`zipfile` stops at its declared size, so a header that understates one is
    a CRC failure rather than a bomb. Any of these is not statable: the file prints as
    it is."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if PROJECT_SETTINGS_NAME not in names or any(map(_GCODE.fullmatch, names)):
                return False
            if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
                return False
            if archive.getinfo(PROJECT_SETTINGS_NAME).file_size > MAX_SETTINGS_BYTES:
                return False
            # Reads each entry in 1 MiB chunks and checks its CRC; None when all match.
            if archive.testzip() is not None:
                return False
            settings = _read_capped(archive, PROJECT_SETTINGS_NAME, MAX_SETTINGS_BYTES)
            return isinstance(json.loads(settings), dict)
    except (zipfile.BadZipFile, ValueError):
        return False


class ArchiveTooLargeError(ValueError):
    """An entry, or the whole 3MF, inflated past its cap."""


class TooManyTrianglesError(ValueError):
    """A model file holds more triangles, or more vertices than they could name, than
    the parse was allowed (:func:`parse_model`'s ``max_triangles``); ``kind`` says
    which."""

    def __init__(self, message: str, kind: Literal["triangles", "vertices"]) -> None:
        super().__init__(message)
        self.kind = kind


#: The chunk every untrusted entry is inflated in.
_CHUNK = 1024 * 1024


def _read_capped(archive: zipfile.ZipFile, name: str, cap: int) -> bytes:
    """``name``'s bytes, read in chunks and refused past ``cap``, whatever its header
    declares."""
    data = bytearray()
    with archive.open(name) as entry:
        while chunk := entry.read(_CHUNK):
            data += chunk
            if len(data) > cap:
                raise ArchiveTooLargeError(f"{name} inflates past {cap} bytes")
    return bytes(data)


_MESH = f"{{{CORE_NS}}}mesh"
_VERTICES = f"{{{CORE_NS}}}vertices"
_VERTEX = f"{{{CORE_NS}}}vertex"
_TRIANGLES = f"{{{CORE_NS}}}triangles"
#: The most distinct triangle ``pid`` values one mesh may name. A ``pid`` is a property
#: group's resource id, so a real file names a handful per mesh; each new one costs a
#: dict entry, and an untrusted file could otherwise name one per triangle (#2024).
#: Generous on purpose: far past any slicer's output, far below what memory notices.
MAX_MESH_PIDS = 10_000
_TRIANGLE = f"{{{CORE_NS}}}triangle"
#: PrusaSlicer's per-triangle multi-material painting; Bambu Studio's is
#: `PAINT_ATTRIBUTE`.
_PRUSA_PAINT = "{http://schemas.slic3r.org/3mf/2017/06}mmu_segmentation"


@dataclass(frozen=True)
class MeshArrays:
    """A ``<mesh>``'s vertices and triangles, as arrays rather than an element each."""

    #: ``(n, 3)`` float64.
    vertices: np.ndarray
    #: ``(m, 3)`` int64: each triangle's ``v1``, ``v2``, ``v3``.
    faces: np.ndarray
    #: Each triangle's ``pid`` as an index into `pids`, or -1 when it has none.
    pid: np.ndarray
    pids: tuple[str, ...]
    #: Each triangle's ``p1``, or -1 when it has none.
    p1: np.ndarray
    #: Each triangle's Bambu Studio ``paint_color`` (``""`` for one without), or None
    #: when no triangle carries one (#1965).
    paint: tuple[str, ...] | None
    #: Whether any triangle carries PrusaSlicer's multi-material painting.
    prusa_painted: bool


@dataclass(frozen=True)
class ParsedModel:
    """A 3MF model file: its tree, in which every ``<mesh>``'s ``<vertices>`` and
    ``<triangles>`` are left empty, and those meshes' arrays, by ``<mesh>`` element."""

    root: ET.Element
    meshes: dict[ET.Element, MeshArrays]


class _MeshTarget:
    """An `ET.XMLParser` target building the tree as `ET.TreeBuilder` does, except a
    mesh's ``<vertex>`` and ``<triangle>`` elements, which go straight into arrays: an
    element costs some 500 bytes against the 30-odd of XML it is read from, so a model
    entry at the archive's cap would otherwise take gigabytes (#1949). Arrays cost
    about what the XML does.

    With ``max_triangles`` and ``max_vertices``, the parse stops at the triangle or the
    vertex past them, every mesh counted together: what bounds the arrays' memory,
    whatever the entry's size."""

    def __init__(self, max_triangles: int | None = None, max_vertices: int | None = None) -> None:
        self.builder = ET.TreeBuilder()
        self.meshes: dict[ET.Element, MeshArrays] = {}
        self._max_triangles = max_triangles
        self._max_vertices = max_vertices
        self._triangles = 0
        self._vertices = 0
        self._open: list[ET.Element] = []
        #: Depth inside an element read into the arrays, whose content is dropped.
        self._skipping = 0
        self._reset()

    def _reset(self) -> None:
        self._points = array("d")
        self._faces = array("q")
        self._pid = array("i")
        self._pids: dict[str, int] = {}
        self._p1 = array("q")
        self._paint: list[str] | None = None
        self._prusa_painted = False
        #: The ``<vertices>`` and ``<triangles>`` blocks this mesh has had.
        self._blocks: set[str] = set()

    def _within(self, parent: str) -> bool:
        return len(self._open) >= 2 and self._open[-1].tag == parent and self._open[-2].tag == _MESH

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        if self._skipping:
            self._skipping += 1
        elif tag == _VERTEX and self._within(_VERTICES):
            self._skipping = 1
            self._vertices += 1
            if self._max_vertices is not None and self._vertices > self._max_vertices:
                raise TooManyTrianglesError(
                    f"stores more than {self._max_vertices:,} vertices, three for every"
                    " triangle allowed",
                    "vertices",
                )
            self._points.extend(
                (float(attrs.get("x", 0)), float(attrs.get("y", 0)), float(attrs.get("z", 0)))
            )
        elif tag == _TRIANGLE and self._within(_TRIANGLES):
            self._skipping = 1
            self._triangles += 1
            if self._max_triangles is not None and self._triangles > self._max_triangles:
                raise TooManyTrianglesError(
                    f"stores more than {self._max_triangles:,} triangles", "triangles"
                )
            self._faces.extend(
                (int(attrs.get("v1", 0)), int(attrs.get("v2", 0)), int(attrs.get("v3", 0)))
            )
            pid = attrs.get("pid")
            if pid is not None and pid not in self._pids and len(self._pids) >= MAX_MESH_PIDS:
                raise ValueError(f"a <mesh> names more than {MAX_MESH_PIDS} property groups")
            self._pid.append(-1 if pid is None else self._pids.setdefault(pid, len(self._pids)))
            p1 = attrs.get("p1")
            self._p1.append(-1 if p1 is None else int(p1 or 0))
            code = attrs.get(PAINT_ATTRIBUTE)
            if code and len(code) > MAX_CODE_DIGITS:
                raise ValueError(
                    f"a triangle's {PAINT_ATTRIBUTE} is longer than {MAX_CODE_DIGITS} digits"
                )
            if code and self._paint is None:
                # The faces before the first painted one are unpainted.
                self._paint = [""] * (len(self._p1) - 1)
            if self._paint is not None:
                self._paint.append(code or "")
            self._prusa_painted = self._prusa_painted or bool(attrs.get(_PRUSA_PAINT))
        else:
            if tag == _MESH:
                if any(element.tag == _MESH for element in self._open):
                    raise ValueError("a <mesh> inside another <mesh>")
                self._reset()
            elif tag in (_VERTICES, _TRIANGLES) and self._open and self._open[-1].tag == _MESH:
                # One of each, as the spec has it: a second would be appended to the
                # first, so its indices would not mean what the writer meant (#2024).
                if tag in self._blocks:
                    raise ValueError(f"a <mesh> with more than one <{tag.rpartition('}')[2]}>")
                self._blocks.add(tag)
            self._open.append(self.builder.start(tag, attrs))

    def end(self, tag: str) -> None:
        if self._skipping:
            self._skipping -= 1
            return
        element = self.builder.end(tag)
        self._open.pop()
        if tag == _MESH:
            self.meshes[element] = MeshArrays(
                vertices=np.frombuffer(self._points, dtype=np.float64).reshape(-1, 3),
                faces=np.frombuffer(self._faces, dtype=np.int64).reshape(-1, 3),
                pid=np.frombuffer(self._pid, dtype=np.int32),
                pids=tuple(self._pids),
                p1=np.frombuffer(self._p1, dtype=np.int64),
                paint=tuple(self._paint) if self._paint is not None else None,
                prusa_painted=self._prusa_painted,
            )
            self._reset()

    def data(self, text: str) -> None:
        if not self._skipping:
            self.builder.data(text)

    def close(self) -> ET.Element:
        return self.builder.close()


def parse_model(
    archive: zipfile.ZipFile,
    name: str,
    cap: int | None = MAX_UNCOMPRESSED_BYTES,
    *,
    max_triangles: int | None = None,
    max_vertices: int | None = None,
) -> ParsedModel:
    """Model file ``name``, inflated in chunks and refused past ``cap`` (``None``: no
    cap), parsed as it inflates, its meshes read into arrays (`_MeshTarget`), refused
    past ``max_triangles`` or ``max_vertices`` (:class:`TooManyTrianglesError`) when
    given. ``ET.ParseError`` when it is not XML, ``ValueError`` when a vertex or triangle
    is not numbers."""
    target = _MeshTarget(max_triangles, max_vertices)
    parser = ET.XMLParser(target=target)
    read = 0
    with archive.open(name) as entry:
        while chunk := entry.read(_CHUNK):
            read += len(chunk)
            if cap is not None and read > cap:
                raise ArchiveTooLargeError(f"{name} inflates past {cap} bytes")
            try:
                parser.feed(chunk)
            except OverflowError:
                # An index past 64 bits: as unreadable as one that is not a number.
                raise ValueError(f"{name} has a vertex index out of range") from None
    root: ET.Element = parser.close()
    return ParsedModel(root, target.meshes)


def state_nozzles(
    payload: bytes,
    *,
    nozzle_stats: Sequence[str] | None = None,
    nozzle_volume_type: Sequence[str] | None = None,
    filament_colour: Sequence[str] | None = None,
    filament_map: FilamentMap | None = None,
) -> bytes:
    """``payload`` stating the nozzles as :func:`replate_3mf` does, and the spools'
    ``filament_colour`` (#476), changed in nothing else: an author's file is printed
    where its author placed it, every plate as they laid it out (#313, #484, #1752).
    Colours of another count than the file's filaments are not stated, since its parts'
    extruder numbers index into them. Only for a file :func:`nozzles_statable` accepts.
    The same file and choices always give the same bytes, so a copy uploaded earlier
    can be found by its hash.

    Each entry is copied through in chunks, counted against
    :data:`MAX_UNCOMPRESSED_BYTES`, so no more than a chunk of it is ever inflated in
    memory (:class:`ArchiveTooLargeError` past the cap)."""
    total = 0
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(payload)) as archive,
        zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out,
    ):
        for info in archive.infolist():
            name = info.filename
            if name == PROJECT_SETTINGS_NAME:
                settings = json.loads(_read_capped(archive, name, MAX_SETTINGS_BYTES))
                _state_nozzles(settings, nozzle_stats, nozzle_volume_type, filament_map)
                if filament_colour is not None and len(filament_colour) == len(
                    settings.get("filament_colour") or []
                ):
                    settings["filament_colour"] = list(filament_colour)
                data = (json.dumps(settings, indent=4) + "\n").encode("utf-8")
                total += len(data)
                out.writestr(_entry(name), data)
                continue
            with archive.open(info) as source, out.open(_entry(name), "w") as target:
                while chunk := source.read(_CHUNK):
                    total += len(chunk)
                    if total > MAX_UNCOMPRESSED_BYTES:
                        raise ArchiveTooLargeError(
                            f"the 3MF inflates past {MAX_UNCOMPRESSED_BYTES} bytes"
                        )
                    target.write(chunk)
    return buffer.getvalue()


#: Who laid a 3MF out, which decides how a print lays it out for the printer (#1752):
#: ``scadbuddy`` is re-placed for the printer's plate (:func:`replate_3mf`), ``author``
#: keeps its placement and states the nozzles (:func:`state_nozzles`), and ``as_is`` is
#: sliced as it stands.
Layout = Literal["scadbuddy", "author", "as_is"]


def layout_of(payload: bytes) -> Layout:
    """Who laid ``payload`` out, judged from its bytes alone, whatever they were
    obtained from (a render, or a file in Bambuddy's library).

    ``scadbuddy``: its ``print_settings_id`` is :data:`PRESET_PLACEHOLDER` (its printer
    is named since #2639; Bambu Studio names a process when it saves) and its plates read
    back as this writer lays them out, one assembly per plate. ``author``: any other
    file :func:`nozzles_statable` accepts, a project Bambu Studio (or ScadBuddy, before
    someone re-saved it there) laid out, whose placement and plates are the author's.
    ``as_is``: anything else (sliced, another slicer's, past the caps), which nothing
    can be stated into. The caps are checked first, so the plates are read back only
    from a file already bounded."""
    if not nozzles_statable(payload):
        return "as_is"
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            settings = json.loads(_read_capped(archive, PROJECT_SETTINGS_NAME, MAX_SETTINGS_BYTES))
            if settings.get("print_settings_id") != PRESET_PLACEHOLDER:
                return "author"
            if not all(each.object_files for each in laid_out_plates(archive)):
                return "author"
    except (KeyError, ValueError, ET.ParseError, zipfile.BadZipFile):
        return "author"
    return "scadbuddy"


#: The colour an STL is wrapped in: it carries none, and a print recolors it for its
#: spool (#476).
STL_COLOUR = "#FFFFFF"


def stl_3mf(payload: bytes, *, model_name: str = "model") -> bytes:
    """An STL as ScadBuddy's own 3MF of one plate and one filament (#1752), so a print
    of it is laid out, stated and recolored as a render's is. Without covers: an STL
    has none to keep, and drawing one for an untrusted mesh is not worth its time.
    Raises :class:`ValueError` for bytes that hold no triangles."""
    mesh = trimesh.load(io.BytesIO(payload), file_type="stl", force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        raise ValueError("the STL holds no triangles")
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "model.3mf"
        write_bambu_3mf(
            [ColourPart(1, "Color 1", STL_COLOUR, mesh)],
            out,
            thumbnails=None,
            model_name=model_name,
        )
        return out.read_bytes()


def _entry(name: str) -> zipfile.ZipInfo:
    """An entry as ScadBuddy writes one: a fixed time, and covers stored."""
    info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
    return info


def _rezipped(payload: bytes, changed: Mapping[str, bytes]) -> bytes:
    """``payload`` with each entry in ``changed`` replaced, written in the writer's policy
    (:func:`_entry`), so a replated file is byte-identical to one written for this plate
    directly, covers included.

    An entry left unchanged and already in that policy is copied as its compressed
    bytes (:func:`~scadbuddy.render.zipcopy.copy_raw`, #2942) rather than inflated and
    deflated again: those are the mesh objects, nearly all of a large file."""
    source = io.BytesIO(payload)
    buffer = io.BytesIO()
    raw_copy = raw_copy_checked()
    with (
        zipfile.ZipFile(source) as archive,
        zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out,
    ):
        for info in archive.infolist():
            name = info.filename
            entry = _entry(name)
            if (
                raw_copy
                and name not in changed
                and info.compress_type == entry.compress_type
                and not info.flag_bits & ENCRYPTED
            ):
                copy_raw(source, info, out, date_time=ZIP_TIMESTAMP)
                continue
            out.writestr(entry, changed[name] if name in changed else archive.read(info))
    return buffer.getvalue()
