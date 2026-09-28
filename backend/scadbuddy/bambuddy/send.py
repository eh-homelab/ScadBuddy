"""The two "send to Bambuddy" flows and the sidebar registration.

Kept out of the route module so they can be tested against respx recordings without
a FastAPI app, and so the route stays a thin adapter.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import NOT_FOUND_PROBLEM, PLATE_FIT_PROBLEM, not_configured
from scadbuddy.bambuddy.models import (
    ExternalLink,
    Pipeline,
    PipelineRunRequest,
    PresetRef,
    Printer,
    QueueItemCreate,
    SliceRequest,
)
from scadbuddy.bambuddy.options import PrintOptions, resolve
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy, SlicedCopy
from scadbuddy.core.problems import ApiError
from scadbuddy.library.deeplink import edit_url, merge_edit_note
from scadbuddy.library.outputs import MODEL_NAME, OutputMeta, OutputStore, download_filename
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.bambu3mf import replate_3mf
from scadbuddy.render.plate import DEFAULT_PLATE as FALLBACK_PLATE
from scadbuddy.render.plate import PlateFitError, PlateGeometry, nozzle_diameter_of, plate_for
from scadbuddy.render.recolour import pin_extruders_3mf, recolour_3mf

logger = logging.getLogger(__name__)

SendMode = Literal["library", "queue"]

SIDEBAR_NAME = "ScadBuddy"
# Earlier builds registered the link as "Customize"; adopt and rename it rather than
# leaving a second entry in Bambuddy's sidebar.
LEGACY_SIDEBAR_NAMES = frozenset({"Customize"})
SIDEBAR_ICON = "shapes"

QUEUE_PATH = "/queue"
LIBRARY_PATH = "/library"

# Two different things are called a "plate" in this module, and they are not
# interchangeable. ``DEFAULT_PLATE`` is the *index* of the plate on the bed, which
# Bambuddy's SliceRequest and QueueItemCreate take (#106). ``FALLBACK_PLATE`` is
# the plate *geometry* the 3MF is laid out against when no printer is known (#105).
DEFAULT_PLATE = 1


class SendRequest(BaseModel):
    mode: SendMode = "library"
    #: The per-send quantity. Left unset it falls back to the remembered ``quantity``
    #: option, and then to Bambuddy's own default of 1.
    copies: int | None = Field(default=None, ge=1, le=1000)
    #: Per-request overrides, the most specific scope. Nothing here is remembered.
    options: PrintOptions = Field(default_factory=PrintOptions)


class SendResult(BaseModel):
    mode: SendMode
    library_file_id: int
    filename: str
    pipeline_run_id: int | None = None
    queue_item_id: int | None = None
    #: Deep link into Bambuddy for what this send produced.
    bambuddy_url: str | None = None
    #: The "Edit in ScadBuddy" link attached to the library file, when one is known.
    edit_url: str | None = None
    #: What was actually sent, after the four scopes were merged. Unset fields were
    #: left to Bambuddy.
    options: PrintOptions = Field(default_factory=PrintOptions)


class SidebarLink(BaseModel):
    """The External Link ScadBuddy registers, plus where Bambuddy renders it."""

    id: int
    name: str
    url: str
    icon: str
    open_in_new_tab: bool
    created: bool
    #: Bambuddy renders an ``open_in_new_tab: false`` link in a sandboxed iframe here.
    embed_path: str


def _read_3mf(store: OutputStore, meta: OutputMeta) -> bytes:
    path: Path = store.directory(meta.id) / MODEL_NAME
    if not path.is_file():
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"output {meta.id!r} has no 3MF to send",
            type_=NOT_FOUND_PROBLEM,
        )
    return path.read_bytes()


@dataclass(frozen=True)
class Target:
    """What the 3MF is laid out for: the target's plate and, when known, its nozzle."""

    plate: PlateGeometry
    #: The nozzle the run chose, or read off the pipeline's printer preset name (#126).
    #: ``None`` — no pipeline, no printer preset, or a name that states no nozzle —
    #: keeps the placeholder.
    nozzle_diameter: str | None = None
    #: One colour per filament, from the spools the run chose (#476). ``None`` — the
    #: send bar, which chooses no spools — keeps the model's own colours.
    colours: tuple[str, ...] | None = None
    #: One physical extruder per filament (0 right, 1 left), from the AMS each chosen
    #: spool is loaded in (#469). ``None`` leaves the choice to the slicer.
    extruders: tuple[int, ...] | None = None

    @property
    def key(self) -> str:
        """Recorded as a copy's ``target_key``: a reused upload has to match both halves.

        Without a nozzle this is the plate's own key, so a file recorded before #126 is
        still reused for the same plate.
        """
        key = self.plate.key
        if self.nozzle_diameter is not None:
            key = f"{key}@{self.nozzle_diameter}"
        if self.colours is not None:
            # A file recoloured for other spools must not be reused for these.
            key = f"{key}~{','.join(self.colours)}"
        if self.extruders is not None:
            # Pinned to other extruders, the same file slices for other nozzles.
            key = f"{key}>{','.join(str(extruder) for extruder in self.extruders)}"
        return key


