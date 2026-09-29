"""One pipeline output's blob (spec 2026-09-27 §5.2 `output`): the multi-plate 3MF
from the pieces' own solids, placed as the layout says, plus the extra files."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

import trimesh
from temporalio.exceptions import ApplicationError

from scadbuddy.core.paths import BUILTIN_PREFIX
from scadbuddy.render.bambu3mf import PlateParts, write_plates_3mf
from scadbuddy.render.glb import bounding_box, write_glb
from scadbuddy.render.job_models import FILE_NAME_PATTERN, JobResult, PipelineOutput
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
from scadbuddy.render.split import ColourPart
from scadbuddy.store.content import BlobScope, template_title
from scadbuddy.template import Blob
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
        if not re.match(FILE_NAME_PATTERN, name):
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
    )


def _joined(meshes: list[trimesh.Trimesh]) -> trimesh.Trimesh:
    """One colour's copies on a plate, as the one mesh the writer takes per colour."""
    joined = trimesh.util.concatenate(meshes)
    if not isinstance(joined, trimesh.Trimesh):
        raise _refuse(f"could not join {len(meshes)} meshes into one ({type(joined).__name__})")
    return joined


async def _write_plates(req: OutputRequest, deps: WorkerDeps, key: str) -> JobResult:
    blobs = deps.blobs
    parts = {p.piece_key: p for p in req.parts}
    layouts: dict[str, PlateLayout] = {}
    colours: list[str] = []
    plates: list[PlateParts] = []
    for plate in req.layout.plates:
        by_colour: dict[str, list[trimesh.Trimesh]] = {}
        names: dict[str, str] = {}
        for placed in plate.items:
            if placed.piece_key not in parts:
                raise _unknown_piece(placed.piece_key)
            if placed.piece_key not in layouts:
                if not await _fetch(deps, placed.piece_key):
                    raise _refuse(f"piece {placed.piece_key} is not in the store")
                layouts[placed.piece_key] = await asyncio.to_thread(
                    PlateLayout.load, blobs.dir_for(placed.piece_key) / LAYOUT_NAME
                )
            box = parts[placed.piece_key].bbox
            offset = (placed.x - box.min[0], placed.y - box.min[1], -box.min[2])
            for part in layouts[placed.piece_key].plates[0].parts:
                mesh = part.mesh.copy()
                mesh.apply_translation(offset)
                by_colour.setdefault(part.colour, []).append(mesh)
                names.setdefault(part.colour, part.name)
                if part.colour not in colours:
                    colours.append(part.colour)
        ordered = sorted(by_colour, key=colours.index)
        plates.append(
            PlateParts(
                tuple(
                    ColourPart(colours.index(c), names[c], c, _joined(by_colour[c]))
                    for c in ordered
                ),
                tuple(colours.index(c) + 1 for c in ordered),
            )
        )
    if not plates:
        raise _refuse("the layout has no plates")
    everything = [part for plate in plates for part in plate.parts]
    layout = PlateLayout(plates, colours, [], bounding_box(everything), [])
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
