from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from concurrent.futures import Executor
from contextlib import (
    AbstractContextManager,
    AsyncExitStack,
    asynccontextmanager,
    contextmanager,
    nullcontext,
    suppress,
)
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any

from scadbuddy.core.config import Config
from scadbuddy.core.metrics import Metrics, RenderStage
from scadbuddy.core.paths import (
    BUILTIN_PREFIX,
    SCHEMA_CACHE_NAME,
    SOURCE_NAME,
    DataPaths,
    model_path,
)
from scadbuddy.library.assets import AssetStore, file_assets
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import (
    CheckoutFetcher,
    CheckoutGate,
    model_search_path,
    require_checkouts,
    resolve_search_path,
    revision_search_path,
)
from scadbuddy.render.bambu3mf import PlateParts, single_plate, write_plates_3mf
from scadbuddy.render.colours import colour_hex
from scadbuddy.render.glb import BoundingBox, bounding_box, write_glb
from scadbuddy.render.job_models import Job as Job
from scadbuddy.render.job_models import JobResult as JobResult
from scadbuddy.render.job_models import JobState as JobState
from scadbuddy.render.job_models import PartInfo as PartInfo
from scadbuddy.render.job_models import PlateInfo as PlateInfo
from scadbuddy.render.provenance import source_version
from scadbuddy.render.runner import OpenSCADError, ProcessOutput, cached_schema, render_3mf
from scadbuddy.render.schema import CustomizerSchema, ParamValue
from scadbuddy.render.solids import STAGED_ASSET_PREFIX, render_solids, solid_file, solid_mesh
from scadbuddy.render.split import ColourPart, split_by_material
from scadbuddy.render.thumbnail import PlateThumbnails, render_plate_thumbnails

# The `X as X` imports above are re-exports: the job models lived here before they
# were split out, and routes and tests import them here.

logger = logging.getLogger(__name__)

RAW_RENDER_NAME = "render.3mf"
MODEL_NAME = "model.3mf"
PREVIEW_NAME = "preview.glb"
LAYOUT_NAME = "layout.json"

# Material 0 is OpenSCAD's "Default": geometry no color() call reached.
UNCOLOURED_MATERIAL_INDEX = 0
UNCOLOURED_WARNING = "uncoloured geometry present; parts are not closed"
THUMBNAIL_TIMEOUT_WARNING = "plate thumbnail timed out; the 3MF carries no cover image"
THUMBNAIL_FAILED_WARNING = "plate thumbnail failed; the 3MF carries no cover image"
MISSING_FILE_WARNING = "OpenSCAD could not open {name}; the model rendered without it"
#: The same fact on a render that failed, where "rendered without it" is untrue.
MISSING_FILE_FAILED_WARNING = "OpenSCAD could not open {name}"
#: The most plates a template may ask for with `echo(plates = N)`. Each plate is a
#: render plus a solid render per colour of its own, so this bounds a job's cost.
MAX_PLATES = 16


def unreadable_colour_warnings(
    schema: CustomizerSchema, params: Mapping[str, ParamValue]
) -> list[str]:
    """A warning for each colour parameter whose value is not a colour OpenSCAD reads
    (a malformed hex, an unknown name): no part can match it, so it gets no extruder."""
    warnings: list[str] = []
    for parameter in schema.parameters:
        if parameter.type != "color":
            continue
        value = params.get(parameter.name, parameter.initial)
        if colour_hex(value) is None:
            warnings.append(
                f"colour parameter {parameter.name!r} is {value!r}, not a colour; "
                "it gets no extruder"
            )
    return warnings


def failed_render_warnings(
    missing_files: Sequence[str], schema: CustomizerSchema, params: Mapping[str, ParamValue]
) -> list[str]:
    """The warnings a failed render can still give (#408): a file it could not open
    is often why it failed -- a template that draws only the picture then renders
    nothing -- and an unreadable colour is a fact about the parameters either way."""
    return [
        *(MISSING_FILE_FAILED_WARNING.format(name=name) for name in missing_files),
        *unreadable_colour_warnings(schema, params),
    ]


