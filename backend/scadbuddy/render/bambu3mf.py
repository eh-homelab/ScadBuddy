from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
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

# One plate, so every index below is 1. These names are Bambu Studio's formats
# (`THUMBNAIL_FILE_FORMAT` and friends in `bbs_3mf.hpp`) with the plate index
# substituted, and `PLATE_THUMBNAIL` is the entry Bambuddy's `ThreeMFParser`
# reads for a library file's `thumbnail_path`.
PLATE_THUMBNAIL = "Metadata/plate_1.png"
PLATE_THUMBNAIL_SMALL = "Metadata/plate_1_small.png"
PLATE_TOP = "Metadata/top_1.png"
PLATE_PICK = "Metadata/pick_1.png"

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


def _placement(parts: Sequence[ColourPart], plate: PlateGeometry) -> Placement:
    # One colour prints without a prime tower, so reserving room for one would
    # refuse plates that are perfectly printable.
    bounds = model_bounds(parts)
    try:
        return place_on_plate(bounds, plate, tower=len(parts) > 1)
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


def root_model(parts: Sequence[ColourPart], model_name: str, offset: Sequence[float]) -> str:
    assembly_id = len(parts) + 1
    components = "".join(
        f'\n    <component p:path="/3D/Objects/object_{index}.model" objectid="{index}"'
        f' p:UUID="{_uuid(model_name, f"component-{index}")}" transform="{IDENTITY}"/>'
        for index in range(1, len(parts) + 1)
    )
    translation = " ".join(_number(value) for value in offset)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}" xmlns:p="{PRODUCTION_NS}">\n'
        f' <metadata name="Application">{BAMBU_APPLICATION}</metadata>\n'
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
        ' <metadata name="Origin">ScadBuddy</metadata>\n'
        f' <metadata name="Title">{escape(model_name)}</metadata>\n'
        " <resources>\n"
        f'  <object id="{assembly_id}" name={quoteattr(model_name)} type="model"'
        f' p:UUID="{_uuid(model_name, "assembly")}">\n'
        f"   <components>{components}\n   </components>\n"
        "  </object>\n"
        " </resources>\n"
        f' <build p:UUID="{_uuid(model_name, "build")}">\n'
        f'  <item objectid="{assembly_id}" transform="1 0 0 0 1 0 0 0 1 {translation}"'
        f' printable="1" p:UUID="{_uuid(model_name, "item")}"/>\n'
        " </build>\n"
        "</model>\n"
    )


