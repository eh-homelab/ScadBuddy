"""One pipeline output's blob (spec 2026-09-27 §5.2 `output`): the multi-plate 3MF
from the pieces' own solids, placed as the layout says, plus the extra files."""

from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
from collections import Counter
from dataclasses import replace
from pathlib import Path

import numpy as np
import trimesh
from temporalio.exceptions import ApplicationError

from scadbuddy.core.paths import BUILTIN_PREFIX
from scadbuddy.render.bambu3mf import PlateParts, write_plates_3mf
from scadbuddy.render.glb import BoundingBox, bounding_box, write_glb
from scadbuddy.render.job_models import FILE_NAME_PATTERN, JobResult, ManifestObject, PipelineOutput
from scadbuddy.render.jobs import (
    LAYOUT_NAME,
    MODEL_NAME,
    PREVIEW_NAME,
    PlateLayout,
    plates_thumbnails,
    result_parts,
    result_plates,
)
from scadbuddy.render.plate import plate_for
from scadbuddy.render.split import ColourPart, Paint
from scadbuddy.store.content import BlobScope, template_title
from scadbuddy.template import Blob, Part
from scadbuddy.workflows.activities import PIECE_NAME, WorkerDeps, _heartbeating, _read_piece
from scadbuddy.workflows.models import OutputRequest

_RESERVED = {MODEL_NAME, PREVIEW_NAME, LAYOUT_NAME, PIECE_NAME}


def output_key(job_id: str, index: int) -> str:
    return f"output-{job_id}-{index}"


def _refuse(message: str) -> ApplicationError:
    return ApplicationError(message, type="OutputError", non_retryable=True)


def _unknown_piece(key: str) -> ApplicationError:
    """A layout that names a piece the output was not given: the pipeline's packing is
    wrong, whichever piece it is."""
    return ApplicationError(
        f"the layout places piece {key}, which is not one of the output's parts",
        type="PackError",
        non_retryable=True,
    )


async def _fetch(deps: WorkerDeps, key: str) -> bool:
    return await _heartbeating(asyncio.create_task(deps.blobs.fetch(key)))


async def build_output(req: OutputRequest, deps: WorkerDeps, *, model_dir: Path) -> PipelineOutput:
    """``model_dir`` is the template's directory at the job's revision: it names the
    store folder, as `_scope` does for a piece.

    The output's own blob is built as `render_main` builds a piece: checked out fresh,
    written, then published against the baseline the checkout read, so a zombie attempt
    cannot overwrite a newer one. Every blob the output reads is then held for the job
    at once (`refs`), because the pipeline may run on for longer than the sweep's grace."""
    blobs = deps.blobs
    record = req.record.model_copy(
        update={"image_revision": deps.revision, "openscad_version": deps.openscad_version}
    )
    for name in req.files:
        if not re.fullmatch(FILE_NAME_PATTERN, name):
            raise _refuse(f"output file name {name!r}: use letters, digits, '.', '_' or '-'")
        if name in _RESERVED:
            raise _refuse(f"output file name {name!r} is reserved for the output's own files")
    known = {p.piece_key for p in req.parts}
    named = (
        [req.layout.own]
        if req.layout.own is not None
        else [item.piece_key for plate in req.layout.plates for item in plate.items]
    )
    for name in named:
        if name not in known:
            raise _unknown_piece(name)
    key = output_key(req.job_id, req.index)
    writes = req.layout.own is None or bool(req.files)
    baseline = (
        await _heartbeating(asyncio.create_task(blobs.checkout_fresh(key))) if writes else None
    )
    keys: list[str] = []
    if req.layout.own is not None:
        own = req.layout.own
        found = await _fetch(deps, own)
        piece = (
            await asyncio.to_thread(_read_piece, blobs.dir_for(own) / PIECE_NAME) if found else None
        )
        if piece is None:
            raise _refuse(f"piece {own} has no finished render")
        result = piece.result
        keys.append(own)
    else:
        result = await _write_plates(req, deps, key)
    if req.files:
        files_dir = blobs.dir_for(key) / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        for name, value in req.files.items():
            if isinstance(value, Blob):
                if not await _fetch(deps, value.key):
                    raise _refuse(f"{name}: blob {value.key} is not in the store")
                root = blobs.dir_for(value.key).resolve()
                source = (root / value.path).resolve()
                if not source.is_relative_to(root) or not source.is_file():
                    raise _refuse(f"{name}: {value.path} is not a file of blob {value.key}")
                await asyncio.to_thread(shutil.copyfile, source, files_dir / name)
            else:
                await asyncio.to_thread((files_dir / name).write_text, value, "utf-8")
    if writes:
        # Internal, like a piece: the default `folder="work"` (phase 3 sweeps it). The
        # file a person sees is Generate's save (Task 7), not this blob.
        title = await asyncio.to_thread(template_title, model_dir, req.slug)
        scope = BlobScope(slug=req.slug, title=title)
        await _heartbeating(
            asyncio.create_task(blobs.publish_fresh(key, scope=scope, expected=baseline))
        )
        keys.append(key)
    for held in keys:
        await asyncio.to_thread(deps.refs.add, held, "job", req.job_id)
    return PipelineOutput(
        name=req.name,
        result=result,
        bom=req.bom,
        files=sorted(req.files),
        files_key=key if req.files else None,
        blob_keys=keys,
        record=record,
        manifest=manifest_of(req),
    )