def extruder_order(
    parts: Sequence[ColourPart], schema: CustomizerSchema, params: Mapping[str, ParamValue]
) -> list[ColourPart]:
    """The split parts in extruder order: spec §7's "extruder 1 = first colour
    parameter".

    OpenSCAD numbers its materials in the order the geometry first uses each colour,
    not in parameter order, so a model that draws its second colour first would
    otherwise swap its extruders. Each part is matched to the first colour parameter,
    in declaration order, whose rendered value is that part's colour; parameters that
    share a value therefore share one extruder, and one no geometry uses gets none.
    Every part no parameter names -- a hard-coded colour, one computed from a
    parameter, the uncoloured Default -- follows, in OpenSCAD's material order."""
    ranks: dict[str, int] = {}
    for parameter in schema.parameters:
        if parameter.type != "color":
            continue
        colour = colour_hex(params.get(parameter.name, parameter.initial))
        if colour is not None:
            ranks.setdefault(colour, len(ranks))

    def rank(part: ColourPart) -> int:
        if part.material_index == UNCOLOURED_MATERIAL_INDEX:
            return len(ranks)
        return ranks.get(part.colour, len(ranks))

    return sorted(parts, key=rank)


async def solid_parts(
    scad_path: Path,
    schema: CustomizerSchema,
    params: Mapping[str, ParamValue],
    preview_parts: Sequence[ColourPart],
    work_dir: Path,
    *,
    config: Config,
    extra_defines: Sequence[str] = (),
) -> tuple[list[ColourPart], list[str]]:
    """The parts the 3MF is written from: one closed solid per colour where OpenSCAD can
    give us one, the open split mesh where it cannot."""
    if any(part.material_index == UNCOLOURED_MATERIAL_INDEX for part in preview_parts):
        return list(preview_parts), [UNCOLOURED_WARNING]
    solids = await render_solids(
        scad_path,
        schema,
        params,
        [part.colour for part in preview_parts],
        work_dir,
        config=config,
        extra_defines=extra_defines,
    )
    parts = [
        part if part.colour not in solids.meshes else replace(part, mesh=solids.meshes[part.colour])
        for part in preview_parts
    ]
    return parts, solids.warnings


def plate_defines(index: int) -> list[str]:
    """The `-D` that makes a multi-plate template draw only plate ``index`` (spec §6.4)."""
    return ["-D", f"$plate={index}"]


@dataclass(frozen=True)
class PartSource:
    """Where a layout part's mesh is on disk, relative to the render's directory:
    a closed solid's own file, or the material of a split render."""

    file: str
    solid: bool = False


