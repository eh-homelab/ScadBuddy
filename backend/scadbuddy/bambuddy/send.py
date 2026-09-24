"""The two "send to Bambuddy" flows and the sidebar registration.

Kept out of the route module so they can be tested against respx recordings without
a FastAPI app, and so the route stays a thin adapter.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import NOT_FOUND_PROBLEM, not_configured
from scadbuddy.bambuddy.models import (
    ExternalLink,
    Pipeline,
    PipelineRunRequest,
    PresetRef,
    QueueItemCreate,
    SliceRequest,
)
from scadbuddy.bambuddy.options import PrintOptions, resolve
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import MODEL_NAME, OutputMeta, OutputStore, download_filename
from scadbuddy.library.settings_store import StoredSettings

logger = logging.getLogger(__name__)

SendMode = Literal["library", "queue"]

SIDEBAR_NAME = "ScadBuddy"
# Earlier builds registered the link as "Customize"; adopt and rename it rather than
# leaving a second entry in Bambuddy's sidebar.
LEGACY_SIDEBAR_NAMES = frozenset({"Customize"})
SIDEBAR_ICON = "shapes"

QUEUE_PATH = "/queue"
LIBRARY_PATH = "/library"

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


async def upload_output(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    folder_id: int | None = None,
) -> tuple[OutputMeta, str]:
    """Upload ``model.3mf``, replacing a file a previous send left behind.

    ``folder_id`` overrides the folder from Settings, which is how a send to a project
    lands in *that project's* folder (#79) — a folder carries ``project_id``, so putting
    the file there is what makes Bambuddy's project page list it.

    Bambuddy keeps both copies if you simply upload again, so a re-send deletes the
    recorded id first. A delete that 404s is not fatal — someone removing the file in
    Bambuddy must not wedge the button.
    """
    if meta.library_file_id is not None:
        try:
            await client.delete_library_file(meta.library_file_id)
        except ApiError as error:
            if error.status != status.HTTP_404_NOT_FOUND:
                raise
            logger.info(
                "the previously sent library file was already gone",
                extra={"library_file_id": meta.library_file_id},
            )

    filename = download_filename(meta)
    uploaded = await client.upload_library_file(
        filename,
        _read_3mf(store, meta),
        folder_id=folder_id if folder_id is not None else settings.library_folder_id,
    )
    return store.record_send(meta.id, library_file_id=uploaded.id), uploaded.filename


async def ensure_uploaded(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    folder_id: int | None = None,
) -> tuple[OutputMeta, int]:
    """The library file id to slice, judge or print, uploading the 3MF if there is none.

    An output is immutable once generated — changing a parameter produces a new one — so
    a recorded id still describes this exact 3MF and is reused rather than re-uploaded.
    The print picker (#86) leans on that: opening it checks eligibility, which needs a
    file in Bambuddy, and must not re-upload on every open.
    """
    if meta.library_file_id is not None:
        if folder_id is not None:
            # The file was uploaded before this project was chosen, so it is sitting in
            # whatever folder that send used. Bambuddy has a move route, and a caller
            # that reports `folder_id` must not report one the file is not in.
            await client.move_library_files([meta.library_file_id], folder_id)
        return meta, meta.library_file_id
    meta, _ = await upload_output(client, store, meta, settings, folder_id=folder_id)
    if meta.library_file_id is None:  # pragma: no cover - upload_output always records one
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "the upload did not return a library file id")
    return meta, meta.library_file_id


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


def _pipeline_slice_request(pipeline: Pipeline, meta: OutputMeta) -> SliceRequest:
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


def _request_scope(request: SendRequest) -> PrintOptions:
    """The per-request overlay. ``copies`` is the send bar's own control for the same
    quantity, and wins over an ``options.quantity`` sent alongside it."""
    if request.copies is None:
        return request.options
    return request.options.model_copy(update={"quantity": request.copies})


def _resolve_options(
    settings: StoredSettings, meta: OutputMeta, request: SendRequest, printer_id: int | None
) -> PrintOptions:
    """global → per-printer → per-model → per-request, least specific first."""
    return resolve(
        settings.print_options,
        settings.printer_print_options.get(str(printer_id)) if printer_id is not None else None,
        settings.model_print_options.get(meta.slug),
        _request_scope(request),
    )


def _needs_pipeline(settings: StoredSettings, meta: OutputMeta, request: SendRequest) -> bool:
    """Whether the configured pipeline has to be read before the path can be chosen.

    Reading it is what tells us the target printer the per-printer scope keys on, and the
    presets to slice with once an option rules the run out.

    With a printer configured the scope key is already known, so the resolution here is
    the real one and the answer is exact. Only when it is *not* — a pipeline aimed at its
    own printer, or at a printer class — can a remembered printer override still turn out
    to be this send's, and then any entry in the map is reason enough to look. Deciding
    that on "the map is non-empty" unconditionally would make one saved override cost
    every later send an extra GET it does not need, and turn that GET's failure into a
    send failure.
    """
    if settings.printer_id is None and settings.printer_print_options:
        return True
    return bool(_resolve_options(settings, meta, request, settings.printer_id).beyond_pipeline())


async def _queue_send(
    client: BambuddyClient,
    store: OutputStore,
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
    # The model's own default pipeline wins over the global one (#86).
    pipeline_id = settings.pipeline_for(meta.slug)
    pipeline: Pipeline | None = None
    if pipeline_id is not None and _needs_pipeline(settings, meta, request):
        pipeline = await client.pipeline(pipeline_id)

    # The printer the *option scopes* key on — the printer ScadBuddy believes it prints to.
    # Not the same thing as the queue item's target, which a pipeline owns outright; see
    # below, where conflating the two pinned a printer-class pipeline to one printer.
    scope_printer_id = settings.printer_id
    if scope_printer_id is None and pipeline is not None:
        scope_printer_id = pipeline.target_printer_id
    options = _resolve_options(settings, meta, request, scope_printer_id)

    if pipeline_id is not None and not options.beyond_pipeline():
        run = await client.run_pipeline(
            pipeline_id,
            PipelineRunRequest(
                source_library_file_id=library_file_id, copies=options.quantity or 1
            ),
        )
        store.record_send(meta.id, pipeline_run_id=run.id, print_route="pipeline")
        return SendResult(
            mode="queue",
            library_file_id=library_file_id,
            filename=filename,
            pipeline_run_id=run.id,
            bambuddy_url=client.config.web_url(QUEUE_PATH),
            options=options,
        )

    printer_id: int | None
    target_model: str | None = None
    if pipeline_id is not None:
        if pipeline is None:  # pragma: no cover - _needs_pipeline already fetched it
            pipeline = await client.pipeline(pipeline_id)
        slice_request = _pipeline_slice_request(pipeline, meta)
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
    return SendResult(
        mode="queue",
        library_file_id=library_file_id,
        filename=filename,
        queue_item_id=item.id,
        bambuddy_url=client.config.web_url(QUEUE_PATH),
        options=options,
    )


async def send_output(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
    request: SendRequest,
) -> SendResult:
    meta, filename = await upload_output(client, store, meta, settings)
    if meta.library_file_id is None:  # pragma: no cover - upload_output always records one
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "the upload did not return a library file id")
    library_file_id = meta.library_file_id

    if request.mode == "library":
        return SendResult(
            mode="library",
            library_file_id=library_file_id,
            filename=filename,
            bambuddy_url=client.config.web_url(LIBRARY_PATH),
        )

    return await _queue_send(client, store, meta, settings, request, library_file_id, filename)


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