def manifest_of(req: OutputRequest) -> list[ManifestObject]:
    """The output's objects, from the layout it is written from (spec §7)."""
    # One entry per piece and its colours (#3637): a copy recoloured is arranged again
    # in its own colours, so it is an entry of its own.
    counts: Counter[tuple[str, str]] = Counter()
    swaps: dict[tuple[str, str], dict[str, str]] = {}
    if req.layout.own is not None:
        counts[(req.layout.own, "{}")] += 1
        swaps[(req.layout.own, "{}")] = {}
    for plate in req.layout.plates:
        for p in plate.items:
            swap = {k.upper(): v.upper() for k, v in p.recolour.items() if k.upper() != v.upper()}
            key = (p.piece_key, json.dumps(swap, sort_keys=True))
            counts[key] += 1
            swaps.setdefault(key, swap)
    named = {entry.part: entry.piece for entry in req.bom if entry.part}
    by_piece: dict[str, list[tuple[str, str]]] = {}
    for key in counts:
        by_piece.setdefault(key[0], []).append(key)
    objects: list[ManifestObject] = []
    for part in req.parts:
        for key in by_piece.get(part.piece_key, []):
            objects.append(_manifest_entry(req, part, counts[key], swaps[key], named))
    return objects


def _manifest_entry(
    req: OutputRequest, part: Part, count: int, recolour: dict[str, str], named: dict[str, str]
) -> ManifestObject:
    earlier = req.provenance.get(part.piece_key)
    return ManifestObject(
        part=part.piece_key,
        file=part.file,
        slug=earlier.slug if earlier else req.slug,
        revision=earlier.revision if earlier else req.record.revision,
        bbox=part.bbox,
        footprint=(part.bbox.size[0], part.bbox.size[1]),
        colours=list(part.colours),
        # Kept, so arranging this output again still states them.
        materials=dict(earlier.materials) if earlier else {},
        count=count,
        plates=part.plates,
        bom_piece=named.get(part.piece_key) or (earlier.bom_piece if earlier else None),
        source_output=earlier.source_output if earlier else None,
        library_file_id=earlier.library_file_id if earlier else None,
        notes=list(part.notes),
        recolour=recolour,
    )


def _joined(meshes: list[trimesh.Trimesh]) -> trimesh.Trimesh:
    """One colour's copies on a plate, as the one mesh the writer takes per colour."""
    joined = trimesh.util.concatenate(meshes)
    if not isinstance(joined, trimesh.Trimesh):
        raise _refuse(f"could not join {len(meshes)} meshes into one ({type(joined).__name__})")
    return joined


