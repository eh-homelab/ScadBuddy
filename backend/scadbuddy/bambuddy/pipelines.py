"""Pipeline selection: which of Bambuddy's slicer pipelines to print an output with.

ScadBuddy owns no slicing settings. A pipeline is Bambuddy's own object — printer
preset + process preset (where the nozzle diameter lives) + one filament preset per
slot + a bed type, aimed at a printer or a printer *class* — and everything here is
either a read of those, a pass-through create, or a run.

Three things about Bambuddy's model shape this module:

* **``check-eligibility`` is a 200 carrying the report**; only ``run`` turns the same
  report into a 409. So the picker can filter on eligibility without risking a print.
* **Under ``target_kind="printer_class"`` a report's ``ok`` means *at least one*
  printer passes**, and the per-printer reasons live in ``printer_reports``. That is
  why :class:`PipelineView` resolves the target to a list of printer ids: with more
  than one, the picker has to ask which printer's reasons it is showing.
* **``PipelineRunRequest`` carries no printer**, so a class-targeted run cannot be
  pinned to the printer the picker asked about — Bambuddy fans out by
  ``fanout_strategy`` and reports what it chose in ``PipelineRun.jobs[]``. The chosen
  printer therefore scopes what the UI *shows*, not where the print goes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import slice_and_queue, target_of
from scadbuddy.bambuddy.errors import not_configured
from scadbuddy.bambuddy.filaments import (
    FilamentOptions,
    FilamentPlan,
    FilamentWarning,
    check,
    gather_options,
    queue_filaments,
    slice_filament_presets,
)
from scadbuddy.bambuddy.models import (
    EligibilityReport,
    EligibilityRequest,
    FanoutStrategy,
    LocalPreset,
    Pipeline,
    PipelineCreate,
    PipelineRun,
    PipelineRunRequest,
    Preset,
    PresetRef,
    Printer,
    TargetKind,
)
from scadbuddy.bambuddy.send import ensure_uploaded
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.library.settings_store import StoredSettings

logger = logging.getLogger(__name__)

QUEUE_PATH = "/queue"

#: Bambuddy documents these in ``SliceRequest.bed_type``'s own description as the
#: canonical BambuStudio / OrcaSlicer values. The field itself is a free 64-character
#: string, not an enum, so this is a menu rather than a validation rule — an unknown
#: value a pipeline already carries is still shown as it stands.
BED_TYPES: tuple[str, ...] = (
    "Cool Plate",
    "Cool Plate (SuperTack)",
    "Supertack Plate",
    "Engineering Plate",
    "High Temp Plate",
    "Textured PEI Plate",
    "Smooth PEI Plate",
)


class PresetChoice(BaseModel):
    """One row of the "New pipeline" form's pickers.

    ``compatible_printers`` is normalised to a list here: ``/slicer/presets`` returns
    one, while ``/local-presets/`` stores the same thing as a JSON-encoded *string*.
    An empty list means the preset declares no restriction, not that it fits nothing.
    """

    ref: PresetRef
    name: str
    filament_type: str | None = None
    filament_colour: str | None = None
    compatible_printers: list[str] = Field(default_factory=list)


class PresetOptions(BaseModel):
    """Printer presets and bed types always; process and filament only once a printer
    preset is named, because unfiltered those two tiers are thousands of rows."""

    printer: list[PresetChoice] = Field(default_factory=list)
    process: list[PresetChoice] = Field(default_factory=list)
    filament: list[PresetChoice] = Field(default_factory=list)
    bed_types: list[str] = Field(default_factory=lambda: list(BED_TYPES))
    #: Echoed back so the browser can tell a filtered answer from an unfiltered one.
    printer_preset: PresetRef | None = None


class PipelineView(BaseModel):
    """A pipeline as the picker shows it: Bambuddy's row plus resolved preset names and
    the printers its target comes out as."""

    id: int
    name: str
    description: str | None = None
    bed_type: str | None = None
    target_kind: TargetKind
    target_printer_id: int | None = None
    target_printer_name: str | None = None
    target_model_class: str | None = None
    fanout_strategy: FanoutStrategy
    printer_preset: PresetRef | None = None
    process_preset: PresetRef | None = None
    filament_presets: list[PresetRef] = Field(default_factory=list)
    #: ``None``/``[]`` where a ref could not be resolved — Bambuddy has no
    #: preset-by-id route, so a name comes from the catalogue or not at all.
    printer_preset_name: str | None = None
    process_preset_name: str | None = None
    filament_preset_names: list[str | None] = Field(default_factory=list)
    #: The target as printer ids: one for ``specific_printer``, every active printer of
    #: the class for ``printer_class``. More than one means the picker must ask.
    printer_ids: list[int] = Field(default_factory=list)


class PipelineChoices(BaseModel):
    pipelines: list[PipelineView] = Field(default_factory=list)
    printers: list[Printer] = Field(default_factory=list)
    #: This model's own default, the global fallback, and the one that would be used.
    model_pipeline_id: int | None = None
    global_pipeline_id: int | None = None
    default_pipeline_id: int | None = None


class PipelineDefault(BaseModel):
    slug: str
    pipeline_id: int | None = None
    global_pipeline_id: int | None = None


class PipelineReport(BaseModel):
    """Bambuddy's report for one pipeline, passed through as it came.

    ``report`` is ``None`` exactly when ``error`` is set: that pipeline could not be
    judged, which is neither ready nor blocked, and the picker says so rather than
    guessing either way. That either-or is enforced below rather than merely described,
    because the browser branches on it: a row with neither would render as silently
    absent, and one with both would claim two states at once.
    """

    pipeline_id: int
    report: EligibilityReport | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _either_a_report_or_a_reason(self) -> PipelineReport:
        # ``not self.error`` rather than ``is None``: a blank reason is no reason, and it
        # would reach the browser as a row with nothing to say either way.
        if (self.report is None) == (not self.error):
            raise ValueError(
                "a pipeline report carries either a report or a non-empty error, "
                "never both or neither"
            )
        return self


class EligibilityOverview(BaseModel):
    library_file_id: int
    reports: list[PipelineReport] = Field(default_factory=list)


class PrintRunRequest(BaseModel):
    """``pipeline_id`` omitted means "whatever this model defaults to".

    ``force`` is the caller's explicit override of a blocking eligibility issue; the UI
    only offers it once the issues have been shown.

    ``filament_plan`` is what makes this request choose its route rather than its
    caller. A plan names one spool per plate slot, and those three queue-item fields
    exist on no other Bambuddy call — so a request carrying one is sliced and queued,
    and one without one runs the pipeline exactly as it did before (#87).
    """

    pipeline_id: int | None = None
    copies: int = Field(default=1, ge=1, le=1000)
    force: bool = False
    #: Only meaningful with a plan: it is the printer whose trays the mapping addresses.
    printer_id: int | None = None
    filament_plan: FilamentPlan | None = None
    plate_id: int = Field(default=1, ge=1)


class PrintRunResult(BaseModel):
    """One shape for both routes, so the caller need not know which one ran.

    ``route`` says which it was, and exactly one of ``run`` / ``queue_item_ids`` is
    populated: a pipeline run reports its copies through ``jobs[]``, while a queued item
    is a single row carrying ``quantity``. Following either to completion is #89.
    """

    pipeline_id: int
    library_file_id: int
    route: Literal["pipeline", "slice_queue"] = "pipeline"
    #: Verbatim, including ``jobs[]`` — which printer each copy landed on is Bambuddy's
    #: answer, not ScadBuddy's choice. ``None`` on the slice-and-queue route.
    run: PipelineRun | None = None
    slice_job_id: int | None = None
    sliced_library_file_id: int | None = None
    queue_item_ids: list[int] = Field(default_factory=list)
    #: The printer the copies will actually print on, where that is knowable. On a
    #: class-targeted pipeline run it is not — Bambuddy fans out and reports per copy.
    printer_id: int | None = None
    #: ScadBuddy's own advisories about the chosen filaments, carried through so the
    #: dialog can keep showing them after the click.
    warnings: list[FilamentWarning] = Field(default_factory=list)
    bambuddy_url: str


def _local_list(raw: str | None) -> list[str]:
    """``compatible_printers`` on a local preset is a JSON-encoded string."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        # Bambuddy stores the OrcaSlicer column verbatim; a non-JSON one is not fatal.
        logger.info("a local preset's compatible_printers was not JSON")
        return []
    return [str(entry) for entry in parsed] if isinstance(parsed, list) else []


def _choice(preset: Preset) -> PresetChoice:
    return PresetChoice(
        ref=PresetRef(source=preset.source, id=preset.id),
        name=preset.name or f"{preset.source}:{preset.id}",
        filament_type=preset.filament_type,
        filament_colour=preset.filament_colour,
        compatible_printers=list(preset.compatible_printers or []),
    )


def _local_choice(preset: LocalPreset) -> PresetChoice:
    colours = _local_list(preset.default_filament_colour)
    return PresetChoice(
        ref=preset.ref(),
        name=preset.name,
        filament_type=preset.filament_type,
        filament_colour=colours[0] if colours else None,
        compatible_printers=_local_list(preset.compatible_printers),
    )


def _fits(choice: PresetChoice, printer_preset_name: str | None) -> bool:
    """A preset with no ``compatible_printers`` declares no restriction."""
    if not choice.compatible_printers:
        return True
    if printer_preset_name is None:
        return True
    return printer_preset_name in choice.compatible_printers


@dataclass(frozen=True)
class _Catalogue:
    """Every preset Bambuddy offers, normalised into :class:`PresetChoice` rows.

    Both catalogues go in: ``/local-presets/`` (OrcaSlicer imports) is *not* the
    ``local`` tier of ``/slicer/presets``, and a user's own filament profile lives only
    there.
    """

    printer: list[PresetChoice]
    process: list[PresetChoice]
    filament: list[PresetChoice]

    def names(self) -> dict[tuple[str, str], str]:
        """``(source, id) -> name``, for resolving the refs a pipeline carries."""
        return {
            (choice.ref.source, choice.ref.id): choice.name
            for choice in (*self.printer, *self.process, *self.filament)
        }


async def _catalogue(client: BambuddyClient) -> _Catalogue:
    catalogue = await client.presets()
    local = await client.local_presets()
    printers: list[PresetChoice] = []
    processes: list[PresetChoice] = []
    filaments: list[PresetChoice] = []
    for tier in (catalogue.cloud, catalogue.standard, catalogue.local, catalogue.orca_cloud):
        printers.extend(_choice(preset) for preset in tier.printer)
        processes.extend(_choice(preset) for preset in tier.process)
        filaments.extend(_choice(preset) for preset in tier.filament)
    printers.extend(_local_choice(row) for row in local.printer)
    processes.extend(_local_choice(row) for row in local.process)
    filaments.extend(_local_choice(row) for row in local.filament)
    return _Catalogue(printer=printers, process=processes, filament=filaments)


async def preset_options(
    client: BambuddyClient, *, printer_preset: PresetRef | None = None
) -> PresetOptions:
    """The presets a new pipeline can be built from.

    Without ``printer_preset`` only the printer tier is returned: the live instance has
    ~4000 process and filament presets across tiers, which is not a payload to hand a
    browser so it can filter client-side. With one, process and filament are filtered
    to those whose ``compatible_printers`` names it.
    """
    catalogue = await _catalogue(client)
    if printer_preset is None:
        return PresetOptions(printer=catalogue.printer, bed_types=list(BED_TYPES))

    chosen = next((choice for choice in catalogue.printer if choice.ref == printer_preset), None)
    name = chosen.name if chosen else None
    return PresetOptions(
        printer=catalogue.printer,
        process=[choice for choice in catalogue.process if _fits(choice, name)],
        filament=[choice for choice in catalogue.filament if _fits(choice, name)],
        bed_types=list(BED_TYPES),
        printer_preset=printer_preset,
    )


def _names(catalogue_by_ref: dict[tuple[str, str], str], ref: PresetRef | None) -> str | None:
    if ref is None:
        return None
    return catalogue_by_ref.get((ref.source, ref.id))


def _target_printer_ids(pipeline: Pipeline, printers: list[Printer]) -> list[int]:
    if pipeline.target_kind == "specific_printer":
        return [pipeline.target_printer_id] if pipeline.target_printer_id is not None else []
    model = pipeline.target_model_class
    if model is None:
        return [printer.id for printer in printers if printer.is_active]
    return [printer.id for printer in printers if printer.is_active and printer.model == model]


def _view(
    pipeline: Pipeline,
    printers: list[Printer],
    preset_names: dict[tuple[str, str], str],
) -> PipelineView:
    by_id = {printer.id: printer for printer in printers}
    target = by_id.get(pipeline.target_printer_id) if pipeline.target_printer_id else None
    return PipelineView(
        id=pipeline.id,
        name=pipeline.name,
        description=pipeline.description,
        bed_type=pipeline.bed_type,
        target_kind=pipeline.target_kind,
        target_printer_id=pipeline.target_printer_id,
        target_printer_name=target.name if target else None,
        target_model_class=pipeline.target_model_class,
        fanout_strategy=pipeline.fanout_strategy,
        printer_preset=pipeline.printer_preset,
        process_preset=pipeline.process_preset,
        filament_presets=list(pipeline.filament_presets),
        printer_preset_name=_names(preset_names, pipeline.printer_preset),
        process_preset_name=_names(preset_names, pipeline.process_preset),
        filament_preset_names=[_names(preset_names, ref) for ref in pipeline.filament_presets],
        printer_ids=_target_printer_ids(pipeline, printers),
    )


async def describe_pipelines(
    client: BambuddyClient, settings: StoredSettings, slug: str
) -> PipelineChoices:
    """Every pipeline, with its presets named and its target resolved to printers."""
    pipelines = await client.pipelines()
    printers = await client.printers()
    # Bambuddy has no preset-by-id route, so naming the five refs a pipeline carries
    # means reading the whole catalogue. It is read server-side; only the names travel.
    preset_names = (await _catalogue(client)).names()
    return PipelineChoices(
        pipelines=[_view(pipeline, printers, preset_names) for pipeline in pipelines],
        printers=printers,
        model_pipeline_id=settings.model_pipelines.get(slug),
        global_pipeline_id=settings.pipeline_id,
        default_pipeline_id=settings.pipeline_for(slug),
    )


async def create_pipeline(client: BambuddyClient, request: PipelineCreate) -> PipelineView:
    """Pass-through create, then re-read the printers so the new row's target resolves.

    ``SlicerPipelineCreate`` carries no target fields, so Bambuddy targets the new
    pipeline itself; the view reports what it chose rather than what was asked for.
    """
    created = await client.create_pipeline(request)
    printers = await client.printers()
    preset_names = (await _catalogue(client)).names()
    return _view(created, printers, preset_names)


async def check_pipelines(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    pipeline_ids: list[int] | None = None,
) -> EligibilityOverview:
    """Ask Bambuddy whether each pipeline would refuse this file, without printing.

    ``pipeline_ids`` omitted means every pipeline. Nothing here is a 409: an ineligible
    answer is a 200 carrying the report, so the picker can mark a row not-ready rather
    than discover the problem on Run.

    The checks run **concurrently and independently**: the picker cannot open until the
    last of them answers, so a sequential loop would cost the sum of every pipeline's
    latency rather than the slowest one's — and one pipeline Bambuddy cannot judge must
    not blank every row that did answer. ``gather`` keeps the order of ``pipeline_ids``;
    ``return_exceptions`` is what keeps a single failure local to its own row.

    Concurrency is deliberately unbounded. A Bambuddy holds a handful of pipelines, and
    the client's own connection pool is the limit that matters; a semaphore here would be
    a guess about a number nothing has yet needed.
    """
    meta, library_file_id = await ensure_uploaded(client, store, meta, settings)
    if pipeline_ids is None:
        pipeline_ids = [pipeline.id for pipeline in await client.pipelines()]
    request = EligibilityRequest(source_library_file_id=library_file_id)
    checks = await asyncio.gather(
        *(client.check_eligibility(pipeline_id, request) for pipeline_id in pipeline_ids),
        return_exceptions=True,
    )
    reports: list[PipelineReport] = []
    for pipeline_id, outcome in zip(pipeline_ids, checks, strict=True):
        if isinstance(outcome, EligibilityReport):
            reports.append(PipelineReport(pipeline_id=pipeline_id, report=outcome))
            continue
        if not isinstance(outcome, ApiError):
            # Anything that is not the client's own mapped failure is a bug here, not a
            # pipeline that cannot be judged, so it is not swallowed into a row.
            raise outcome
        logger.info(
            "a pipeline could not be judged for eligibility",
            extra={"pipeline_id": pipeline_id, "status": outcome.status},
        )
        reports.append(PipelineReport(pipeline_id=pipeline_id, error=outcome.detail))
    return EligibilityOverview(library_file_id=library_file_id, reports=reports)


def filament_preset_index(catalogue: _Catalogue) -> dict[str, PresetRef]:
    """``preset id -> ref``, so a spool's ``slicer_filament`` string can be resolved.

    The inventory stores the preset as a bare id ("GFG00", or a local preset's row id
    "2") with no tier, while a :class:`PresetRef` needs both. The catalogue is the only
    place the tier is written down, so an id it does not hold cannot be turned into a
    ref — and is left as the pipeline's own preset rather than guessed at.

    A cloud id and a local id could in principle collide; the first tier read wins and
    the later one is ignored, which matches ``_catalogue``'s own ordering.
    """
    index: dict[str, PresetRef] = {}
    for choice in catalogue.filament:
        index.setdefault(choice.ref.id, choice.ref)
    return index


async def filament_options_for_output(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    printer_id: int | None = None,
    pipeline_id: int | None = None,
    plate_id: int = 1,
) -> FilamentOptions:
    """The filament step's whole payload for one output (#87).

    Uploads the 3MF if Bambuddy has not got it, for the same reason the eligibility
    check does: the plate's slots are read out of a *library file*, so there is no
    answer before one exists. An output is immutable, so this uploads once.

    The pipeline is read only to name its process preset, which is where the nozzle
    diameter is written; without one the nozzle rules stay quiet rather than comparing
    against a default nobody chose.
    """
    meta, library_file_id = await ensure_uploaded(client, store, meta, settings)
    process_preset_name: str | None = None
    resolved = pipeline_id or settings.pipeline_for(meta.slug)
    if resolved is not None:
        pipeline = next(
            (row for row in await client.pipelines() if row.id == resolved),
            None,
        )
        if pipeline is not None:
            names = (await _catalogue(client)).names()
            process_preset_name = _names(names, pipeline.process_preset)
    return await gather_options(
        client,
        library_file_id=library_file_id,
        printer_id=printer_id,
        plate_id=plate_id,
        process_preset_name=process_preset_name,
        fallback_colours=list(meta.colors),
    )


async def run_for_output(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
    request: PrintRunRequest,
) -> PrintRunResult:
    """Print this output, by whichever of Bambuddy's two routes the request needs.

    Without a filament plan this is unchanged from #86: ``POST
    /slicer-pipelines/{id}/run`` with ``copies`` and an explicit ``force``. A blocking
    eligibility issue is Bambuddy's 409, whose report the client passes through
    verbatim, and ``force`` is what turns it into a run recorded as
    ``eligibility_overridden``.

    **With** a plan the run route cannot express it — ``ams_mapping`` and friends live
    only on the queue item — so the plate is sliced from this same pipeline's presets
    and queued against one printer. That is a real difference in behaviour, not an
    implementation detail: a class-targeted pipeline fans out and a queue item does
    not, which is why the dialog says so before the click.
    """
    pipeline_id = request.pipeline_id or settings.pipeline_for(meta.slug)
    if pipeline_id is None:
        raise not_configured(
            "no slicer pipeline is set for this model and there is no default, "
            "so there is nothing to print with"
        )
    meta, library_file_id = await ensure_uploaded(client, store, meta, settings)

    if request.filament_plan is None:
        run = await client.run_pipeline(
            pipeline_id,
            PipelineRunRequest(
                source_library_file_id=library_file_id,
                copies=request.copies,
                force=request.force,
            ),
        )
        store.record_send(meta.id, pipeline_run_id=run.id)
        return PrintRunResult(
            pipeline_id=pipeline_id,
            library_file_id=library_file_id,
            route="pipeline",
            run=run,
            bambuddy_url=client.config.web_url(QUEUE_PATH),
        )

    pipeline = next((row for row in await client.pipelines() if row.id == pipeline_id), None)
    if pipeline is None:
        raise not_configured(
            f"Bambuddy no longer has slicer pipeline {pipeline_id}, so the chosen "
            "filaments cannot be sliced with it"
        )
    printer_id, _ = target_of(pipeline, request.printer_id)
    options = await gather_options(
        client,
        library_file_id=library_file_id,
        printer_id=printer_id,
        plate_id=request.plate_id,
        process_preset_name=_names((await _catalogue(client)).names(), pipeline.process_preset),
        fallback_colours=list(meta.colors),
    )
    warnings = check(options, request.filament_plan, copies=request.copies)
    presets, colours, preset_warnings = slice_filament_presets(
        options,
        request.filament_plan,
        pipeline_presets=list(pipeline.filament_presets),
        resolve=filament_preset_index(await _catalogue(client)),
    )
    outcome = await slice_and_queue(
        client,
        library_file_id=library_file_id,
        pipeline=pipeline,
        printer_id=request.printer_id,
        filament_presets=presets,
        filament_colours=colours,
        filaments=queue_filaments(options, request.filament_plan),
        plate_id=request.plate_id,
        copies=request.copies,
    )
    for queue_item_id in outcome.queue_item_ids:
        store.record_send(meta.id, queue_item_id=queue_item_id)
    return PrintRunResult(
        pipeline_id=pipeline_id,
        library_file_id=library_file_id,
        route="slice_queue",
        slice_job_id=outcome.slice_job_id,
        sliced_library_file_id=outcome.sliced_library_file_id,
        queue_item_ids=outcome.queue_item_ids,
        printer_id=outcome.printer_id,
        warnings=warnings + preset_warnings,
        bambuddy_url=client.config.web_url(QUEUE_PATH),
    )