async def _target_model_and_preset(
    client: BambuddyClient, settings: StoredSettings, slug: str, *, pipeline_id: int | None = None
) -> tuple[str | None, PresetRef | None]:
    """Which printer model this output is heading for, as Bambuddy names it, and the
    printer preset of the pipeline in play.

    The pipeline wins over the globally configured printer, the same precedence
    :meth:`StoredSettings.pipeline_for` gives the print itself. A pipeline aimed
    at a printer *class* names the model directly; one aimed at a specific
    printer has to be resolved through the printer list. A ``None`` model — no pipeline,
    no printer, or a printer Bambuddy reports without a model — is not an error;
    it means the default plate. ``pipeline_id`` names the pipeline actually in play
    when the caller has already chosen one that is not the slug's default — a run
    request may override it (#86), and the plate has to follow the same pipeline the
    print will use, not the one the settings would have picked.
    """
    pipeline_id = pipeline_id if pipeline_id is not None else settings.pipeline_for(slug)
    if pipeline_id is None and settings.printer_id is None:
        # Nothing to resolve against, so do not spend two round trips finding out.
        return None, None
    printers: list[Printer] | None = None
    printer_preset: PresetRef | None = None
    if pipeline_id is not None:
        pipeline = next((row for row in await client.pipelines() if row.id == pipeline_id), None)
        if pipeline is not None:
            printer_preset = pipeline.printer_preset
            if pipeline.target_model_class:
                return pipeline.target_model_class, printer_preset
            if pipeline.target_printer_id is not None:
                printers = await client.printers()
                target = next(
                    (row for row in printers if row.id == pipeline.target_printer_id), None
                )
                if target is not None and target.model:
                    return target.model, printer_preset
    if settings.printer_id is not None:
        printers = printers if printers is not None else await client.printers()
        target = next((row for row in printers if row.id == settings.printer_id), None)
        if target is not None and target.model:
            return target.model, printer_preset
    return None, printer_preset


async def _nozzle_diameter(client: BambuddyClient, preset: PresetRef | None) -> str | None:
    """The nozzle ``preset``'s name states, from ``/slicer/presets`` (#126).

    Bambuddy has no preset-by-id route, and a :class:`PresetRef` carries no name, so
    this reads the catalogue. The value is only *reported* — Bambuddy slices with the
    pipeline's presets, never this field — so a catalogue that cannot be read degrades
    to the placeholder rather than failing the send.
    """
    if preset is None:
        return None
    try:
        catalogue = await client.presets()
    # ValueError covers a 200 whose body is not JSON or not a catalogue (both
    # JSONDecodeError and pydantic's ValidationError subclass it).
    except (ApiError, ValueError):
        logger.warning(
            "could not read the preset catalogue; the 3MF keeps the placeholder nozzle",
            extra={"preset_source": preset.source, "preset_id": preset.id},
        )
        return None
    for tier in (catalogue.cloud, catalogue.standard, catalogue.local, catalogue.orca_cloud):
        for row in tier.printer:
            if row.source == preset.source and row.id == preset.id:
                return nozzle_diameter_of(row.name)
    return None


