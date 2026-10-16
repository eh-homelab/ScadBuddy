"""Mesh models from real MakerWorld files (#2146). Without `git lfs pull` these skip."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh
from pydantic import TypeAdapter

from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.edits import Edit, apply_edits
from scadbuddy.meshmodel.ingest import scene_from_objects
from scadbuddy.meshmodel.locate import object_at
from scadbuddy.meshmodel.meshes import mesh_loader
from scadbuddy.meshmodel.paint import placement
from scadbuddy.meshmodel.scene import Scene, SceneObject, SceneSource, value
from scadbuddy.meshmodel.section import Section, measure
from scadbuddy.render.geometry import analyze_geometry
from scadbuddy.render.objects3mf import ReadObject, read_objects
from scadbuddy.render.read_budget import ReadBudget
from scadbuddy.render.split import ColourPart
from tests.support.fixtures import makerworld, makerworld_path
from tests.test_mesh_models_openscad import mesh_of, render

pytestmark = pytest.mark.requires_fixtures
EDITS = TypeAdapter(list[Edit])
READ = [e for e in makerworld() if "refused" not in e["arrange_reads"]]
CARROT = "688-carrot-garden.3mf"
GREEN = "#0ACC38"  # the painted leaves' filament (manifest: filament_colours)


def opened(name: str, directory: Path) -> tuple[list[ReadObject], Scene]:
    data = makerworld_path(name).read_bytes()
    read = read_objects(data, ReadBudget())
    scene, files = scene_from_objects(read, SceneSource(kind="upload", name=name, sha256="ab" * 32))
    for path, body in files.items():
        (directory / path).parent.mkdir(parents=True, exist_ok=True)
        (directory / path).write_bytes(body)
    return read, scene


@pytest.mark.parametrize("entry", READ, ids=lambda e: e["file"].removesuffix(".3mf"))
def test_every_readable_fixture_opens_with_its_objects_parts_and_paint(
    entry: dict[str, Any], tmp_path: Path
) -> None:
    read, scene = opened(entry["file"], tmp_path)
    assert len(scene.objects) == entry["arrange_reads"]["objects"]
    assert [len(o.parts) for o in scene.objects] == [
        len(o.parts) for o in read for _ in range(o.count)
    ]
    painted = sum(p.painted for o in scene.objects for p in o.parts)
    assert painted == entry["arrange_reads"]["painted_parts"]
    negatives = sum(p.negative for o in scene.objects for p in o.parts)
    assert negatives == entry["arrange_reads"]["negative_parts"]
    # Real files are not all closed (the frogs' bodies have six edges shared by four
    # faces), so a part's file is held to what was read, not to watertight: the same
    # faces, and closed exactly when the source part is once welded the way a load welds
    # it (a pinched vertex stays two in the source's index space and one after the weld).
    load = mesh_of(tmp_path)
    first = 0
    for obj in read:
        for source, part in zip(obj.parts, scene.objects[first].parts, strict=True):
            mesh = load(part.mesh)
            welded = source.mesh.copy()
            welded.merge_vertices()
            assert len(mesh.faces) == len(welded.faces), f"{obj.name} {part.id}"
            assert mesh.is_watertight == welded.is_watertight, f"{obj.name} {part.id}"
        first += obj.count
    assert "module o1()" in compile_scene(scene)


def carrot_neck(directory: Path, scene: Scene) -> tuple[SceneObject, Any, float, Section]:
    """The small carrot, its mesh, and its neck: the narrowest section where the painted
    leaves meet the body, searched from a little below the lowest painted face to a little
    above it, so a leaf's own thin section is never taken for it."""
    load = mesh_of(directory)
    small = next(o for o in scene.objects if o.name.startswith("Small Carrot"))
    mesh = load(small.parts[0].mesh)
    codes = json.loads(
        (directory / small.parts[0].mesh.replace(".3mf", ".paint.json")).read_text()
    )["codes"]
    # The codes are the faces', in order. This leans on trimesh's default process=True load
    # merging vertices but dropping no faces and keeping their order; a trimesh bump that
    # changes that fails here (#2516).
    assert len(codes) == len(mesh.faces)
    painted = np.array([bool(code) for code in codes])
    leaves_from = float(mesh.triangles_center[painted][:, 2].min())
    sections = {
        z: measure(mesh, [0.0, 0.0, z], [0.0, 0.0, 1.0])
        for z in np.arange(leaves_from - 8.0, leaves_from + 4.0, 0.5)
    }
    z, neck = min(
        ((z, s) for z, s in sections.items() if s is not None and s.inside),
        key=lambda zs: zs[1].room,
    )
    # A neck, not just any section: narrower than the body just below it. Measured on this
    # fixture (the small carrot is 58 mm tall, painted from z = 30.4): the body is a solid
    # disc 2.5 mm below the neck (room 13.8 at z = 28), and hollow further down, where the
    # axis is outside the section, so the comparison point is close, not 6 mm away.
    below = measure(mesh, [0.0, 0.0, z - 2.5], [0.0, 0.0, 1.0])
    assert below is not None and below.inside and neck.room < below.room
    return small, mesh, float(z), neck