@dataclass(frozen=True)
class PlateLayout:
    """What the 3MF of a render is written from: its plates, and the one filament
    list their extruder numbers index into."""

    plates: list[PlateParts]
    colours: list[str]
    warnings: list[str]
    #: The everything-at-once render's box: what the preview shows and the result reports.
    bbox: BoundingBox
    #: Each plate's `PartSource`s, in the order of its parts.
    sources: list[tuple[PartSource, ...]]

    def save(self, path: Path) -> None:
        """The layout as JSON beside the files its parts are read from; no meshes."""
        plates = [
            {
                "extruders": list(plate.extruders),
                "parts": [
                    {
                        "material_index": part.material_index,
                        "name": part.name,
                        "colour": part.colour,
                        "file": source.file,
                        "solid": source.solid,
                    }
                    for part, source in zip(plate.parts, sources, strict=True)
                ],
            }
            for plate, sources in zip(self.plates, self.sources, strict=True)
        ]
        document = {
            "plates": plates,
            "colours": self.colours,
            "warnings": self.warnings,
            "bbox": self.bbox.model_dump(mode="json"),
        }
        path.write_text(json.dumps(document), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> PlateLayout:
        """What :meth:`save` wrote, the meshes re-read from the files it names."""
        work = path.parent
        document = json.loads(path.read_text(encoding="utf-8"))
        splits: dict[str, list[ColourPart]] = {}

        def part(entry: dict[str, Any]) -> tuple[ColourPart, PartSource]:
            source = PartSource(entry["file"], entry["solid"])
            if source.solid:
                mesh = solid_mesh(work / source.file)
                if mesh is None:
                    raise ValueError(f"{source.file} holds no solid")
            else:
                if source.file not in splits:
                    splits[source.file] = split_by_material(work / source.file)
                mesh = next(
                    split.mesh
                    for split in splits[source.file]
                    if split.material_index == entry["material_index"]
                )
            return ColourPart(entry["material_index"], entry["name"], entry["colour"], mesh), source

        plates: list[PlateParts] = []
        sources: list[tuple[PartSource, ...]] = []
        for plate in document["plates"]:
            parts = [part(entry) for entry in plate["parts"]]
            plates.append(PlateParts(tuple(p for p, _ in parts), tuple(plate["extruders"])))
            sources.append(tuple(s for _, s in parts))
        return cls(
            plates,
            document["colours"],
            document["warnings"],
            BoundingBox.model_validate(document["bbox"]),
            sources,
        )


def _part_sources(
    work_dir: Path, raw: Path, split: Sequence[ColourPart], parts: Sequence[ColourPart]
) -> tuple[PartSource, ...]:
    """Where each of ``parts`` is on disk. `solid_parts` returns the very part it was
    given where it has no closed solid for that colour, so a part that is not its
    split is the wrapper render of its position (`solid_file`)."""
    return tuple(
        PartSource(raw.relative_to(work_dir).as_posix())
        if part is given
        else PartSource(solid_file(raw.parent, index).relative_to(work_dir).as_posix(), solid=True)
        for index, (part, given) in enumerate(zip(parts, split, strict=True), start=1)
    )


async def plate_layout(
    scad_path: Path,
    schema: CustomizerSchema,
    params: Mapping[str, ParamValue],
    preview_parts: Sequence[ColourPart],
    count: int,
    work_dir: Path,
    *,
    config: Config,
) -> PlateLayout:
    """The plates of a render: one for an ordinary template, ``count`` for one that
    asked for more with `echo(plates = N)` (spec §6.4).

    Plate *k* is its own render with ``$plate = k``, split, then the per-colour solids
    of §6.3 with the same ``$plate``. Its parts are numbered against the extruder
    order of the everything-at-once render (``preview_parts``), so a colour is the
    same extruder on every plate. A colour only the everything render draws gets no
    extruder, and one only a plate draws is appended; both are warnings, because
    either means the template's plates and its preview disagree.
    """
    box = bounding_box(preview_parts)
    if count <= 1:
        parts, solid_warnings = await solid_parts(
            scad_path, schema, params, preview_parts, work_dir, config=config
        )
        single = _part_sources(work_dir, work_dir / RAW_RENDER_NAME, preview_parts, parts)
        return PlateLayout(
            [single_plate(parts)], [part.colour for part in parts], solid_warnings, box, [single]
        )
    if count > MAX_PLATES:
        raise OpenSCADError(
            f"the template asks for {count} plates; ScadBuddy renders at most {MAX_PLATES}", []
        )

    colours = [part.colour for part in preview_parts]
    warnings: list[str] = []
    drawn: list[tuple[list[ColourPart], list[int]]] = []
    sources: list[tuple[PartSource, ...]] = []
    for index in range(1, count + 1):
        plate_dir = work_dir / f"plate-{index}"
        plate_dir.mkdir(parents=True, exist_ok=True)
        defines = plate_defines(index)
        raw = plate_dir / RAW_RENDER_NAME
        try:
            await render_3mf(scad_path, schema, params, raw, config=config, extra_defines=defines)
        except OpenSCADError as error:
            raise OpenSCADError(
                f"plate {index} of {count}: {error}",
                error.log_tail,
                error.returncode,
                diagnostics=error.diagnostics,
                diagnostics_dropped=error.diagnostics_dropped,
            ) from error
        split = split_by_material(raw)
        if not split:
            raise OpenSCADError(f"plate {index} of {count} rendered no geometry", [])
        for part in split:
            if part.colour not in colours:
                colours.append(part.colour)
                warnings.append(
                    f"plate {index}: {part.colour} is not in the all-plates render; "
                    f"it gets extruder {len(colours)}"
                )
        split.sort(key=lambda part: colours.index(part.colour))
        parts, solid_warnings = await solid_parts(
            scad_path, schema, params, split, plate_dir, config=config, extra_defines=defines
        )
        # Unprefixed and once each: `geometry.split_colours` reads these back by colour.
        warnings += [warning for warning in solid_warnings if warning not in warnings]
        drawn.append((parts, [colours.index(part.colour) + 1 for part in parts]))
        sources.append(_part_sources(work_dir, raw, split, parts))

    # Dense extruders, as §7 promises: a colour no plate draws gives up its slot.
    used = sorted({extruder for _, extruders in drawn for extruder in extruders})
    renumber = {old: new for new, old in enumerate(used, start=1)}
    for old, colour in enumerate(colours, start=1):
        if old not in renumber:
            warnings.append(f"{colour} is drawn only with every plate at once; it is on no plate")
    plates = [
        PlateParts(tuple(parts), tuple(renumber[extruder] for extruder in extruders))
        for parts, extruders in drawn
    ]
    return PlateLayout(plates, [colours[old - 1] for old in used], warnings, box, sources)


def result_parts(layout: PlateLayout) -> list[PartInfo]:
    """One entry per extruder: the name the first plate to use it gives it, and whether
    every plate's part of that colour is a closed solid."""
    infos: list[PartInfo] = []
    for extruder, colour in enumerate(layout.colours, start=1):
        parts = [
            part
            for plate in layout.plates
            for part, number in zip(plate.parts, plate.extruders, strict=True)
            if number == extruder
        ]
        infos.append(
            PartInfo(
                name=parts[0].name,
                colour=colour,
                extruder=extruder,
                watertight=all(part.watertight for part in parts),
            )
        )
    return infos


def result_plates(layout: PlateLayout) -> list[PlateInfo]:
    """The job result's per-plate summary: empty for a one-plate render."""
    if len(layout.plates) == 1:
        return []
    return [
        PlateInfo(
            index=index,
            bbox_mm=bounding_box(plate.parts),
            colors=[layout.colours[extruder - 1] for extruder in plate.extruders],
        )
        for index, plate in enumerate(layout.plates, start=1)
    ]


async def plate_thumbnails(
    parts: Sequence[ColourPart], *, config: Config, executor: Executor | None = None
) -> tuple[PlateThumbnails | None, list[str]]:
    """The 3MF's cover images, under the same wall-clock budget as a render.

    Two separate reasons, and neither is the other's.

    OFF THE EVENT LOOP, because rasterising four images is seconds of numpy on a
    large mesh where writing the rest of the 3MF is milliseconds of XML. One loop
    serves the whole process, so a synchronous call would stall every other job's
    poll, `/healthz` and the second render worker — and §5.3's debounced preview
    submits these back to back on a slider drag. Everything else in this pipeline
    already yields: `render_3mf` and `render_solids` await a subprocess.

    BOUNDED, because §6.1's guarantee is that a job is time-bounded, and until
    now `SCADBUDDY_RENDER_TIMEOUT` delivered it by killing an `openscad` child.
    This step has no child to kill, and its cost rises with face count, so a mesh
    each OpenSCAD pass produced well inside its own budget can still rasterise
    for far longer than the whole job is supposed to take — with
    `render_concurrency` 2, two of those starve the queue. The budget is
    `render_timeout` rather than a new knob: this is the same job's time.

    The degradation is a 3MF with no cover images, NOT a failed job — the model
    is what the user asked for and the cover is a nicety. `write_plates_3mf` then
    omits the png content type, the cover relationships and the plate's
    `thumbnail_file`/`top_file`/`pick_file` along with the images, so the package
    stays self-consistent rather than carrying dangling references.

    `wait_for` cannot cancel the thread it abandons, so the orphan keeps its core
    until it finishes. That is acceptable here and would not be for `openscad`:
    this work is O(faces) plus O(covered pixels) with no loop that can fail to
    terminate, whereas a `.scad` can legitimately spin forever. It is also why the
    worker passes its own `executor` (#116): on the loop's default one an orphan
    holds a slot `write_plates_3mf` needs, so a backlog of slow covers could stall
    jobs whose own render finished in budget. On a dedicated pool a backlog only
    queues the next cover, which then times out like any other.
    """
    covers, warnings = await plates_thumbnails([parts], config=config, executor=executor)
    return (covers[0] if covers is not None else None), warnings


async def plates_thumbnails(
    plates: Sequence[Sequence[ColourPart]],
    *,
    config: Config,
    executor: Executor | None = None,
) -> tuple[list[PlateThumbnails] | None, list[str]]:
    """Every plate's cover images, under the ONE budget :func:`plate_thumbnails`
    documents: a multi-plate render (spec §6.4) is still one job to bound. All or
    none, as the 3MF writer takes them."""
    loop = asyncio.get_running_loop()
    faces = sum(len(part.mesh.faces) for parts in plates for part in parts)

    def render_all() -> list[PlateThumbnails]:
        return [render_plate_thumbnails(parts) for parts in plates]

    try:
        rendered = await asyncio.wait_for(
            loop.run_in_executor(executor, render_all),
            timeout=config.render_timeout,
        )
    except TimeoutError:
        logger.warning(
            "plate thumbnail render exceeded the budget; writing the 3MF without cover images",
            extra={"faces": faces},
        )
        return None, [THUMBNAIL_TIMEOUT_WARNING]
    except Exception:
        # Same degradation for a rasteriser bug (a degenerate face, an allocation
        # failure) as for a slow one: it may cost the cover, never the model (#116).
        logger.exception(
            "plate thumbnail render failed; writing the 3MF without cover images",
            extra={"faces": faces},
        )
        return None, [THUMBNAIL_FAILED_WARNING]
    return rendered, []


@contextmanager
def staged_assets(
    schema: CustomizerSchema,
    params: Mapping[str, ParamValue],
    model_dir: Path,
    store: AssetStore,
) -> Iterator[dict[str, ParamValue]]:
    """``params`` with each uploaded file copied beside the model (#204).

    OpenSCAD resolves `import()` and `surface()` relative to the file that calls
    them, so the copy goes into the model's own directory -- where every wrapper
    render of §6.3 runs too, which is what keeps the closed parts from silently
    losing the picture. The name is generated and bare, so a template that guards
    its file parameter against paths still takes it. Each render gets its own
    copies: two renders of one model overlap routinely, and a shared name would be
    deleted from under the one still running.
    """
    staged = dict(params)
    created: list[Path] = []
    try:
        for name, meta in file_assets(schema, params, store, model_dir).items():
            target = model_dir / f"{STAGED_ASSET_PREFIX}{secrets.token_hex(8)}.{meta.kind}"
            shutil.copyfile(store.blob_path(meta), target)
            created.append(target)
            staged[name] = target.name
        yield staged
    finally:
        for path in created:
            path.unlink(missing_ok=True)


@dataclass(frozen=True)
class ModelSource:
    """What a render or a schema read works from: a `.scad`, where its derived
    schema is cached, and which revision the two belong to."""

    scad: Path
    schema_cache: Path
    version: str | None
    #: The checkouts of the libraries this revision declares, at the pins it goes
    #: with (#93): its whole OPENSCADPATH.
    library_path: tuple[Path, ...] = ()

    def configure(self, config: Config) -> Config:
        """``config`` for every openscad call made on this source."""
        return replace(config, library_path=self.library_path)


async def source_directory(
    slug: str, requested: str | None, *, paths: DataPaths, history: ModelHistory | None
) -> tuple[Path, str | None]:
    """The model directory a revision's files are read from, and that revision: the
    live directory for the current revision (or none requested), otherwise an export.
    Nothing about the libraries it pins, so a caller that only reads its files (a
    template's ``ui/``) works while a pinned checkout is missing and cannot be fetched.
    """
    current = (
        await asyncio.to_thread(history.last_commit, model_path(slug))
        if history is not None and history.available
        else None
    )
    if requested is None or requested == current:
        return paths.model_dir(slug), current
    directory = paths.model_revision_dir(slug, requested)
    if (directory / SOURCE_NAME).is_file():
        # Mark it used, so `prune_revision_exports` evicts by LAST USE rather
        # than by export time and cannot take an old revision out from under a
        # render that is still browsing it.
        await asyncio.to_thread(_touch, directory)
    else:
        # A worker on the bambuddy store has no history: a snapshot it materialized is
        # a populated export and needs none. Without one here (the prune took it after
        # `materialize`), there is nothing to export it from: say so; a retry brings the
        # snapshot in again.
        if history is None or not history.available:
            raise SnapshotUnavailableError(
                f"the export of {slug}@{requested} is gone and there is no history to"
                " export it from"
            )
        await asyncio.to_thread(export_revision, history, slug, requested, directory)
    return directory, requested


async def resolve_source(
    slug: str,
    requested: str | None,
    *,
    paths: DataPaths,
    history: ModelHistory | None,
    fetcher: CheckoutFetcher | None = None,
) -> ModelSource:
    """Resolve a model to the source a render reads: the live one, or an export of
    an older revision.

    `last_commit` is read ONCE here, and the resolved revision comes back on the
    result so a caller does not have to ask again to stamp `model_version`.

    The export is an ordinary model directory under ``data/cache``, so the
    renderer -- including the wrapper `render_solids` drops next to the source --
    works on it unchanged, and nothing generated lands in the repository. Commits
    are immutable, so a populated export is never stale.

    With a ``fetcher``, a pinned library checkout missing from the volume is cloned
    back into place rather than failing the resolve (#169).
    """
    directory, version = await source_directory(slug, requested, paths=paths, history=history)
    if directory == paths.model_dir(slug):
        return ModelSource(
            scad=paths.model_source(slug),
            schema_cache=paths.model_schema_cache(slug),
            version=version,
            # Off the loop: `model.json` and each checkout are reads
            # on the same PVC the history's calls are offloaded for.
            library_path=await resolve_search_path(
                fetcher, partial(model_search_path, paths, slug)
            ),
        )
    # The pins that revision declares, not the live model's: an old revision
    # renders against the library versions it was written with.
    return ModelSource(
        scad=directory / SOURCE_NAME,
        schema_cache=directory / SCHEMA_CACHE_NAME,
        version=version,
        library_path=await resolve_search_path(
            fetcher, partial(revision_search_path, paths, directory)
        ),
    )


def _touch(directory: Path) -> None:
    with suppress(OSError):
        os.utime(directory)


#: Marks a revision export used, so `prune_revision_exports` evicts by last use.
touch_export = _touch


class SnapshotUnavailableError(RuntimeError):
    """No snapshot is stored and this process has no git history to make one."""


def prune_revision_exports(paths: DataPaths, ttl: float, *, now: float | None = None) -> list[str]:
    """Evict revision exports nobody has rendered from in ``ttl`` seconds.

    `cache/schema/` needs none of this -- one file per slug, overwritten in
    place -- but every distinct `{slug, commit}` anyone opens "Customize this
    version" on writes a directory that is otherwise kept forever, on a 5 Gi
    PVC, for a feature whose whole point is browsing arbitrary old revisions.
    Losing one costs a `git archive`, so this mirrors the TTL sweep `jobs/`
    already gets, on the same clock and the same two trigger points.
    """
    root = paths.model_revisions
    if not root.is_dir():
        return []
    cutoff = (now if now is not None else time.time()) - ttl
    removed: list[str] = []
    for slug_dir in sorted(root.iterdir()):
        if not slug_dir.is_dir():
            continue
        for export in sorted(slug_dir.iterdir()):
            if not export.is_dir() or export.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(export, ignore_errors=True)
            removed.append(f"{slug_dir.name}/{export.name}")
        with suppress(OSError):
            slug_dir.rmdir()  # only when it emptied
    return removed


def export_revision(history: ModelHistory, slug: str, version: str, directory: Path) -> None:
    """Export beside the destination, then move it into place.

    Renders are debounced, so two of the same revision overlap routinely, and a
    reader that finds `model.scad` present while the other writer is still
    extracting `model.json` would render against half a revision.
    """
    staging = directory.with_name(f"{directory.name}.{os.getpid()}.{threading.get_ident()}")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        history.export(model_path(slug), version, staging)
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staging, directory)
        except OSError:
            # Another writer got there first; its copy is just as good.
            if not (directory / SOURCE_NAME).is_file():
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@asynccontextmanager
async def library_lease(
    checkouts: CheckoutGate | None, holder: str, library_path: Sequence[Path]
) -> AsyncIterator[None]:
    """A lease on the checkouts a render resolved, when there are any to hold.
    Released when the attempt's render ends however it ends -- done, failed,
    cancelled -- and per attempt: a retry of the same job after a lapsed lease
    holds its own, so the first attempt finishing late never releases it."""
    if checkouts is None or not library_path:
        yield
        return
    async with checkouts.rendering(holder, library_path):
        # A removal that ran between resolving and leasing took one away.
        require_checkouts(library_path)
        yield