def placement_matrix(box: BoundingBox, x: float, y: float, rot: float) -> np.ndarray:
    """Turn a part ``rot`` degrees about Z, then move it so its turned box's low corner
    is at ``(x, y)`` and it sits on the plate (z = 0)."""
    turn = trimesh.transformations.rotation_matrix(math.radians(rot), (0, 0, 1))
    corners = np.array(
        [[bx, by, 0.0, 1.0] for bx in (box.min[0], box.max[0]) for by in (box.min[1], box.max[1])]
    ).T
    turned = turn @ corners
    move = trimesh.transformations.translation_matrix(
        (x - float(turned[0].min()), y - float(turned[1].min()), -float(box.min[2]))
    )
    matrix: np.ndarray = move @ turn
    return matrix


def _materials(req: OutputRequest) -> dict[str, str]:
    """Each colour's material as the arranged objects' sources state it. Only a library
    file states any. A render's colour has a material only when a library object
    shares its slot, since one slot is one filament. A colour two sources
    give different materials is unknown too: one slot prints both parts, and picking
    either would slice the other in the wrong material. The writer then states none
    (`project_settings`)."""
    materials: dict[str, str] = {}
    conflicting: set[str] = set()

    def state(colour: str, material: str) -> None:
        if materials.setdefault(colour.upper(), material) != material:
            conflicting.add(colour.upper())

    for obj in req.provenance.values():
        for colour, material in obj.materials.items():
            state(colour, material)
    # A recoloured copy's new colour is the same material as the one it replaces (#3637).
    for plate in req.layout.plates:
        for placed in plate.items:
            source = req.provenance.get(placed.piece_key)
            stated = {c.upper(): m for c, m in source.materials.items()} if source else {}
            for old, new in placed.recolour.items():
                if old.upper() in stated:
                    state(new, stated[old.upper()])
    return {c: m for c, m in materials.items() if c not in conflicting}


