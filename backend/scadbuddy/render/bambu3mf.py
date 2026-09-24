from __future__ import annotations

import json
import uuid
import zipfile
from collections.abc import Sequence
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import numpy as np

from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import render_plate_thumbnails

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

DEFAULT_PLATE_SIZE = (256.0, 256.0)
UUID_NAMESPACE = uuid.UUID("2f0c5f8e-6c1a-5d3b-9a7f-4f2d8b1c6e30")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
IDENTITY = "1 0 0 0 1 0 0 0 1 0 0 0"


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


def _plate_offset(
    parts: Sequence[ColourPart], plate_size: tuple[float, float]
) -> tuple[float, ...]:
    bounds = np.array([part.mesh.bounds for part in parts])
    low = bounds[:, 0, :].min(axis=0)
    high = bounds[:, 1, :].max(axis=0)
    return (
        plate_size[0] / 2 - (low[0] + high[0]) / 2,
        plate_size[1] / 2 - (low[1] + high[1]) / 2,
        -low[2],
    )


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
        f'  <metadata key="thumbnail_file" value="{PLATE_THUMBNAIL}"/>\n'
        f'  <metadata key="top_file" value="{PLATE_TOP}"/>\n'
        f'  <metadata key="pick_file" value="{PLATE_PICK}"/>\n'
        "  <model_instance>\n"
        f'   <metadata key="object_id" value="{assembly_id}"/>\n'
        '   <metadata key="instance_id" value="0"/>\n'
        "  </model_instance>\n"
        " </plate>\n"
        "</config>\n"
    )


def project_settings(parts: Sequence[ColourPart]) -> str:
    return json.dumps({"filament_colour": [part.colour for part in parts]}, indent=4) + "\n"


def _content_types() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        f' <Default Extension="rels" ContentType="{RELS_CONTENT_TYPE}"/>\n'
        f' <Default Extension="model" ContentType="{MODEL_CONTENT_TYPE}"/>\n'
        f' <Default Extension="png" ContentType="{PNG_CONTENT_TYPE}"/>\n'
        "</Types>\n"
    )


def _package_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        f' <Relationship Id="rel-1" Type="{MODEL_RELATIONSHIP}" Target="/3D/3dmodel.model"/>\n'
        f' <Relationship Id="rel-2" Type="{THUMBNAIL_RELATIONSHIP}"'
        f' Target="/{PLATE_THUMBNAIL}"/>\n'
        f' <Relationship Id="rel-4" Type="{COVER_MIDDLE_RELATIONSHIP}"'
        f' Target="/{PLATE_THUMBNAIL}"/>\n'
        f' <Relationship Id="rel-5" Type="{COVER_SMALL_RELATIONSHIP}"'
        f' Target="/{PLATE_THUMBNAIL_SMALL}"/>\n'
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
    plate_size: tuple[float, float] = DEFAULT_PLATE_SIZE,
) -> None:
    if not parts:
        raise ValueError("a 3MF needs at least one colour part")
    offset = _plate_offset(parts, plate_size)
    thumbnails = render_plate_thumbnails(parts)
    entries: list[tuple[str, bytes]] = [
        (name, payload.encode("utf-8"))
        for name, payload in (
            ("[Content_Types].xml", _content_types()),
            ("_rels/.rels", _package_rels()),
            ("3D/3dmodel.model", root_model(parts, model_name, offset)),
            ("3D/_rels/3dmodel.model.rels", _model_rels(len(parts))),
            *(
                (f"3D/Objects/object_{index}.model", object_model(part, index))
                for index, part in enumerate(parts, start=1)
            ),
            ("Metadata/model_settings.config", model_settings(parts, model_name)),
            ("Metadata/project_settings.config", project_settings(parts)),
        )
    ]
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