def no_stage(name: RenderStage) -> AbstractContextManager[None]:
    """A stage that neither reports nor times anything: a stage run on its own."""
    return nullcontext()


def timed_stage(metrics: Metrics | None) -> Callable[[RenderStage], AbstractContextManager[None]]:
    """A stage timed into `stage_duration`, as `render_job`'s are; untimed without
    metrics."""
    return metrics.stage if metrics is not None else no_stage


@dataclass(frozen=True)
class Prepared:
    """The source a piece renders, as `resolve_source` settled it: plain values, so
    a stage in another process can take it up."""

    scad: Path
    version: str
    library_path: tuple[Path, ...]
    schema_cache: Path


async def prepare_source(
    slug: str,
    revision: str | None,
    *,
    config: Config,
    paths: DataPaths,
    history: ModelHistory | None,
    fetcher: CheckoutFetcher | None,
) -> tuple[Prepared, Config]:
    """The source, its revision, and ``config`` for every openscad call made on it."""
    source = await resolve_source(slug, revision, paths=paths, history=history, fetcher=fetcher)
    # #90 stamps the model's own commit id, which `provenance.source_version`
    # was written to accept (a free string, never a structured field). The
    # content hash remains the answer when there is no repository to name a
    # revision -- and it hashes what was actually rendered, which for an old
    # revision is its export, not the live model directory. Reads every file
    # under it; off the loop, like the other two.
    version = source.version
    config = source.configure(config)
    if version is None:
        version = await asyncio.to_thread(source_version, source.scad.parent)
    return Prepared(source.scad, version, source.library_path, source.schema_cache), config