async def target_for(
    client: BambuddyClient,
    settings: StoredSettings,
    slug: str,
    *,
    pipeline_id: int | None = None,
    printer_id: int | None = None,
    nozzle_diameter: str | None = None,
    colours: Sequence[str] | None = None,
    extruders: Sequence[int] | None = None,
) -> Target:
    """The plate and nozzle the 3MF is laid out for.

    With an explicit ``printer_id`` and ``nozzle_diameter`` — the spool-first run, which
    has chosen both (spec 2026-09-27 §4) — no pipeline is read: the model comes from the
    printer list and the nozzle is the one chosen. Otherwise, as the send bar and the
    eligibility check need, from the pipeline in play. ``colours`` are the chosen spools'
    (#476), and only the spool-first run has any; so are ``extruders`` (#469).
    """
    if printer_id is not None and nozzle_diameter is not None:
        printer = next((row for row in await client.printers() if row.id == printer_id), None)
        model = printer.model if printer is not None else None
        return Target(
            _plate_for_model(model),
            nozzle_diameter,
            tuple(colours) if colours is not None else None,
            tuple(extruders) if extruders is not None else None,
        )
    model, printer_preset = await _target_model_and_preset(
        client, settings, slug, pipeline_id=pipeline_id
    )
    return Target(_plate_for_model(model), await _nozzle_diameter(client, printer_preset))


def _plate_for_model(model: str | None) -> PlateGeometry:
    plate = plate_for(model)
    if model and plate is FALLBACK_PLATE:
        logger.info(
            "no plate geometry for this printer model; using the default plate",
            extra={"printer_model": model},
        )
    return plate


def _laid_out_for(payload: bytes, target: Target) -> bytes:
    """Re-place the 3MF for ``target``, refusing here rather than at the slicer.

    The 3MF was written at render time, when no printer was chosen, so the plate
    it carries is the fallback. Moving it now is what puts the object — and the
    prime tower a multi-colour print needs — where every extruder can reach them.
    """
    try:
        payload = replate_3mf(payload, target.plate, nozzle_diameter=target.nozzle_diameter)
    except PlateFitError as error:
        raise ApiError(status.HTTP_409_CONFLICT, str(error), type_=PLATE_FIT_PROBLEM) from error
    # In the spools' colours, so the plate thumbnail Bambuddy shows is the print (#476).
    if target.colours is not None:
        payload = recolour_3mf(payload, target.colours)
    # On the extruders the spools feed, so no filament is sliced for the other nozzle (#469).
    if target.extruders is None:
        return payload
    try:
        return pin_extruders_3mf(payload, target.extruders)
    except ValueError as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"ScadBuddy can't pin this 3MF's filaments to their extruders: {error}.",
        ) from error


def is_inbox(folder_id: int | None, settings: StoredSettings) -> bool:
    """Whether ``folder_id`` is the inbox: the folder from Settings, where a send with no
    project lands (``None``, the library root, when Settings names none).

    The only folder ScadBuddy ever deletes from (#316). Every other folder a copy is in
    is a project's, and the file there is the user's record of what that project
    printed.
    """
    return folder_id == settings.library_folder_id


async def upload_output(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    target: Target | None = None,
    folder_id: int | None = None,
) -> tuple[int, str]:
    """Upload a new copy of ``model.3mf`` into ``folder_id`` (the inbox when ``None``);
    returns its library file id and file name.

    A folder carries ``project_id``, so the folder is what files the copy under a
    project (#79) and puts it on Bambuddy's project page.

    Only in the inbox does the new copy *supersede* anything: every other copy already
    in the inbox is deleted, so the inbox holds one copy per output rather than one per
    printer it was ever aimed at. A copy in a project's folder is never deleted, even
    when this upload is for another printer — that project printed from it (#316).

    The order matters. The plate fit is decided before anything touches Bambuddy, so a
    model that cannot be laid out refuses with every copy intact. The upload comes
    before the deletes, so there is never a moment with no copy at all. A delete that
    404s was done for us; one that fails otherwise is logged and the copy *stays
    recorded* — the send itself succeeded, and forgetting the id would strand the file
    in Bambuddy with nothing pointing at it. It is tried again the next time an upload
    supersedes it.
    """
    target = target if target is not None else await target_for(client, settings, meta.slug)
    payload = _laid_out_for(_read_3mf(store, meta), target)
    folder = folder_id if folder_id is not None else settings.library_folder_id

    uploaded = await client.upload_library_file(download_filename(meta), payload, folder_id=folder)
    await uploads.record(
        meta.id, LibraryCopy(id=uploaded.id, folder_id=folder, target_key=target.key)
    )
    if is_inbox(folder, settings):
        for copy in await uploads.for_output(meta.id):
            if copy.id == uploaded.id or copy.folder_id != folder:
                continue
            await _delete_copy(client, uploads, meta, copy.id, strict=False)
    return uploaded.id, uploaded.filename