async def _write_plates(req: OutputRequest, deps: WorkerDeps, key: str) -> JobResult:
    blobs = deps.blobs
    parts = {p.piece_key: p for p in req.parts}
    layouts: dict[str, PlateLayout] = {}
    # One slot per colour: a plan that spells it twice (in two cases) still gets one.
    colours: list[str] = list(dict.fromkeys(c.upper() for c in req.colours))
    plates: list[PlateParts] = []
    # The plate each piece is last placed on: its meshes are let go after it (#2573),
    # so the pieces loaded at once are those still to be placed, not every one read.
    last = {
        item.piece_key: index
        for index, plate in enumerate(req.layout.plates)
        for item in plate.items
    }
    for index, plate in enumerate(req.layout.plates):
        # #956: each placed copy is an object of its own (and each object a copy's
        # template declared), so a by-object print can finish one before the next. Within
        # an object, a colour's meshes are still joined into one part.
        plate_parts: list[ColourPart] = []
        plate_extruders: list[int] = []
        plate_objects: list[int] = []
        number = 0
        for placed in plate.items:
            if placed.piece_key not in parts:
                raise _unknown_piece(placed.piece_key)
            if placed.piece_key not in layouts:
                if not await _fetch(deps, placed.piece_key):
                    raise _refuse(f"piece {placed.piece_key} is not in the store")
                layouts[placed.piece_key] = await asyncio.to_thread(
                    PlateLayout.load, blobs.dir_for(placed.piece_key) / LAYOUT_NAME
                )
            matrix = placement_matrix(parts[placed.piece_key].bbox, placed.x, placed.y, placed.rot)
            source = layouts[placed.piece_key].plates[0]
            # This copy's colours swapped (#3637): its painting's too.
            swap = {k.upper(): v.upper() for k, v in placed.recolour.items()}
            for owner in dict.fromkeys(source.objects):
                by_colour: dict[str, list[trimesh.Trimesh]] = {}
                names: dict[str, str] = {}
                # Each painted part is a part of its own: its faces are its painting's (#1965).
                painted: list[ColourPart] = []
                # Its negative parts, cut out of this object by the slicer.
                negatives: list[ColourPart] = []
                for part, object_number in zip(source.parts, source.objects, strict=True):
                    if object_number != owner:
                        continue
                    mesh = part.mesh.copy()
                    mesh.apply_transform(matrix)
                    if part.negative:
                        # Named no filament: it prints nothing.
                        negatives.append(replace(part, mesh=mesh))
                        continue
                    colour = swap.get(part.colour.upper(), part.colour.upper())
                    paint = part.paint
                    if paint is not None and swap:
                        paint = Paint(
                            paint.codes,
                            tuple(swap.get(c.upper(), c.upper()) for c in paint.colours),
                        )
                    for used in [colour, *(paint.used() if paint is not None else [])]:
                        if used.upper() not in colours:
                            colours.append(used.upper())
                    if paint is not None:
                        painted.append(
                            ColourPart(colours.index(colour), part.name, colour, mesh, paint)
                        )
                        continue
                    by_colour.setdefault(colour, []).append(mesh)
                    names.setdefault(colour, part.name)
                ordered = sorted(by_colour, key=colours.index)
                joined = [
                    ColourPart(colours.index(c), names[c], c, _joined(by_colour[c]))
                    for c in ordered
                ]
                printed = (*joined, *painted)
                if not printed:
                    continue
                extruders = tuple(colours.index(part.colour) + 1 for part in printed)
                # A negative part takes its object's first extruder: the slicer needs one,
                # and it prints nothing with it.
                cut = tuple(
                    replace(part, material_index=extruders[0] - 1, colour=colours[extruders[0] - 1])
                    for part in negatives
                )
                number += 1
                plate_parts += [*printed, *cut]
                plate_extruders += [*extruders, *(extruders[0] for _ in cut)]
                plate_objects += [number] * (len(printed) + len(cut))
        if not plate_parts:
            # Skipped, it would renumber every later plate against the layout.
            raise _refuse(f"plate {index + 1} of the layout has nothing to print")
        plates.append(PlateParts(tuple(plate_parts), tuple(plate_extruders), tuple(plate_objects)))
        for done in [piece for piece in layouts if last[piece] == index]:
            del layouts[done]
    if not plates:
        raise _refuse("the layout has no plates")
    everything = [part for plate in plates for part in plate.parts]
    # What the result reports (its parts, colours, plates) is what prints.
    shown = [
        PlateParts(
            tuple(p for p in plate.parts if not p.negative),
            tuple(e for p, e in zip(plate.parts, plate.extruders, strict=True) if not p.negative),
            tuple(o for p, o in zip(plate.parts, plate.objects, strict=True) if not p.negative),
        )
        for plate in plates
    ]
    layout = PlateLayout(shown, colours, [], bounding_box(everything), [])
    thumbnails, warnings = await plates_thumbnails(
        [plate.parts for plate in plates], config=deps.config, executor=deps.thumbnail_executor
    )
    work = blobs.dir_for(key)
    await asyncio.to_thread(
        write_plates_3mf,
        plates,
        colours,
        work / MODEL_NAME,
        thumbnails=thumbnails,
        model_name=req.slug.removeprefix(BUILTIN_PREFIX),
        plate=plate_for(req.plate_model),
        materials=_materials(req),
    )
    await asyncio.to_thread(write_glb, list(plates[0].parts), work / PREVIEW_NAME)
    root = deps.paths.root
    return JobResult(
        model_3mf=str((work / MODEL_NAME).relative_to(root)),
        preview_glb=str((work / PREVIEW_NAME).relative_to(root)),
        source_version=req.record.revision or "",
        parts=result_parts(layout),
        bbox_mm=layout.bbox,
        colors=colours,
        warnings=warnings,
        plates=result_plates(layout),
        notes=[note for p in req.parts for note in p.notes],
    )