async def _render_main(
    prepared: Prepared,
    schema: CustomizerSchema,
    staged: Mapping[str, ParamValue],
    params: Mapping[str, ParamValue],
    work: Path,
    *,
    config: Config,
) -> ProcessOutput:
    try:
        return await render_3mf(
            prepared.scad, schema, staged, work / RAW_RENDER_NAME, config=config
        )
    except OpenSCADError as error:
        error.warnings = failed_render_warnings(error.missing_files, schema, params)
        raise


async def _render_solids(
    prepared: Prepared,
    schema: CustomizerSchema,
    staged: Mapping[str, ParamValue],
    params: Mapping[str, ParamValue],
    work: Path,
    output: ProcessOutput,
    *,
    config: Config,
    stage: Callable[[RenderStage], AbstractContextManager[None]],
) -> PlateLayout:
    with stage("split"):
        preview_parts = extruder_order(split_by_material(work / RAW_RENDER_NAME), schema, staged)
        if not preview_parts:
            raise OpenSCADError(
                "the render produced no geometry",
                output.log_tail,
                diagnostics=output.diagnostics,
                diagnostics_dropped=output.diagnostics_dropped,
                missing_files=output.missing_files,
                warnings=failed_render_warnings(output.missing_files, schema, params),
            )
        write_glb(preview_parts, work / PREVIEW_NAME)

    with stage("solids"):
        # One plate unless the template asked for more (spec §6.4); every plate
        # beyond the ordinary render is rendered and solidified here.
        layout = await plate_layout(
            prepared.scad, schema, staged, preview_parts, output.plates or 1, work, config=config
        )
    layout.save(work / LAYOUT_NAME)
    return layout