def neck_anchor(small: SceneObject, z: float, neck: Section) -> dict[str, Any]:
    """The edit that anchors the small carrot in green through its neck."""
    # Ingest places an object by translation only, so object to world is that shift
    # (`_primitive` solves with the full matrix; this relies on rotate 0, scale 1).
    m = small.transform
    assert [value(v) for v in m.rotate] == [0.0, 0.0, 0.0]
    assert [value(v) for v in m.scale] == [1.0, 1.0, 1.0]
    return {
        "edit": "add",
        "object": small.id,
        "primitive": "cylinder",
        # 6 mm tall, so it reaches 3 mm either side of the neck and stops short of the five
        # loose rings, whose tops are at z = 27 (a taller anchor touches them and joins
        # them to the carrot: 6 shells become 4).
        "size": [2 * neck.room + 3, 2 * neck.room + 3, 6],
        "at": [
            float(neck.centroid[0]) + value(m.translate[0]),
            float(neck.centroid[1]) + value(m.translate[1]),
            z + value(m.translate[2]),
        ],
        "colour": GREEN,
    }


@pytest.mark.requires_openscad
def test_the_carrot_neck_anchored_in_green_prints_as_one_piece(tmp_path: Path) -> None:
    _, scene = opened(CARROT, tmp_path)
    small, mesh, z, neck = carrot_neck(tmp_path, scene)
    shells_before = len(mesh.split(only_watertight=False))
    anchored = apply_edits(
        scene,
        EDITS.validate_python([neck_anchor(small, z, neck)]),
        mesh_of(tmp_path),
    )
    only = [o for o in anchored.objects if o.id == small.id]
    parts = render(tmp_path, anchored.model_copy(update={"objects": only}))
    by_colour = {p.colour: p.mesh for p in parts}
    assert set(by_colour) == {small.parts[0].colour, GREEN}
    # The same scene in one colour: the render's union, where touching solids are one shell
    # (two colours' meshes never share a vertex, so they cannot be counted joined).
    one = only[0].model_copy(
        update={"parts": [p.model_copy(update={"colour": GREEN}) for p in only[0].parts]}
    )
    (single,) = render(tmp_path, anchored.model_copy(update={"objects": [one]}))
    # One piece: the anchor joins what was there and adds no shell of its own.
    assert len(single.mesh.split(only_watertight=False)) == shells_before
    # No volume claimed by both colours.
    assert sum(p.mesh.volume for p in parts) == pytest.approx(single.mesh.volume, rel=0.005)


def placed_parts(scene: Scene, directory: Path) -> list[ColourPart]:
    """What the op-free render's solids are, without openscad: each colour's parts at
    their objects' places (measured identical to the render, meshmodel/paint.py)."""
    load = mesh_loader(directory)
    by_colour: dict[str, list[trimesh.Trimesh]] = {}
    for obj in scene.objects:
        m, t = placement(obj)
        for part in obj.parts:
            if part.negative:
                continue
            mesh = load(part.mesh)
            world = np.asarray(mesh.vertices) @ m.T + t
            by_colour.setdefault(part.colour, []).append(
                trimesh.Trimesh(vertices=world, faces=mesh.faces, process=False)
            )
    parts = []
    for extruder, (colour, meshes) in enumerate(by_colour.items(), start=1):
        joined = trimesh.util.concatenate(meshes)
        assert isinstance(joined, trimesh.Trimesh)
        parts.append(ColourPart(extruder, colour, colour, joined))
    return parts


def test_the_carrot_gardens_weak_joints_are_its_two_necks(tmp_path: Path) -> None:
    """Measured 2026-10-13: the necks at z 30.9 (small carrot) and 33.5 (big); the
    planter's walls and the big carrot's hollow body pass the 25 % rule but their
    sections are rings, so they are not reported."""
    _, scene = opened(CARROT, tmp_path)
    joints = analyze_geometry(placed_parts(scene, tmp_path)).weak_joints
    assert len(joints) == 2
    small, big = sorted(joints, key=lambda j: j.z)
    assert 30.0 < small.z < 32.0
    assert 32.5 < big.z < 35.0
    assert all(j.suggestion.joint is not None for j in joints)


def test_the_carrot_gardens_suggested_splits_name_their_carrots_and_fit(tmp_path: Path) -> None:
    """The carrot loop (spec §9): each weak joint's suggestion, sent to edit_scene with
    the object `object_at` names, splits that carrot with a joint that fits."""
    _, scene = opened(CARROT, tmp_path)
    load = mesh_loader(tmp_path)
    joints = analyze_geometry(placed_parts(scene, tmp_path)).weak_joints
    named = {object_at(scene, load, joint.centroid): joint for joint in joints}
    painted = {o.id for o in scene.objects if any(p.painted for p in o.parts)}
    assert set(named) == painted
    for oid, joint in named.items():
        edit = {**joint.suggestion.model_dump(), "object": oid}
        split = apply_edits(scene, EDITS.validate_python([edit]), load)
        assert len(split.objects) == len(scene.objects) + 1
