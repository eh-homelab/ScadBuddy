# models/mesh-joint-demo/make.py
"""Build this template from scratch (#2146): a two-colour carrot made with trimesh, read
into a scene, split at the neck with a keyed peg on the body and the socket in the
leaves, the leaves laid on their side, where the leaves sit exposed. Run from backend/:
`uv run --frozen python ../models/mesh-joint-demo/make.py`."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import trimesh
from pydantic import TypeAdapter

from scadbuddy.meshmodel.edits import Edit, apply_edits
from scadbuddy.meshmodel.ingest import scene_from_objects
from scadbuddy.meshmodel.operations import compiled
from scadbuddy.meshmodel.scene import SCENE_NAME, SceneSource, dump_scene
from scadbuddy.render.objects3mf import ReadObject
from scadbuddy.render.split import ColourPart

HERE = Path(__file__).resolve().parent


def revolved(profile: list[tuple[float, float]]) -> trimesh.Trimesh:
    return trimesh.creation.revolve(np.array(profile, dtype=float), sections=64)


def main() -> None:
    body = revolved([(0, 0), (10, 0), (10, 30), (3, 30), (0, 30)])
    leaves = revolved([(0, 30), (3, 30), (3, 35), (6, 35), (6, 45), (0, 45)])
    read = [ReadObject(name="Carrot", count=1, parts=[
        ColourPart(0, "Body", "#F08A24", body), ColourPart(1, "Leaves", "#3A7D2C", leaves),
    ])]
    source = SceneSource(kind="upload", name="carrot (made by make.py)", sha256="0" * 64)
    scene, files = scene_from_objects(read, source)
    for name, data in files.items():
        (HERE / name).parent.mkdir(parents=True, exist_ok=True)
        (HERE / name).write_bytes(data)

    def mesh_of(path: str) -> trimesh.Trimesh:
        return trimesh.load(io.BytesIO((HERE / path).read_bytes()), file_type="3mf", force="mesh")

    edits = TypeAdapter(list[Edit])
    scene = apply_edits(scene, edits.validate_python([
        {"edit": "split_at_plane", "object": "o1", "point": [0, 0, 32], "normal": [0, 0, 1],
         "joint": {"depth": 4, "clearance": 0.2, "key": "flat", "peg_on": "below"}},
        # The leaves on their side, so layer lines run along them, not across the neck.
        {"edit": "lay_flat", "object": "o2", "normal": [1, 0, 0]},
    ]), mesh_of)
    # Beside the body, keeping the height lay_flat dropped it to. The leaves turn about
    # the origin and lie along -X from translate.x - 32 to translate.x - 45, so 70 puts
    # them at x 25..38, clear of the body (r=10).
    dropped = scene.objects[1].transform.translate[2]
    scene = apply_edits(scene, edits.validate_python([
        {"edit": "transform", "object": "o2", "translate": [70, 0, dropped]},
        # Where the leaves sit on the bed: a parameter that cannot break the joint.
        {"edit": "expose", "object": "o2", "op": None, "field": "translate.0", "name": "leaves_x"},
    ]), mesh_of)
    scene, text = compiled(scene)
    (HERE / SCENE_NAME).write_text(dump_scene(scene))
    (HERE / "model.scad").write_text(text)


if __name__ == "__main__":
    main()