async def render_main(
    prepared: Prepared,
    params: Mapping[str, ParamValue],
    work: Path,
    *,
    config: Config,
    assets: AssetStore,
    checkouts: CheckoutGate | None,
    holder: str,
    stage: Callable[[RenderStage], AbstractContextManager[None]] = no_stage,
) -> ProcessOutput:
    """The customizer schema, then the raw multi-material 3MF, `RAW_RENDER_NAME` in
    ``work``."""
    async with library_lease(checkouts, holder, prepared.library_path):
        schema = await cached_schema(prepared.scad, prepared.schema_cache, config=config)
        work.mkdir(parents=True, exist_ok=True)
        with (
            staged_assets(schema, params, prepared.scad.parent, assets) as staged,
            stage("render"),
        ):
            return await _render_main(prepared, schema, staged, params, work, config=config)


async def render_solids_stage(
    prepared: Prepared,
    params: Mapping[str, ParamValue],
    work: Path,
    output: ProcessOutput,
    *,
    config: Config,
    assets: AssetStore,
    checkouts: CheckoutGate | None,
    holder: str,
    stage: Callable[[RenderStage], AbstractContextManager[None]] = no_stage,
) -> PlateLayout:
    """The preview parts and `PREVIEW_NAME`, then one closed solid per colour per
    plate, from `RAW_RENDER_NAME` in ``work``. The layout is saved as `LAYOUT_NAME`
    beside the files it names, which is what :func:`finish_piece_stage` reads."""
    async with library_lease(checkouts, holder, prepared.library_path):
        schema = await cached_schema(prepared.scad, prepared.schema_cache, config=config)
        with staged_assets(schema, params, prepared.scad.parent, assets) as staged:
            return await _render_solids(
                prepared, schema, staged, params, work, output, config=config, stage=stage
            )


