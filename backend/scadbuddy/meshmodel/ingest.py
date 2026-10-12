"""A file's objects as a scene (spec 2026-10-11 §4): one mesh file per (object, colour
part), with no colour of its own (§3.3), recentred so the object sits on z = 0 about
its own x/y centre; an object read with ``count`` copies becomes that many scene objects
sharing its meshes; the objects start in a row along X, ``gap`` apart, centred on the
origin. A painted part's codes are kept beside its mesh for paint transfer (§7)."""

from __future__ import annotations

import dataclasses
import io
import json
import zipfile

import numpy as np
import trimesh

from scadbuddy.meshmodel.scene import Part, Scene, SceneObject, SceneSource, Transform
from scadbuddy.render.bambu3mf import (
    CORE_NS,
    MODEL_CONTENT_TYPE,
    MODEL_RELATIONSHIP,
    RELS_CONTENT_TYPE,
    ROOT_MODEL_NAME,
    ZIP_TIMESTAMP,
)
from scadbuddy.render.objects3mf import ReadObject

_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    f'<Default Extension="rels" ContentType="{RELS_CONTENT_TYPE}"/>'
    f'<Default Extension="model" ContentType="{MODEL_CONTENT_TYPE}"/></Types>'
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    f'<Relationship Target="/{ROOT_MODEL_NAME}" Id="rel0" Type="{MODEL_RELATIONSHIP}"/>'
    "</Relationships>"
)


def part_mesh_3mf(mesh: trimesh.Trimesh) -> bytes:
    vertices = "".join(
        f'<vertex x="{x!r}" y="{y!r}" z="{z!r}"/>' for x, y, z in mesh.vertices.tolist()
    )
    triangles = "".join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces.tolist()
    )
    model = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{CORE_NS}">'
        f'<resources><object id="1" type="model"><mesh><vertices>{vertices}</vertices>'
        f"<triangles>{triangles}</triangles></mesh></object></resources>"
        '<build><item objectid="1"/></build></model>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in (
            ("[Content_Types].xml", _TYPES),
            ("_rels/.rels", _RELS),
            (ROOT_MODEL_NAME, model),
        ):
            archive.writestr(zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP), text)
    return buffer.getvalue()


def scene_from_objects(
    read: list[ReadObject], source: SceneSource, *, gap: float = 5.0
) -> tuple[Scene, dict[str, bytes]]:
    files: dict[str, bytes] = {}
    objects: list[SceneObject] = []
    widths: list[float] = []
    notes: list[str] = []
    for obj in read:
        # A read object's files are named by the id of its first scene object.
        stem = f"meshes/o{len(objects) + 1}"
        vertices = np.concatenate([np.asarray(p.mesh.vertices) for p in obj.parts])
        low, high = vertices.min(axis=0), vertices.max(axis=0)
        offset = -np.array([(low[0] + high[0]) / 2, (low[1] + high[1]) / 2, low[2]])
        parts: list[Part] = []
        for number, colour_part in enumerate(obj.parts, start=1):
            mesh = colour_part.mesh.copy()
            mesh.apply_translation(offset)
            path = f"{stem}-p{number}.3mf"
            files[path] = part_mesh_3mf(mesh)
            painted = colour_part.paint is not None
            if colour_part.paint is not None:
                files[f"{stem}-p{number}.paint.json"] = json.dumps(
                    dataclasses.asdict(colour_part.paint)
                ).encode()
            parts.append(
                Part(
                    id=f"p{number}",
                    mesh=path,
                    colour=colour_part.colour.upper(),
                    painted=painted,
                    negative=colour_part.negative,
                )
            )
        if any(p.painted for p in parts):
            notes.append(
                f"{obj.name}: painted in Bambu Studio; the paint is kept for printing, "
                "not yet shown in renders"
            )
        notes += [f"{obj.name}: {note}" for note in obj.notes]
        for _ in range(obj.count):
            objects.append(SceneObject(id=f"o{len(objects) + 1}", name=obj.name, parts=parts))
            widths.append(float(high[0] - low[0]))
    total = sum(widths) + gap * (len(widths) - 1)
    x = -total / 2
    placed = []
    for scene_object, width in zip(objects, widths, strict=True):
        placed.append(
            scene_object.model_copy(
                update={"transform": Transform(translate=(x + width / 2, 0.0, 0.0))}
            )
        )
        x += width + gap
    return Scene(source=source, objects=placed, notes=notes), files
