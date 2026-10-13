"""A mesh model's part meshes, read from its directory (spec 2026-10-11 §3): each
``meshes/<object>-<part>.3mf`` as one mesh, its faces in the file's order, which is the
order its ``.paint.json`` codes follow."""

from __future__ import annotations

from scadbuddy.meshmodel.operations import mesh_loader as part_loader

__all__ = ["part_loader"]