async def finish_piece_stage(
    prepared: Prepared,
    params: Mapping[str, ParamValue],
    work: Path,
    output: ProcessOutput,
    *,
    config: Config,
    paths: DataPaths,
    slug: str,
    thumbnail_executor: Executor | None,
    stage: Callable[[RenderStage], AbstractContextManager[None]] = no_stage,
    schema: CustomizerSchema | None = None,
) -> JobResult:
    """The cover images and `MODEL_NAME`, from `LAYOUT_NAME` and `PREVIEW_NAME` in
    ``work``. ``schema`` is the one the render used, when the caller derived it
    under its lease; otherwise it is derived here."""
    layout = await asyncio.to_thread(PlateLayout.load, work / LAYOUT_NAME)
    if schema is None:
        schema = await cached_schema(prepared.scad, prepared.schema_cache, config=config)
    # Exit 0 with the picture missing is otherwise invisible: the preview simply
    # has no overlay, and nothing says why.
    warnings = [
        *(MISSING_FILE_WARNING.format(name=name) for name in output.missing_files),
        *layout.warnings,
    ]
    with stage("thumbnail"):
        thumbnails, thumbnail_warnings = await plates_thumbnails(
            [plate.parts for plate in layout.plates], config=config, executor=thumbnail_executor
        )
    warnings += thumbnail_warnings
    warnings += unreadable_colour_warnings(schema, params)

    model_3mf = work / MODEL_NAME
    # A built-in's bare slug, as download_filename names the file: the id's
    # `builtin:` prefix is not something to show as the model's title.
    with stage("write"):
        await asyncio.to_thread(
            write_plates_3mf,
            layout.plates,
            layout.colours,
            model_3mf,
            thumbnails=thumbnails,
            model_name=slug.removeprefix(BUILTIN_PREFIX),
        )

    return JobResult(
        model_3mf=str(model_3mf.relative_to(paths.root)),
        preview_glb=str((work / PREVIEW_NAME).relative_to(paths.root)),
        source_version=prepared.version,
        parts=result_parts(layout),
        bbox_mm=layout.bbox,
        colors=list(layout.colours),
        warnings=warnings,
        plates=result_plates(layout),
        diagnostics=list(output.diagnostics),
        diagnostics_dropped=output.diagnostics_dropped,
        notes=list(output.notes),
    )