async def _delete_copy(
    client: BambuddyClient,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    library_file_id: int,
    *,
    strict: bool,
) -> None:
    """Delete one copy and forget it once the delete has come back (committed or 404).

    ``strict`` raises a failed delete; otherwise it is logged and the copy stays
    recorded, to be retried. Forgetting it first is never right: a delete that failed
    would leave the file in Bambuddy with nothing pointing at it.
    """
    try:
        await client.delete_library_file(library_file_id)
    except ApiError as error:
        if error.status != status.HTTP_404_NOT_FOUND:
            if strict:
                raise
            logger.warning(
                "could not delete a superseded inbox copy; it stays recorded for the next try",
                extra={"library_file_id": library_file_id, "status": error.status},
            )
            return
        logger.info("the library copy was already gone", extra={"library_file_id": library_file_id})
    await uploads.forget(meta.id, library_file_id)


async def _ensure_copy(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    target: Target | None,
    folder_id: int | None,
) -> tuple[int, str]:
    """:func:`ensure_uploaded`, plus the file name Bambuddy holds the copy under."""
    target = target if target is not None else await target_for(client, settings, meta.slug)
    folder = folder_id if folder_id is not None else settings.library_folder_id
    for copy in await uploads.for_output(meta.id):
        if copy.folder_id != folder or copy.target_key != target.key:
            continue
        # Someone may have deleted it in Bambuddy since. Reusing a dead id would fail
        # the slice or the eligibility check with an upstream 404, so it is read first
        # and a 404 is dropped and uploaded again rather than failing the send.
        try:
            found = await client.library_file(copy.id)
        except ApiError as error:
            if error.status != status.HTTP_404_NOT_FOUND:
                raise
            logger.info(
                "a recorded library copy was deleted in Bambuddy; uploading it again",
                extra={"library_file_id": copy.id},
            )
            await uploads.forget(meta.id, copy.id)
            continue
        return copy.id, found.filename
    return await upload_output(
        client, store, uploads, meta, settings, target=target, folder_id=folder_id
    )


async def ensure_uploaded(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    target: Target | None = None,
    folder_id: int | None = None,
) -> int:
    """The library file id to slice, judge or print, uploading the 3MF where needed.

    ``folder_id`` is the folder the copy has to be in — a project's (#79) — and
    ``None`` means the inbox, the folder from Settings.

    An output is immutable once generated — changing a parameter produces a new one —
    so a recorded copy still describes this exact 3MF. What is *not* immutable is where
    it sits and what it was laid out for, so a copy is reused only where both still
    hold: the same folder, and the same :attr:`Target.key` (the plate, #105, and the
    nozzle, #126). The print picker (#86) leans on the reuse: opening it checks
    eligibility, which needs a file in Bambuddy, and must not upload on every open.

    Anything else uploads a **new copy** there (#316). Never a move: a file sent to
    project A is A's record of what it printed, and A's slices and archives stay in A,
    so moving the file to project B would leave A pointing at nothing. And never a
    delete outside the inbox; see :func:`upload_output`.
    """
    library_file_id, _ = await _ensure_copy(
        client, store, uploads, meta, settings, target=target, folder_id=folder_id
    )
    return library_file_id


