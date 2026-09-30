"""A downloaded 3MF, laid out for the default printer and named on its presets (#769).

The stored file names ``ScadBuddy`` placeholders for every preset, which the slicer CLI
ignores and Bambu Studio's GUI does not: it opens the file on custom presets built from
generic defaults. So when Settings names a default printer, the download is re-plated
for it and names the presets the print dialog would slice with, from the same resolver
(``resolver.resolve``) and the model's choices when they were remembered for that
printer; otherwise the size mounted on its right-hand (main) extruder and the default
tier. When that side reports no size, the presets stay placeholders: the left nozzle's
size is never taken for it.

A printer the resolver has no presets for, or a refusal (no spool for a color, no
preset for the size), still gets the re-plated file, on the placeholders. Without a
default printer, with Bambuddy unreachable, or when the file does not fit the printer,
the stored file is served unchanged: a download never fails for want of either.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import get_args
from urllib.parse import quote

from fastapi import Response
from fastapi.responses import FileResponse

from scadbuddy.bambuddy.catalogue import _catalogue
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.extruders import RIGHT, fitted_size
from scadbuddy.bambuddy.filaments import FilamentPlan, SlotNeed, build_options, normalise_colour
from scadbuddy.bambuddy.models import NozzleChoice, NozzleSize
from scadbuddy.bambuddy.resolver import PRINTER_MODEL, PrintChoices, resolve
from scadbuddy.bambuddy.send import target_for
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, download_filename
from scadbuddy.library.settings_store import ModelPrintChoices, StoredSettings
from scadbuddy.render.bambu3mf import PROJECT_SETTINGS_NAME, ZIP_TIMESTAMP, replate_3mf
from scadbuddy.render.plate import PlateFitError, PlateGeometry, plate_for

logger = logging.getLogger(__name__)

THREE_MF_MEDIA_TYPE = "model/3mf"

_SIZES: tuple[str, ...] = get_args(NozzleSize)


@dataclass(frozen=True)
class ProjectPresets:
    """Bambu's own system presets, in ``project_settings.config``'s keys: one filament
    preset per filament, and one nozzle diameter per extruder."""

    printer_settings_id: str
    print_settings_id: str
    filament_settings_id: list[str]
    nozzle_diameter: list[str]
    printer_model: str


def with_presets(payload: bytes, presets: ProjectPresets) -> bytes:
    """``payload`` with ``presets`` in place of the placeholders it was written with.

    Only ``project_settings.config`` changes; every other key stays, so the five the
    slicer dereferences (``render/bambu3mf.py``, above ``PRESET_PLACEHOLDER``) are all
    still there.
    """
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in entries:
            if name == PROJECT_SETTINGS_NAME:
                settings = {**json.loads(data), **asdict(presets)}
                data = (json.dumps(settings, indent=4) + "\n").encode("utf-8")
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            # The writer's policy: covers stored, everything else deflated.
            info.compress_type = (
                zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
            )
            out.writestr(info, data)
    return buffer.getvalue()


async def download_3mf(path: Path, meta: OutputMeta, settings: StoredSettings) -> Response:
    """``path``'s 3MF as an attachment, for the default printer when there is one.

    The stored file itself, as a :class:`FileResponse` (so Range and conditional GET
    still work), whenever nothing about it changes."""
    filename = download_filename(meta)
    payload = await for_default_printer(path, meta, settings)
    if payload is None:
        return FileResponse(path, media_type=THREE_MF_MEDIA_TYPE, filename=filename)
    quoted = quote(filename)
    # The header FileResponse writes for the same filename.
    disposition = (
        f'attachment; filename="{filename}"'
        if quoted == filename
        else f"attachment; filename*=utf-8''{quoted}"
    )
    return Response(
        payload, media_type=THREE_MF_MEDIA_TYPE, headers={"Content-Disposition": disposition}
    )


async def for_default_printer(
    path: Path, meta: OutputMeta, settings: StoredSettings
) -> bytes | None:
    """``path``'s 3MF re-plated for the default printer and, when they resolve, naming
    its presets; ``None`` when the stored file is served as it is.

    The plate is fitted whenever the printer is known; only the preset names fall back
    to the placeholders when the resolver cannot name them.
    """
    if settings.printer_id is None:
        return None
    try:
        async with client_for(settings) as client:
            # The plate the send path fits a file to, for the same printer.
            target = await target_for(client, settings, printer_id=settings.printer_id)
            try:
                presets = await _presets(client, meta, settings, settings.printer_id)
            except (ApiError, ValueError) as error:
                logger.info(
                    "download keeps the preset placeholders",
                    extra={"output_id": meta.id, "reason": str(error)},
                )
                presets = None
    except (ApiError, ValueError) as error:
        logger.info(
            "download served as stored: the default printer is unreadable",
            extra={"output_id": meta.id, "reason": str(error)},
        )
        return None
    try:
        return await asyncio.to_thread(_rewrite, path, target.plate, presets)
    except (PlateFitError, ValueError) as error:
        logger.info(
            "download served as stored: it does not fit the default printer",
            extra={"output_id": meta.id, "reason": str(error)},
        )
        return None


def _rewrite(path: Path, plate: PlateGeometry, presets: ProjectPresets | None) -> bytes:
    replated = replate_3mf(path.read_bytes(), plate)
    return with_presets(replated, presets) if presets is not None else replated


async def _presets(
    client: BambuddyClient, meta: OutputMeta, settings: StoredSettings, printer_id: int
) -> ProjectPresets | None:
    printer = await client.printer(printer_id)
    model = plate_for(printer.model).model
    # The resolver names H2C presets only; another printer keeps the placeholders.
    if model != f"Bambu Lab {PRINTER_MODEL}":
        return None
    remembered = settings.model_print_choices.get(meta.slug)
    # Choices remembered for another printer say nothing about this one's nozzles.
    if remembered is None or remembered.printer_id != printer_id:
        remembered = ModelPrintChoices()
    nozzles = remembered.nozzles or await _mounted(client, printer_id)
    if not nozzles:
        return None
    # As the dialog seeds itself: a remembered process only with remembered nozzles.
    process = remembered.process_name if remembered.nozzles else None
    choices = PrintChoices(
        nozzles=nozzles,
        tier=None if process else (remembered.tier or "standard"),
        process_name=process,
    )

    catalogue = await _catalogue(client)
    # No library file: the slots are the output's own filaments, in order, as a plate
    # ScadBuddy wrote declares them.
    options = build_options(
        library_file_id=0,
        spools=await client.spools(),
        assignments=await client.spool_assignments(),
        requirements=[
            SlotNeed(slot_id=index, colour=normalise_colour(colour))
            for index, colour in enumerate(meta.colors, start=1)
        ],
        printer=printer,
        slot_materials=(await client.inventory_remain(printer_id)).slot_materials,
    )
    # The remembered spools where they are still in the inventory, the auto-match for
    # the rest, as the picker opens.
    offered = {option.spool_id for option in options.spools}
    slot_ids = {slot.slot_id for slot in options.slots}
    chosen = {choice.slot_id: choice for choice in options.suggested}
    chosen.update(
        (choice.slot_id, choice)
        for choice in remembered.filament_plan
        if choice.spool_id in offered and choice.slot_id in slot_ids
    )
    plan = FilamentPlan(slots=[chosen[slot_id] for slot_id in sorted(chosen)])
    spool_presets = {
        spool_id: await client.spool_filament_presets(spool_id)
        for spool_id in sorted({slot.spool_id for slot in plan.slots})
    }
    resolved = resolve(options, plan, choices, catalogue, spool_presets)
    if resolved.errors or resolved.printer_preset is None or resolved.process_preset is None:
        logger.info(
            "the resolver refused the download's presets",
            extra={"output_id": meta.id, "errors": [error.message for error in resolved.errors]},
        )
        return None
    names = catalogue.names()
    size = choices.nozzles[0].size
    return ProjectPresets(
        printer_settings_id=names[(resolved.printer_preset.source, resolved.printer_preset.id)],
        print_settings_id=names[(resolved.process_preset.source, resolved.process_preset.id)],
        filament_settings_id=[names[(ref.source, ref.id)] for ref in resolved.filament_presets],
        # Bambu's printer preset states its size on every extruder.
        nozzle_diameter=[size] * (printer.nozzle_count or 1),
        printer_model=model,
    )


async def _mounted(client: BambuddyClient, printer_id: int) -> list[NozzleChoice]:
    """The main (right-hand) extruder's nozzle, which the dialog's size prints on."""
    # That side only: the left one's size is never the right one's, so a right-hand
    # nozzle that reports no size is unknown.
    size = fitted_size(await client.printer_status(printer_id), RIGHT)
    return [NozzleChoice.model_validate({"size": size})] if size in _SIZES else []
