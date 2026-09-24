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
from scadbuddy.bambuddy.errors import NOT_FOUND_PROBLEM, PLATE_FIT_PROBLEM, not_configured
from scadbuddy.bambuddy.models import (
    CalibrationMode,
    ExternalLink,
    PipelineRunRequest,
    Printer,
    QueueItemCreate,
    SliceRequest,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import MODEL_NAME, OutputMeta, OutputStore, download_filename
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.bambu3mf import replate_3mf
from scadbuddy.render.plate import DEFAULT_PLATE, PlateFitError, PlateGeometry, plate_for

logger = logging.getLogger(__name__)

SendMode = Literal["library", "queue"]

SIDEBAR_NAME = "Customize"
SIDEBAR_ICON = "shapes"

QUEUE_PATH = "/queue"
LIBRARY_PATH = "/library"

# ScadBuddy's own queue policy, not Bambuddy's defaults — it defaults both of these to
# "auto". They are three-way enums ("off" | "on" | "auto"), so a bool 422s. ScadBuddy
# sends an already-sliced plate for a shape the user just previewed, and asks for
# neither calibration pass so the print starts without a bed-levelling delay.
QUEUE_BED_LEVELLING: CalibrationMode = "off"
QUEUE_FLOW_CALI: CalibrationMode = "off"


class SendRequest(BaseModel):
    mode: SendMode = "library"
    copies: int = Field(default=1, ge=1, le=1000)


class SendResult(BaseModel):
    mode: SendMode
    library_file_id: int
    filename: str
    pipeline_run_id: int | None = None
    queue_item_id: int | None = None
    #: Deep link into Bambuddy for what this send produced.
    bambuddy_url: str | None = None


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


async def target_printer_model(
    client: BambuddyClient, settings: StoredSettings, slug: str
) -> str | None:
    """Which printer model this output is heading for, as Bambuddy names it.

    The pipeline wins over the globally configured printer, the same precedence
    :meth:`StoredSettings.pipeline_for` gives the print itself. A pipeline aimed
    at a printer *class* names the model directly; one aimed at a specific
    printer has to be resolved through the printer list. ``None`` — no pipeline,
    no printer, or a printer Bambuddy reports without a model — is not an error;
    it means the default plate.
    """
    pipeline_id = settings.pipeline_for(slug)
    if pipeline_id is None and settings.printer_id is None:
        # Nothing to resolve against, so do not spend two round trips finding out.
        return None
    printers: list[Printer] | None = None
    if pipeline_id is not None:
        pipeline = next((row for row in await client.pipelines() if row.id == pipeline_id), None)
        if pipeline is not None:
            if pipeline.target_model_class:
                return pipeline.target_model_class
            if pipeline.target_printer_id is not None:
                printers = await client.printers()
                target = next(
                    (row for row in printers if row.id == pipeline.target_printer_id), None
                )
                if target is not None and target.model:
                    return target.model
    if settings.printer_id is not None:
        printers = printers if printers is not None else await client.printers()
        target = next((row for row in printers if row.id == settings.printer_id), None)
        if target is not None and target.model:
            return target.model
    return None


async def target_plate(
    client: BambuddyClient, settings: StoredSettings, slug: str
) -> PlateGeometry:
    model = await target_printer_model(client, settings, slug)
    plate = plate_for(model)
    if model and plate is DEFAULT_PLATE:
        logger.info(
            "no plate geometry for this printer model; using the default plate",
            extra={"printer_model": model},
        )
    return plate


def _laid_out_for(payload: bytes, plate: PlateGeometry) -> bytes:
    """Re-place the 3MF for ``plate``, refusing here rather than at the slicer.

    The 3MF was written at render time, when no printer was chosen, so the plate
    it carries is the fallback. Moving it now is what puts the object — and the
    prime tower a multi-colour print needs — where every extruder can reach them.
    """
    try:
        return replate_3mf(payload, plate)
    except PlateFitError as error:
        raise ApiError(status.HTTP_409_CONFLICT, str(error), type_=PLATE_FIT_PROBLEM) from error


async def upload_output(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
) -> tuple[OutputMeta, str]:
    """Upload ``model.3mf``, replacing a file a previous send left behind.

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
    payload = _laid_out_for(_read_3mf(store, meta), await target_plate(client, settings, meta.slug))
    uploaded = await client.upload_library_file(
        filename, payload, folder_id=settings.library_folder_id
    )
    return store.record_send(meta.id, library_file_id=uploaded.id), uploaded.filename


async def ensure_uploaded(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
) -> tuple[OutputMeta, int]:
    """The library file id to slice, judge or print, uploading the 3MF if there is none.

    An output is immutable once generated — changing a parameter produces a new one — so
    a recorded id still describes this exact 3MF and is reused rather than re-uploaded.
    The print picker (#86) leans on that: opening it checks eligibility, which needs a
    file in Bambuddy, and must not re-upload on every open.
    """
    if meta.library_file_id is not None:
        return meta, meta.library_file_id
    meta, _ = await upload_output(client, store, meta, settings)
    if meta.library_file_id is None:  # pragma: no cover - upload_output always records one
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "the upload did not return a library file id")
    return meta, meta.library_file_id


def _slice_request(settings: StoredSettings, meta: OutputMeta) -> SliceRequest:
    if settings.printer_preset is None or settings.process_preset is None:
        raise not_configured(
            "no slicer pipeline is configured, and the printer and process presets "
            "needed to slice directly are not set either"
        )
    if not settings.filament_presets:
        raise not_configured(
            "no filament presets are configured; set one per AMS slot, in extruder order"
        )
    colours = list(meta.colors)
    if len(colours) > len(settings.filament_presets):
        raise not_configured(
            f"this output has {len(colours)} colours but only "
            f"{len(settings.filament_presets)} filament presets are configured"
        )
    return SliceRequest(
        printer_preset=settings.printer_preset,
        process_preset=settings.process_preset,
        filament_presets=settings.filament_presets,
        filament_colours=colours,
        bed_type=settings.bed_type,
        plate=1,
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

    # The model's own default pipeline wins over the global one (#86); before that
    # issue there was only the global ``pipeline_id``, which is now the fallback.
    pipeline_id = settings.pipeline_for(meta.slug)
    if pipeline_id is not None:
        run = await client.run_pipeline(
            pipeline_id,
            PipelineRunRequest(source_library_file_id=library_file_id, copies=request.copies),
        )
        store.record_send(meta.id, pipeline_run_id=run.id)
        return SendResult(
            mode="queue",
            library_file_id=library_file_id,
            filename=filename,
            pipeline_run_id=run.id,
            bambuddy_url=client.config.web_url(QUEUE_PATH),
        )

    if settings.printer_id is None:
        raise not_configured(
            "no slicer pipeline and no printer are configured, so there is nothing to queue to"
        )

    accepted = await client.slice(library_file_id, _slice_request(settings, meta))
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
            printer_id=settings.printer_id,
            library_file_id=sliced,
            quantity=request.copies,
            plate_id=1,
            bed_levelling=QUEUE_BED_LEVELLING,
            flow_cali=QUEUE_FLOW_CALI,
            layer_inspect=True,
            timelapse=True,
        )
    )
    store.record_send(meta.id, queue_item_id=item.id)
    return SendResult(
        mode="queue",
        library_file_id=library_file_id,
        filename=filename,
        queue_item_id=item.id,
        bambuddy_url=client.config.web_url(QUEUE_PATH),
    )


async def register_sidebar(client: BambuddyClient, settings: StoredSettings) -> SidebarLink:
    """Upsert the ``Customize`` External Link, idempotent by name."""
    if not settings.public_url:
        raise not_configured(
            "no public ScadBuddy URL is configured, so Bambuddy would have nothing to link to"
        )
    url = settings.public_url.rstrip("/")
    existing = next(
        (link for link in await client.external_links() if link.name == SIDEBAR_NAME), None
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