async def delete_inbox_copies(
    client: BambuddyClient,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
) -> None:
    """Delete the output's copies that sit in the inbox, before the output itself goes.

    A copy in a project's folder is kept: it is that project's record. Any failure but
    a 404 raises, so the caller keeps the output — and with it the only pointer to a
    file still in Bambuddy — for a retry.

    Slices are left alone: a queued print may still reference one.
    """
    for copy in await uploads.for_output(meta.id):
        if is_inbox(copy.folder_id, settings):
            await _delete_copy(client, uploads, meta, copy.id, strict=True)


async def attach_edit_link(
    client: BambuddyClient,
    library_file_id: int,
    meta: OutputMeta,
    settings: StoredSettings,
) -> str | None:
    """Best-effort: note the "Edit in ScadBuddy" link on the uploaded library file.

    Deliberately the last thing a send does, and deliberately swallowing every
    ApiError. The note is cosmetic — the file is already uploaded and the print
    already queued — so letting a timeout or a rejected note abort the send would
    fail the request for work that had in fact succeeded. Same reasoning as the
    tolerated 404 in upload_output.

    Returns the link only when Bambuddy took it, so the result never claims a link
    that is not actually on the file.
    """
    link = edit_url(settings.public_url, meta.id)
    if link is None:
        return None
    try:
        existing = await client.library_file(library_file_id)
        await client.annotate_library_file(library_file_id, merge_edit_note(existing.notes, link))
    except ApiError:
        logger.warning(
            "could not attach the edit link to the library file",
            extra={"library_file_id": library_file_id, "output_id": meta.id},
        )
        return None
    return link


def _check_colours(meta: OutputMeta, presets: list[PresetRef], what: str) -> None:
    if len(meta.colors) > len(presets):
        raise not_configured(
            f"this output has {len(meta.colors)} colours but {what} provides only "
            f"{len(presets)} filament presets"
        )


def _settings_slice_request(settings: StoredSettings, meta: OutputMeta) -> SliceRequest:
    if settings.printer_preset is None or settings.process_preset is None:
        raise not_configured(
            "no slicer pipeline is configured, and the printer and process presets "
            "needed to slice directly are not set either"
        )
    if not settings.filament_presets:
        raise not_configured(
            "no filament presets are configured; set one per AMS slot, in extruder order"
        )
    _check_colours(meta, settings.filament_presets, "the configured filament presets")
    return SliceRequest(
        printer_preset=settings.printer_preset,
        process_preset=settings.process_preset,
        filament_presets=settings.filament_presets,
        filament_colours=list(meta.colors),
        bed_type=settings.bed_type,
        plate=DEFAULT_PLATE,
    )


def pipeline_slice_request(pipeline: Pipeline, meta: OutputMeta) -> SliceRequest:
    """Slice from the pipeline's own presets, the way a pipeline run would.

    This is what a send carrying print options does *instead of* running the pipeline.
    ``POST /slicer-pipelines/{id}/run`` accepts only a source, ``copies`` and ``force``:
    it answers 202 and its queue entries are created later, in a background task, from
    Bambuddy's own model defaults — so there is no option passthrough and not even a
    queue-entry id to ``PATCH`` by the time it returns. Bambuddy's own
    ``_slice_request_from_pipeline`` reads the same four fields this does.
    """
    if pipeline.printer_preset is None or pipeline.process_preset is None:
        raise not_configured(
            f"slicer pipeline {pipeline.id} has no printer and process presets, so "
            "ScadBuddy cannot slice with it to apply the print options"
        )
    if not pipeline.filament_presets:
        raise not_configured(
            f"slicer pipeline {pipeline.id} has no filament presets, so ScadBuddy "
            "cannot slice with it to apply the print options"
        )
    _check_colours(meta, pipeline.filament_presets, f"slicer pipeline {pipeline.id}")
    return SliceRequest(
        printer_preset=pipeline.printer_preset,
        process_preset=pipeline.process_preset,
        filament_presets=pipeline.filament_presets,
        filament_colours=list(meta.colors),
        bed_type=pipeline.bed_type,
        plate=DEFAULT_PLATE,
    )


def request_scope(copies: int | None, options: PrintOptions) -> PrintOptions:
    """The per-request overlay. ``copies`` is each dialog's own control for the same
    quantity, and wins over an ``options.quantity`` sent alongside it.

    Shared by the send bar and the print picker (#78) for the same reason
    :func:`resolve_print_options` is.
    """
    if copies is None:
        return options
    return options.model_copy(update={"quantity": copies})


