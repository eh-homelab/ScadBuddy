"""A mesh model's part meshes, read from its directory (spec 2026-10-11 §3): each
``meshes/<object>-<part>.3mf`` as one mesh, its faces in the file's order, which is the
order its ``.paint.json`` codes follow. A leaf module: `paint.py`, imported by
`render/jobs.py`, reads meshes through it, so it must not pull in the API."""

from __future__ import annotations

import io
from pathlib import Path

import trimesh

from scadbuddy.meshmodel.shared import MeshOf, SceneEditError
from scadbuddy.render.read_budget import ReadBudget


def mesh_loader(directory: Path) -> MeshOf:
    """Each part mesh read once per run."""
    cache: dict[str, trimesh.Trimesh] = {}

    def mesh_of(path: str) -> trimesh.Trimesh:
        if path not in cache:
            cache[path] = _load(path, (directory / path).read_bytes())
        return cache[path]

    return mesh_of


def _load(path: str, data: bytes) -> trimesh.Trimesh:
    """A part file read with the streaming reader, then welded as `trimesh.load`'s
    process=True welds it: vertices merged, every face kept in its order (#7397). A part
    file is one object of one uncoloured mesh (`ingest.py` `part_mesh_3mf`); a file of
    any other shape is loaded by `trimesh.load`, as every part file was before. That
    load parses the whole XML tree: a 228 MB part file raised the process's RSS by
    2.9 GB, the reader by about 0.1 GB."""
    # Imported here: `objects3mf` imports `render/jobs.py`, which imports this module
    # through `paint.py`.
    from scadbuddy.render.objects3mf import UnreadableObjectsError, read_objects

    try:
        # The widest budget: a part file was read within the model's budget when it was
        # made, and its load must not be refused where trimesh's was not.
        read = read_objects(data, ReadBudget.widest())
    except UnreadableObjectsError:
        read = None
    if read is not None and len(read) == 1 and read[0].count == 1 and len(read[0].parts) == 1:
        part = read[0].parts[0]
        if part.paint is None and not part.negative:
            mesh = part.mesh
            mesh.merge_vertices()
            return mesh
    loaded = trimesh.load(io.BytesIO(data), file_type="3mf", force="mesh")
    if not isinstance(loaded, trimesh.Trimesh):
        raise SceneEditError(f"{path} holds no mesh")
    return loaded