def model_settings(parts: Sequence[ColourPart], model_name: str, *, covers: bool) -> str:
    assembly_id = len(parts) + 1
    entries = "".join(
        f'  <part id="{index}" subtype="normal_part">\n'
        f'   <metadata key="name" value={quoteattr(part.name)}/>\n'
        f'   <metadata key="extruder" value="{index}"/>\n'
        "  </part>\n"
        for index, part in enumerate(parts, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<config>\n"
        f' <object id="{assembly_id}">\n'
        f'  <metadata key="name" value={quoteattr(model_name)}/>\n'
        '  <metadata key="extruder" value="1"/>\n'
        f"{entries}"
        " </object>\n"
        " <plate>\n"
        '  <metadata key="plater_id" value="1"/>\n'
        '  <metadata key="plater_name" value=""/>\n'
        '  <metadata key="locked" value="false"/>\n'
        f"{_cover_metadata() if covers else ''}"
        "  <model_instance>\n"
        f'   <metadata key="object_id" value="{assembly_id}"/>\n'
        '   <metadata key="instance_id" value="0"/>\n'
        "  </model_instance>\n"
        " </plate>\n"
        "</config>\n"
    )


def _cover_metadata() -> str:
    return (
        f'  <metadata key="thumbnail_file" value="{PLATE_THUMBNAIL}"/>\n'
        f'  <metadata key="top_file" value="{PLATE_TOP}"/>\n'
        f'  <metadata key="pick_file" value="{PLATE_PICK}"/>\n'
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
#: measured to slice identically on a two-extruder H2C. The send path replaces it
#: with the target pipeline's real nozzle when it can name one (#126): slicing never
#: reads it, but a person deciding whether to start a print does.
PLACEHOLDER_NOZZLE_DIAMETER = ["0.4"]


def project_settings(
    parts: Sequence[ColourPart], placement: Placement, plate: PlateGeometry
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
    """
    settings: dict[str, str | list[str]] = {
        "filament_colour": [part.colour for part in parts],
        # Required-or-segfault; see above.
        "printer_settings_id": PRESET_PLACEHOLDER,
        "print_settings_id": PRESET_PLACEHOLDER,
        "filament_settings_id": [PRESET_PLACEHOLDER for _ in parts],
        "nozzle_diameter": PLACEHOLDER_NOZZLE_DIAMETER,
        "printable_height": _number(plate.height),
    }
    if placement.tower is not None:
        settings["wipe_tower_x"] = [_number(placement.tower[0])]
        settings["wipe_tower_y"] = [_number(placement.tower[1])]
    return json.dumps(settings, indent=4) + "\n"


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


def write_bambu_3mf(
    parts: Sequence[ColourPart],
    out_path: Path,
    *,
    thumbnails: PlateThumbnails | None,
    model_name: str = "model",
    plate: PlateGeometry = DEFAULT_PLATE,
) -> None:
    """Write the 3MF laid out for ``plate``.

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
    covers = thumbnails is not None
    placement = _placement(parts, plate)
    offset = placement.offset
    entries: list[tuple[str, bytes]] = [
        (name, payload.encode("utf-8"))
        for name, payload in (
            ("[Content_Types].xml", _content_types(covers=covers)),
            ("_rels/.rels", _package_rels(covers=covers)),
            (ROOT_MODEL_NAME, root_model(parts, model_name, offset)),
            ("3D/_rels/3dmodel.model.rels", _model_rels(len(parts))),
            *(
                (f"3D/Objects/object_{index}.model", object_model(part, index))
                for index, part in enumerate(parts, start=1)
            ),
            ("Metadata/model_settings.config", model_settings(parts, model_name, covers=covers)),
            (PROJECT_SETTINGS_NAME, project_settings(parts, placement, plate)),
        )
    ]
    if thumbnails is not None:
        entries += [
            (PLATE_THUMBNAIL, thumbnails.plate),
            (PLATE_THUMBNAIL_SMALL, thumbnails.plate_small),
            (PLATE_TOP, thumbnails.top),
            (PLATE_PICK, thumbnails.pick),
        ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            # PNG is already a deflate stream; re-deflating it is pure CPU for
            # nothing, and Studio stores its own thumbnails uncompressed too.
            info.compress_type = (
                zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
            )
            archive.writestr(info, payload)


_ITEM_TRANSFORM = re.compile(r'(<item\b[^>]*?\btransform=")([^"]*)(")')


def _object_bounds(archive: zipfile.ZipFile) -> np.ndarray:
    """The ``(2, 3)`` box of every part, read back out of a written archive.

    The component transforms this writer emits are the identity, so the vertex
    extents *are* the assembly's extents; the build item carries the whole
    placement. Reading them back rather than trusting a recorded plate size keeps
    :func:`replate_3mf` correct for files written before plates were per printer.
    """
    lows: list[np.ndarray] = []
    highs: list[np.ndarray] = []
    for name in archive.namelist():
        if not (name.startswith("3D/Objects/") and name.endswith(".model")):
            continue
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


def replate_3mf(
    payload: bytes, plate: PlateGeometry, *, nozzle_diameter: str | None = None
) -> bytes:
    """Return ``payload`` laid out for ``plate``.

    A 3MF is written at render time, before anyone has chosen a printer, so the
    send path re-places it once the target is known: the build item is re-centred
    on the area every extruder reaches and the prime tower is moved with it.
    Raises :class:`~scadbuddy.render.plate.PlateFitError` when the model cannot
    fit that printer — before the upload, rather than after Bambuddy's slicer has
    spent a minute finding out.

    ``nozzle_diameter``, when the target names one, replaces the placeholder in
    ``project_settings.config`` (#126); ``None`` leaves whatever the file states.
    """
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
        bounds = _object_bounds(archive)

    settings: dict[str, Any] = next(
        (json.loads(data) for name, data in entries if name == PROJECT_SETTINGS_NAME), {}
    )
    colours = settings.get("filament_colour") or []
    placement = place_on_plate(bounds, plate, tower=len(colours) > 1)

    rewritten: list[tuple[str, bytes]] = []
    for name, data in entries:
        if name == ROOT_MODEL_NAME:
            rotation = IDENTITY.rsplit(" ", 3)[0]
            transform = rotation + " " + " ".join(_number(value) for value in placement.offset)
            text = data.decode("utf-8")
            match = _ITEM_TRANSFORM.search(text)
            if match is None:
                raise ValueError("the 3MF's build item carries no transform to re-place")
            data = (text[: match.start(2)] + transform + text[match.end(2) :]).encode("utf-8")
        elif name == PROJECT_SETTINGS_NAME:
            settings.pop("wipe_tower_x", None)
            settings.pop("wipe_tower_y", None)
            # The plate changed, so restate its Z. The other keys the BBL loader
            # needs do not vary by printer and are already in the file.
            settings["printable_height"] = _number(plate.height)
            if nozzle_diameter is not None:
                settings["nozzle_diameter"] = [nozzle_diameter]
            if placement.tower is not None:
                settings["wipe_tower_x"] = [_number(placement.tower[0])]
                settings["wipe_tower_y"] = [_number(placement.tower[1])]
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