async def render_job(
    job: Job,
    *,
    config: Config,
    paths: DataPaths,
    assets: AssetStore,
    history: ModelHistory | None = None,
    thumbnail_executor: Executor | None = None,
    metrics: Metrics | None = None,
    checkouts: CheckoutGate | None = None,
    on_stage: Callable[[RenderStage], None] | None = None,
    fetcher: CheckoutFetcher | None = None,
) -> tuple[JobResult, list[str]]:
    def stage(name: RenderStage) -> AbstractContextManager[None]:
        if on_stage is not None:
            on_stage(name)
        return metrics.stage(name) if metrics is not None else nullcontext()

    # Resolved again rather than carried on the job: the model can be edited
    # between submit and render, and a stored "this one is live" flag would then
    # render newer source while claiming the older revision.
    async with AsyncExitStack() as held:
        with stage("source"):
            # One timed stage for all of it: resolving the source and deriving its
            # schema are the same step.
            prepared, config = await prepare_source(
                job.slug,
                job.model_version,
                config=config,
                paths=paths,
                history=history,
                fetcher=fetcher,
            )
            # Held from here for every openscad run below -- the schema derivation
            # included: those are what read the checkouts on OPENSCADPATH, and a
            # removal must not take one out from under them (#253). One lease and
            # one staging for the render and the solids, where the stages each
            # take their own.
            await held.enter_async_context(library_lease(checkouts, job.id, prepared.library_path))
            schema = await cached_schema(prepared.scad, prepared.schema_cache, config=config)
        # The whole pipeline in one process is the tests' harness now; the worker
        # runs these stages as activities, each in a scratch dir of its own.
        work = paths.cache / "render-job" / job.id
        work.mkdir(parents=True, exist_ok=True)

        with staged_assets(schema, job.params, prepared.scad.parent, assets) as params:
            with stage("render"):
                output = await _render_main(
                    prepared, schema, params, job.params, work, config=config
                )
            await _render_solids(
                prepared, schema, params, job.params, work, output, config=config, stage=stage
            )
    result = await finish_piece_stage(
        prepared,
        job.params,
        work,
        output,
        config=config,
        paths=paths,
        slug=job.slug,
        thumbnail_executor=thumbnail_executor,
        stage=stage,
        schema=schema,
    )
    return result, output.log_tail


#: What Retry-After says: a render's typical time, before the queue is measured.
INITIAL_RENDER_ESTIMATE = 10.0
