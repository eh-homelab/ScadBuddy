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

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PRODUCTION_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
MODEL_RELATIONSHIP = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
MODEL_CONTENT_TYPE = "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"
RELS_CONTENT_TYPE = "application/vnd.openxmlformats-package.relationships+xml"

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
        if plate is not DEFAULT_PLATE:
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
        f' <metadata name="Application">ScadBuddy</metadata>\n'
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


def model_settings(parts: Sequence[ColourPart], model_name: str) -> str:
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
        "  <model_instance>\n"
        f'   <metadata key="object_id" value="{assembly_id}"/>\n'
        '   <metadata key="instance_id" value="0"/>\n'
        "  </model_instance>\n"
        " </plate>\n"
        "</config>\n"
    )


def project_settings(parts: Sequence[ColourPart], placement: Placement) -> str:
    """``Metadata/project_settings.config``, in Bambu Studio's own JSON shape.

    ``ConfigBase::save_to_json`` writes every vector option as an array of
    *stringified* values, and ``wipe_tower_x``/``wipe_tower_y`` are per-plate
    ``coFloats`` holding the tower's front-left corner — hence ``["145"]`` rather
    than ``[145.0]``.

    Note what this file is and is not worth: Bambu Studio only reads it when the
    3MF's ``Application`` metadata names BambuStudio (``bbs_3mf.cpp`` sets
    ``dont_load_config`` otherwise and drops the whole config), so for a file
    ScadBuddy writes the slicer falls back to ``PrintConfig.cpp``'s defaults and
    these keys are inert — see #105. Bambuddy itself does read the file, and the
    position recorded here is the one the placement above reserved room for, so
    it is written as the honest record of where the tower belongs.
    """
    settings: dict[str, list[str]] = {"filament_colour": [part.colour for part in parts]}
    if placement.tower is not None:
        settings["wipe_tower_x"] = [_number(placement.tower[0])]
        settings["wipe_tower_y"] = [_number(placement.tower[1])]
    return json.dumps(settings, indent=4) + "\n"


def _content_types() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        f' <Default Extension="rels" ContentType="{RELS_CONTENT_TYPE}"/>\n'
        f' <Default Extension="model" ContentType="{MODEL_CONTENT_TYPE}"/>\n'
        "</Types>\n"
    )


def _package_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        f' <Relationship Id="rel-1" Type="{MODEL_RELATIONSHIP}" Target="/3D/3dmodel.model"/>\n'
        "</Relationships>\n"
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
    model_name: str = "model",
    plate: PlateGeometry = DEFAULT_PLATE,
) -> None:
    """Write the 3MF laid out for ``plate``.

    The render pipeline has no printer yet, so it writes against
    :data:`~scadbuddy.render.plate.DEFAULT_PLATE`; :func:`replate_3mf` moves the
    result onto the real one when the send path learns which printer it is for.
    """
    if not parts:
        raise ValueError("a 3MF needs at least one colour part")
    placement = _placement(parts, plate)
    offset = placement.offset
    entries: list[tuple[str, str]] = [
        ("[Content_Types].xml", _content_types()),
        ("_rels/.rels", _package_rels()),
        (ROOT_MODEL_NAME, root_model(parts, model_name, offset)),
        ("3D/_rels/3dmodel.model.rels", _model_rels(len(parts))),
    ]
    entries += [
        (f"3D/Objects/object_{index}.model", object_model(part, index))
        for index, part in enumerate(parts, start=1)
    ]
    entries += [
        ("Metadata/model_settings.config", model_settings(parts, model_name)),
        (PROJECT_SETTINGS_NAME, project_settings(parts, placement)),
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
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


def replate_3mf(payload: bytes, plate: PlateGeometry) -> bytes:
    """Return ``payload`` laid out for ``plate``.

    A 3MF is written at render time, before anyone has chosen a printer, so the
    send path re-places it once the target is known: the build item is re-centred
    on the area every extruder reaches and the prime tower is moved with it.
    Raises :class:`~scadbuddy.render.plate.PlateFitError` when the model cannot
    fit that printer — before the upload, rather than after Bambuddy's slicer has
    spent a minute finding out.
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
            if placement.tower is not None:
                settings["wipe_tower_x"] = [_number(placement.tower[0])]
                settings["wipe_tower_y"] = [_number(placement.tower[1])]
            data = (json.dumps(settings, indent=4) + "\n").encode("utf-8")
        rewritten.append((name, data))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in rewritten:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            out.writestr(info, data)
    return buffer.getvalue()
