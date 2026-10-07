from __future__ import annotations

import io
import json
import math
import os
import re
import uuid
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

import numpy as np

from scadbuddy.render.plate import (
    DEFAULT_PLATE,
    Placement,
    PlateFitError,
    PlateGeometry,
    centre_on_plate,
    place_on_plate,
)
from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import PlateThumbnails

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
    triangles = "".join(
        f'\n     <triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}"/>' for f in part.mesh.faces
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
    """The ``(2, 3)`` min/max box every part shares, in the meshes' coordinates."""
    bounds = np.array([part.mesh.bounds for part in parts])
    return np.array([bounds[:, 0, :].min(axis=0), bounds[:, 1, :].max(axis=0)])


@dataclass(frozen=True)
class PlateParts:
    """One plate of a 3MF: its colour parts, and the 1-based extruder each prints
    with — an index into the file's one filament list, which every plate shares
    (spec §6.4)."""

    parts: tuple[ColourPart, ...]
    extruders: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.parts:
            raise ValueError("a plate needs at least one colour part")
        if len(self.parts) != len(self.extruders):
            raise ValueError("a plate needs one extruder per colour part")

    @property
    def tower(self) -> bool:
        """Whether this plate needs a prime tower: only when it uses more than one
        filament. One colour prints without one, so reserving room would refuse
        plates that are perfectly printable."""
        return len(set(self.extruders)) > 1


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


def root_model(
    plates: Sequence[PlateParts], model_name: str, offsets: Sequence[Sequence[float]]
) -> str:
    total = sum(len(plate.parts) for plate in plates)
    objects: list[str] = []
    items: list[str] = []
    for plate_index, (ids, offset) in enumerate(
        zip(_object_ids(plates), offsets, strict=True), start=1
    ):
        assembly_id = total + plate_index
        components = "".join(
            f'\n    <component p:path="/3D/Objects/object_{index}.model" objectid="{index}"'
            f' p:UUID="{_uuid(model_name, f"component-{index}")}" transform="{IDENTITY}"/>'
            for index in ids
        )
        name = _assembly_name(model_name, plate_index, len(plates))
        objects.append(
            f'  <object id="{assembly_id}" name={quoteattr(name)} type="model"'
            f' p:UUID="{_uuid(model_name, _numbered("assembly", plate_index))}">\n'
            f"   <components>{components}\n   </components>\n"
            "  </object>\n"
        )
        translation = " ".join(_number(value) for value in offset)
        items.append(
            f'  <item objectid="{assembly_id}" transform="1 0 0 0 1 0 0 0 1 {translation}"'
            f' printable="1" p:UUID="{_uuid(model_name, _numbered("item", plate_index))}"/>\n'
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


def model_settings(plates: Sequence[PlateParts], model_name: str, *, covers: bool) -> str:
    total = sum(len(plate.parts) for plate in plates)
    objects: list[str] = []
    plate_entries: list[str] = []
    for plate_index, (plate, ids) in enumerate(
        zip(plates, _object_ids(plates), strict=True), start=1
    ):
        assembly_id = total + plate_index
        entries = "".join(
            f'  <part id="{index}" subtype="normal_part">\n'
            f'   <metadata key="name" value={quoteattr(part.name)}/>\n'
            f'   <metadata key="extruder" value="{extruder}"/>\n'
            "  </part>\n"
            for index, part, extruder in zip(ids, plate.parts, plate.extruders, strict=True)
        )
        name = _assembly_name(model_name, plate_index, len(plates))
        objects.append(
            f' <object id="{assembly_id}">\n'
            f'  <metadata key="name" value={quoteattr(name)}/>\n'
            f'  <metadata key="extruder" value="{plate.extruders[0]}"/>\n'
            f"{entries}"
            " </object>\n"
        )
        plate_entries.append(
            " <plate>\n"
            f'  <metadata key="plater_id" value="{plate_index}"/>\n'
            '  <metadata key="plater_name" value=""/>\n'
            '  <metadata key="locked" value="false"/>\n'
            f"{_cover_metadata(plate_index) if covers else ''}"
            "  <model_instance>\n"
            f'   <metadata key="object_id" value="{assembly_id}"/>\n'
            '   <metadata key="instance_id" value="0"/>\n'
            "  </model_instance>\n"
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
#: it honestly knows and names itself for the rest, rather than inventing preset
#: names that could be mistaken for real ones — it owns no slicer settings.
PRESET_PLACEHOLDER = "ScadBuddy"
#: Only the arity-free presence of this option matters; 1 and 3 entries were both
#: measured to slice identically on a two-extruder H2C. The print run replaces it
#: with the nozzle it chose (#126): slicing never reads it, but a person deciding
#: whether to start a print does.
PLACEHOLDER_NOZZLE_DIAMETER = ["0.4"]


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
    colours: Sequence[str], placements: Sequence[Placement], plate: PlateGeometry
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
    """
    settings: dict[str, str | list[str]] = {
        "filament_colour": list(colours),
        # Required-or-segfault; see above.
        "printer_settings_id": PRESET_PLACEHOLDER,
        "print_settings_id": PRESET_PLACEHOLDER,
        "filament_settings_id": [PRESET_PLACEHOLDER for _ in colours],
        "nozzle_diameter": PLACEHOLDER_NOZZLE_DIAMETER,
        "printable_height": _number(plate.height),
    }
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
            (PROJECT_SETTINGS_NAME, project_settings(colours, placements, plate)),
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


def _vertex_bounds(archive: zipfile.ZipFile, names: Sequence[str]) -> np.ndarray:
    """The ``(2, 3)`` box of the object files ``names``, read back out of an archive.

    The component transforms this writer emits are the identity, so the vertex
    extents *are* the assembly's extents; the build item carries the whole
    placement. Reading them back rather than trusting a recorded plate size keeps
    :func:`replate_3mf` correct for files written before plates were per printer.
    """
    lows: list[np.ndarray] = []
    highs: list[np.ndarray] = []
    for name in names:
        root = ET.fromstring(archive.read(name))
        for node in root.iter(f"{{{CORE_NS}}}vertices"):
            points = np.array(
                [[float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))] for v in node],
                dtype=np.float64,
            )
            if points.size:
                lows.append(points.min(axis=0))
                highs.append(points.max(axis=0))
    if not lows:
        raise ValueError("the 3MF has no object geometry to place")
    return np.array([np.min(lows, axis=0), np.max(highs, axis=0)])


@dataclass(frozen=True)
class LaidOutPlate:
    """What :func:`replate_3mf` needs of one plate of a written archive."""

    index: int
    assembly_id: str
    object_files: tuple[str, ...]


def laid_out_plates(archive: zipfile.ZipFile) -> list[LaidOutPlate]:
    """Each plate of a ScadBuddy archive: its index, its build item's assembly and
    the object files that assembly is made of.

    Plates are read from ``model_settings.config`` (``plater_id`` → the assembly its
    ``model_instance`` names), and a file with no such list is one plate holding
    every build item — the shape of every 3MF written before plates were numbered.
    """
    names = set(archive.namelist())
    root = ET.fromstring(archive.read(ROOT_MODEL_NAME))
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

    plates: list[tuple[int, str]] = []
    if MODEL_SETTINGS_NAME in names:
        config = ET.fromstring(archive.read(MODEL_SETTINGS_NAME))
        for each in plate_settings(config):
            if each.object_id is not None and each.object_id in items:
                plates.append((each.index, each.object_id))
    if len(plates) != len(items):
        # Not a layout this writer produced plate by plate: one plate, placed by its
        # first build item, holding every object file.
        every = tuple(sorted(name for name in names if name.startswith("3D/Objects/")))
        return [LaidOutPlate(1, items[0], every)]
    return [
        LaidOutPlate(index, object_id, components.get(object_id, ()))
        for index, object_id in sorted(plates)
    ]


def replate_3mf(
    payload: bytes,
    plate: PlateGeometry,
    *,
    nozzle_diameter: str | None = None,
    nozzle_stats: Sequence[str] | None = None,
    nozzle_volume_type: Sequence[str] | None = None,
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
        bounds = {
            each.assembly_id: _vertex_bounds(archive, each.object_files)
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
        if each.assembly_id not in bounds:
            continue
        try:
            placement = place_on_plate(
                bounds[each.assembly_id], plate, tower=len(each.object_files) > 1
            )
        except PlateFitError as error:
            if count == 1:
                raise
            raise PlateFitError(f"plate {each.index}: {error}") from error
        placements.append(placement)
        offset = _plate_offset(placement, each.index, count, plate)
        transforms[each.assembly_id] = rotation + " " + " ".join(_number(v) for v in offset)

    def _replaced(match: re.Match[str]) -> str:
        tag = match.group(0)
        transform = transforms.get(_attribute(tag, "objectid") or "")
        if transform is None:
            return tag
        if _attribute(tag, "transform") is None:
            raise ValueError("the 3MF's build item carries no transform to re-place")
        return re.sub(r'\btransform="[^"]*"', f'transform="{transform}"', tag, count=1)

    rewritten: list[tuple[str, bytes]] = []
    for name, data in entries:
        if name == ROOT_MODEL_NAME:
            data = _ITEM.sub(_replaced, data.decode("utf-8")).encode("utf-8")
        elif name == PROJECT_SETTINGS_NAME:
            # The plate changed, so restate its Z. The other keys the BBL loader
            # needs do not vary by printer and are already in the file.
            settings["printable_height"] = _number(plate.height)
            if nozzle_diameter is not None:
                settings["nozzle_diameter"] = [nozzle_diameter]
                settings.update(one_extruder_map(len(settings.get("filament_colour", []))))
            if nozzle_stats is not None:
                settings["extruder_nozzle_stats"] = list(nozzle_stats)
                settings["extruder_nozzle_stats_new"] = list(nozzle_stats)
            if nozzle_volume_type is not None:
                settings["nozzle_volume_type"] = list(nozzle_volume_type)
            _set_towers(settings, [placement.tower for placement in placements])
            data = (json.dumps(settings, indent=4) + "\n").encode("utf-8")
        rewritten.append((name, data))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in rewritten:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            # Same policy as the writer, so a replated file is byte-identical to
            # one written for this plate directly — covers included.
            info.compress_type = (
                zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
            )
            out.writestr(info, data)
    return buffer.getvalue()