def resolve_print_options(
    settings: StoredSettings, slug: str, printer_id: int | None, request_scope: PrintOptions
) -> PrintOptions:
    """global → per-printer → per-model → per-request, least specific first.

    Shared by the send bar and the print picker (#124), so the two can never disagree
    about which remembered option wins.
    """
    return resolve(
        settings.print_options,
        settings.printer_print_options.get(str(printer_id)) if printer_id is not None else None,
        settings.model_print_options.get(slug),
        request_scope,
    )


def _resolve_options(
    settings: StoredSettings, meta: OutputMeta, request: SendRequest, printer_id: int | None
) -> PrintOptions:
    return resolve_print_options(
        settings, meta.slug, printer_id, request_scope(request.copies, request.options)
    )


async def scope_printer(
    settings: StoredSettings,
    request_printer_id: int | None,
    fetch_pipeline: Callable[[], Awaitable[Pipeline]] | None,
) -> tuple[int | None, Pipeline | None]:
    """The printer the per-printer scope keys on, and the pipeline if it had to be read.

    That is the printer ScadBuddy believes it prints to: the one the request names, else
    the configured one, else the pipeline's own target. Not the same thing as the queue
    item's target, which a pipeline owns outright. Shared by the send bar and the print
    picker (#141), each passing its own way of reading the pipeline, or ``None`` when
    there is none to read.

    The pipeline is read only when no printer is named and some per-printer option is
    remembered at all. With a printer named the key is already known; with the map empty
    the key cannot change the resolution. Reading it on any saved override regardless
    would make one override cost every later send an extra GET it does not need, and turn
    that GET's failure into a send failure.
    """
    printer_id = request_printer_id or settings.printer_id
    if printer_id is not None or not settings.printer_print_options or fetch_pipeline is None:
        return printer_id, None
    pipeline = await fetch_pipeline()
    return pipeline.target_printer_id, pipeline


