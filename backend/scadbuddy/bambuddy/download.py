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
the stored file keeps its plate and placeholders: a download never fails for want of
either.

A template's ``print_settings`` (#770) are written into ``project_settings.config`` on
every path, re-plated or not, and listed as edits to the system process. Only a
template without them, on one of those fallbacks, gets the stored file byte for byte.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import get_args
from urllib.parse import quote

from fastapi import Response
from fastapi.responses import FileResponse

from scadbuddy.bambuddy.catalogue import _catalogue
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.extruders import RIGHT, fitted_size
from scadbuddy.bambuddy.filaments import SlotNeed, build_options, normalise_colour, seed_plan
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


def with_presets(
    payload: bytes, presets: ProjectPresets | None, print_settings: Mapping[str, str] | None = None
) -> bytes:
    """``payload`` with ``presets`` in place of the placeholders it was written with,
    and the template's ``print_settings`` (#770) over the process.

    The print settings are listed in ``different_settings_to_system``, whose first
    entry is the process's edits, then one per filament and one for the printer, as
    Bambu Studio saves a project; so it shows them as changes to the system preset.

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
                settings = json.loads(data)
                if presets is not None:
                    settings.update(asdict(presets))
                if print_settings:
                    settings.update(print_settings)
                    filaments = len(settings.get("filament_settings_id") or [])
                    settings["different_settings_to_system"] = [
                        ";".join(print_settings),
                        *[""] * filaments,
                        "",
                    ]
                data = (json.dumps(settings, indent=4) + "\n").encode("utf-8")
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            # The writer's policy: covers stored, everything else deflated.
            info.compress_type = (
                zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
            )
            out.writestr(info, data)
    return buffer.getvalue()


async def download_3mf(
    path: Path,
    meta: OutputMeta,
    settings: StoredSettings,
    print_settings: Mapping[str, str] | None = None,
) -> Response:
    """``path``'s 3MF as an attachment, for the default printer when there is one, with
    the template's ``print_settings`` (#770) whether or not there is.

    The stored file itself, as a :class:`FileResponse` (so Range and conditional GET
    still work), whenever nothing about it changes."""
    filename = download_filename(meta)
    payload = await for_default_printer(path, meta, settings, print_settings)
    if payload is None and print_settings:
        payload = await asyncio.to_thread(
            lambda: with_presets(path.read_bytes(), None, print_settings)
        )
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
    path: Path,
    meta: OutputMeta,
    settings: StoredSettings,
    print_settings: Mapping[str, str] | None = None,
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
    # A file that does not fit the plate is handled in `_rewrite`; a malformed one
    # raises from there rather than being served as if nothing were wrong.
    return await asyncio.to_thread(_rewrite, path, target.plate, presets, print_settings, meta.id)


def _rewrite(
    path: Path,
    plate: PlateGeometry,
    presets: ProjectPresets | None,
    print_settings: Mapping[str, str] | None,
    output_id: str,
) -> bytes | None:
    """The stored file re-plated, with ``presets`` and ``print_settings``. One that
    does not fit ``plate`` keeps its own plate and still gets ``print_settings``, from
    the same read of the file (#852); with none, ``None``: it is served as stored."""
    stored = path.read_bytes()
    # Only PlateFitError: a plate that does not fit this printer's bed. A malformed
    # stored 3MF (BadZipFile, or a JSONDecodeError from its project_settings.config)
    # raises, so it is never served as a download with placeholders.
    try:
        replated = replate_3mf(stored, plate)
    except PlateFitError as error:
        logger.info(
            "download not re-plated: it does not fit the default printer",
            extra={"output_id": output_id, "reason": str(error)},
        )
        return with_presets(stored, None, print_settings) if print_settings else None
    if presets is None and not print_settings:
        return replated
    return with_presets(replated, presets, print_settings)


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
    # The remembered spools where they still fit, the auto-match for the rest, as the
    # picker opens.
    plan = seed_plan(options, remembered.filament_plan)
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
        # Bambu's printer preset states its size on both of the H2C's extruders, which
        # Bambuddy does not always count (``nozzle_count`` can be missing).
        nozzle_diameter=[size, size],
        printer_model=model,
    )


async def _mounted(client: BambuddyClient, printer_id: int) -> list[NozzleChoice]:
    """The main (right-hand) extruder's nozzle, which the dialog's size prints on."""
    # That side only: the left one's size is never the right one's, so a right-hand
    # nozzle that reports no size is unknown.
    size = fitted_size(await client.printer_status(printer_id), RIGHT)
    return [NozzleChoice.model_validate({"size": size})] if size in _SIZES else []
