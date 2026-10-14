"""A mesh model's part meshes, read from its directory (spec 2026-10-11 §3): each
``meshes/<object>-<part>.3mf`` as one mesh, its faces in the file's order, which is the
order its ``.paint.json`` codes follow. A leaf module: `paint.py`, imported by
`render/jobs.py`, reads meshes through it, so it must not pull in the API."""

from __future__ import annotations

import io
from pathlib import Path

import trimesh

from scadbuddy.meshmodel.shared import MeshOf, SceneEditError


def mesh_loader(directory: Path) -> MeshOf:
    """Each part mesh read once per run."""
    cache: dict[str, trimesh.Trimesh] = {}

    def mesh_of(path: str) -> trimesh.Trimesh:
        if path not in cache:
            data = (directory / path).read_bytes()
            mesh = trimesh.load(io.BytesIO(data), file_type="3mf", force="mesh")
            if not isinstance(mesh, trimesh.Trimesh):
                raise SceneEditError(f"{path} holds no mesh")
            cache[path] = mesh
        return cache[path]

    return mesh_of