async def _queue_send(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    request: SendRequest,
    library_file_id: int,
    filename: str,
) -> SendResult:
    """Queue mode, after the upload.

    The resolved options decide the path, not the configuration alone: a pipeline run
    cannot carry any of them, so a send that has one takes the slice-and-queue route
    using that same pipeline's presets and target.
    """
    # The Settings pipeline; a legacy per-model one is no longer read.
    pipeline_id = settings.pipeline_for(meta.slug)
    # The printer the *option scopes* key on; see below, where conflating it with the
    # queue item's target pinned a printer-class pipeline to one printer.
    scope_printer_id, pipeline = await scope_printer(
        settings,
        None,
        partial(client.pipeline, pipeline_id) if pipeline_id is not None else None,
    )
    options = _resolve_options(settings, meta, request, scope_printer_id)

    if pipeline_id is not None and not options.beyond_pipeline():
        run = await client.run_pipeline(
            pipeline_id,
            PipelineRunRequest(
                source_library_file_id=library_file_id, copies=options.quantity or 1
            ),
        )
        store.record_send(meta.id, pipeline_run_id=run.id, print_route="pipeline")
        if run.sliced_library_file_id is not None:
            await uploads.record_sliced(
                meta.id,
                library_file_id,
                SlicedCopy(id=run.sliced_library_file_id, preset_key=str(pipeline_id)),
            )
        return SendResult(
            mode="queue",
            library_file_id=library_file_id,
            filename=filename,
            pipeline_run_id=run.id,
            bambuddy_url=client.config.web_url(QUEUE_PATH),
            options=options,
            edit_url=await attach_edit_link(client, library_file_id, meta, settings),
        )

    printer_id: int | None
    target_model: str | None = None
    if pipeline_id is not None:
        if pipeline is None:  # the scope needed no pipeline, but the slice does
            pipeline = await client.pipeline(pipeline_id)
        slice_request = pipeline_slice_request(pipeline, meta)
        # The pipeline's own target, never ``settings.printer_id``: a ``printer_class``
        # pipeline resolves no printer id at all, and taking the configured one there
        # pinned every copy to that single printer and silently ended the fan-out the
        # pipeline exists for. Bambuddy picks among the class from ``target_model``.
        printer_id = pipeline.target_printer_id
        target_model = pipeline.target_model_class if printer_id is None else None
        if printer_id is None and target_model is None:
            raise not_configured(
                f"slicer pipeline {pipeline.id} targets neither a printer nor a printer "
                "model, so there is nothing to queue the print options to"
            )
    else:
        printer_id = settings.printer_id
        if printer_id is None:
            raise not_configured(
                "no slicer pipeline and no printer are configured, so there is nothing to queue to"
            )
        slice_request = _settings_slice_request(settings, meta)

    accepted = await client.slice(library_file_id, slice_request)
    job = await client.await_slice(accepted.job_id)
    failure = job.failure
    if failure is not None:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, f"Bambuddy failed to slice the file: {failure}")
    sliced = job.result.library_file_id if job.result else None
    if sliced is None:
        raise ApiError(
            status.HTTP_502_BAD_GATEWAY,
            f"Bambuddy slice job {accepted.job_id} completed without a sliced file",
        )

    await uploads.record_sliced(
        meta.id, library_file_id, SlicedCopy(id=sliced, preset_key=slice_request.preset_key)
    )
    item = await client.enqueue(
        QueueItemCreate(
            printer_id=printer_id,
            target_model=target_model,
            library_file_id=sliced,
            plate_id=DEFAULT_PLATE,
            # Only the options that were actually set; the rest stay Bambuddy's.
            **options.queue_fields(),
        )
    )
    store.record_send(
        meta.id,
        queue_item_id=item.id,
        print_route="slice_queue",
        slice_job_id=accepted.job_id,
    )
    # Slicing leaves a second library entry, and the queue references that one — so
    # it is what a reader opens from the queue. Both are this output, so both get the
    # link; the note is best-effort either way.
    # Two independent best-effort notes; nothing waits on the first to send the second.
    noted, noted_sliced = await asyncio.gather(
        attach_edit_link(client, library_file_id, meta, settings),
        attach_edit_link(client, sliced, meta, settings),
    )
    return SendResult(
        mode="queue",
        library_file_id=library_file_id,
        filename=filename,
        queue_item_id=item.id,
        bambuddy_url=client.config.web_url(QUEUE_PATH),
        options=options,
        edit_url=noted or noted_sliced,
    )


async def send_output(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    request: SendRequest,
) -> SendResult:
    library_file_id, filename = await _ensure_copy(
        client, store, uploads, meta, settings, target=None, folder_id=None
    )

    if request.mode == "library":
        return SendResult(
            mode="library",
            library_file_id=library_file_id,
            filename=filename,
            bambuddy_url=client.config.web_url(LIBRARY_PATH),
            edit_url=await attach_edit_link(client, library_file_id, meta, settings),
        )

    return await _queue_send(
        client, store, uploads, meta, settings, request, library_file_id, filename
    )


async def register_sidebar(client: BambuddyClient, settings: StoredSettings) -> SidebarLink:
    """Upsert the ``ScadBuddy`` External Link, idempotent by name (legacy names adopted)."""
    if not settings.public_url:
        raise not_configured(
            "no public ScadBuddy URL is configured, so Bambuddy would have nothing to link to"
        )
    url = settings.public_url.rstrip("/")
    links = await client.external_links()
    existing = next((link for link in links if link.name == SIDEBAR_NAME), None) or next(
        (
            link
            for link in links
            if link.name in LEGACY_SIDEBAR_NAMES and link.url.rstrip("/") == url
        ),
        None,
    )
    link: ExternalLink
    if existing is None:
        link = await client.create_external_link(
            name=SIDEBAR_NAME, url=url, icon=SIDEBAR_ICON, open_in_new_tab=False
        )
        created = True
    else:
        link = await client.update_external_link(
            existing.id, name=SIDEBAR_NAME, url=url, icon=SIDEBAR_ICON, open_in_new_tab=False
        )
        created = False
    return SidebarLink(
        id=link.id,
        name=link.name,
        url=link.url,
        icon=link.icon,
        open_in_new_tab=link.open_in_new_tab,
        created=created,
        embed_path=f"/external/{link.id}",
    )
