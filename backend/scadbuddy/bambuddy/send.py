"""The send bar's library upload, the upload the print run shares, and the sidebar link.

Kept out of the route module so they can be tested against respx recordings without
a FastAPI app, and so the route stays a thin adapter. The send bar only uploads
(#312); slicing and queueing is the print dialog's run, in
``scadbuddy.bambuddy.pipelines``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import status
from pydantic import BaseModel

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import NOT_FOUND_PROBLEM, PLATE_FIT_PROBLEM, not_configured
from scadbuddy.bambuddy.models import ExternalLink
from scadbuddy.bambuddy.options import PrintOptions, resolve
from scadbuddy.core.problems import ApiError
from scadbuddy.library.deeplink import edit_url, merge_edit_note
from scadbuddy.library.outputs import MODEL_NAME, OutputMeta, OutputStore, download_filename
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.bambu3mf import replate_3mf
from scadbuddy.render.plate import DEFAULT_PLATE as FALLBACK_PLATE
from scadbuddy.render.plate import PlateFitError, PlateGeometry, plate_for

logger = logging.getLogger(__name__)

SIDEBAR_NAME = "ScadBuddy"
# Earlier builds registered the link as "Customize"; adopt and rename it rather than
# leaving a second entry in Bambuddy's sidebar.
LEGACY_SIDEBAR_NAMES = frozenset({"Customize"})
SIDEBAR_ICON = "shapes"

LIBRARY_PATH = "/library"


class SendRequest(BaseModel):
    """The send bar's body. It only uploads to the library (#312).

    ``mode`` stays so that a client still asking for the removed ``"queue"`` mode is
    refused with a 422 rather than silently getting an upload it did not ask for. Any
    other field an older client sends (``copies``, ``options``) is ignored.
    """

    mode: Literal["library"] = "library"


class SendResult(BaseModel):
    library_file_id: int
    filename: str
    #: Bambuddy's library page, where the upload landed.
    bambuddy_url: str | None = None
    #: The "Edit in ScadBuddy" link attached to the library file, when one is known.
    edit_url: str | None = None


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
    #: The nozzle the print run chose (#126). ``None`` — the send bar, which chooses
    #: none — keeps the placeholder.
    nozzle_diameter: str | None = None

    @property
    def key(self) -> str:
        """Recorded as ``library_file_plate``: a reused upload has to match both halves.

        Without a nozzle this is the plate's own key, so a file recorded before #126 is
        still reused for the same plate.
        """
        if self.nozzle_diameter is None:
            return self.plate.key
        return f"{self.plate.key}@{self.nozzle_diameter}"


async def target_for(
    client: BambuddyClient,
    settings: StoredSettings,
    *,
    printer_id: int | None = None,
    nozzle_diameter: str | None = None,
) -> Target:
    """The plate and nozzle the 3MF is laid out for.

    The plate is ``printer_id``'s, else the printer set in Settings, else the fallback
    plate; a printer Bambuddy reports without a model falls back the same way. The
    nozzle is stated only when the caller chose one: the print run does (spec
    2026-09-27 §4), the send bar does not.
    """
    printer_id = printer_id if printer_id is not None else settings.printer_id
    if printer_id is None:
        # Nothing to resolve against, so do not spend a round trip finding out.
        return Target(_plate_for_model(None), nozzle_diameter)
    printer = next((row for row in await client.printers() if row.id == printer_id), None)
    model = printer.model if printer is not None else None
    return Target(_plate_for_model(model), nozzle_diameter)


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
        return replate_3mf(payload, target.plate, nozzle_diameter=target.nozzle_diameter)
    except PlateFitError as error:
        raise ApiError(status.HTTP_409_CONFLICT, str(error), type_=PLATE_FIT_PROBLEM) from error


async def upload_output(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    target: Target | None = None,
    folder_id: int | None = None,
) -> tuple[OutputMeta, str]:
    """Upload ``model.3mf``, replacing a file a previous send left behind.

    ``folder_id`` overrides the folder from Settings, which is how a send to a project
    lands in *that project's* folder (#79) — a folder carries ``project_id``, so putting
    the file there is what makes Bambuddy's project page list it.

    Bambuddy keeps both copies if you simply upload again, so a re-send deletes the
    recorded id first. A delete that 404s is not fatal — someone removing the file in
    Bambuddy must not wedge the button.

    The order matters: the plate fit is decided *before* anything is deleted, so a
    model that cannot be laid out refuses with the previous send still intact rather
    than taking the old file with it. The recorded id is cleared only once the delete
    has actually come back — committed or 404 — so a failure between delete and upload
    cannot leave ``library_file_id`` pointing at a file that is gone, and a delete that
    *fails* leaves the id in place to be retried rather than orphaning the file.
    """
    target = target if target is not None else await target_for(client, settings)
    payload = _laid_out_for(_read_3mf(store, meta), target)
    filename = download_filename(meta)

    if meta.library_file_id is not None:
        library_file_id = meta.library_file_id
        try:
            await client.delete_library_file(library_file_id)
        except ApiError as error:
            if error.status != status.HTTP_404_NOT_FOUND:
                # The file is still there and still ours. Leaving the recorded id
                # alone is what lets the next send delete it; clearing it first
                # would strand the file in Bambuddy with nothing pointing at it,
                # and every retry would add another copy.
                raise
            logger.info(
                "the previously sent library file was already gone",
                extra={"library_file_id": library_file_id},
            )
        meta = store.forget_library_file(meta.id)

    uploaded = await client.upload_library_file(
        filename,
        payload,
        folder_id=folder_id if folder_id is not None else settings.library_folder_id,
    )
    recorded = store.record_send(
        meta.id, library_file_id=uploaded.id, library_file_plate=target.key
    )
    return recorded, uploaded.filename


async def ensure_uploaded(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    target: Target | None = None,
    folder_id: int | None = None,
) -> tuple[OutputMeta, int]:
    """The library file id to slice, judge or print, uploading the 3MF if there is none.

    An output is immutable once generated — changing a parameter produces a new one — so
    a recorded id still describes this exact 3MF and is reused rather than re-uploaded.
    The print dialog leans on that: its filament step reads the plate's slots out of a
    library file, and must not re-upload on every open.

    The *placement* is not immutable, though: it is chosen from the printer this send
    is aimed at (#105), and the printer can change between sends. So the id is only
    reused while it was laid out for the plate now in play — and states the nozzle now
    in play (#126); otherwise this re-uploads,
    or the second send would hand Bambuddy a file centred on the previous printer's bed
    with the prime tower somewhere the new one's extruders cannot reach.
    """
    target = target if target is not None else await target_for(client, settings)
    if meta.library_file_id is not None and meta.library_file_plate == target.key:
        if folder_id is not None:
            # The file was uploaded before this project was chosen, so it is sitting in
            # whatever folder that send used. Bambuddy has a move route, and a caller
            # that reports `folder_id` must not report one the file is not in.
            await client.move_library_files([meta.library_file_id], folder_id)
        return meta, meta.library_file_id
    meta, _ = await upload_output(client, store, meta, settings, target=target, folder_id=folder_id)
    if meta.library_file_id is None:  # pragma: no cover - upload_output always records one
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "the upload did not return a library file id")
    return meta, meta.library_file_id


async def attach_edit_link(
    client: BambuddyClient,
    library_file_id: int,
    meta: OutputMeta,
    settings: StoredSettings,
) -> str | None:
    """Best-effort: note the "Edit in ScadBuddy" link on the uploaded library file.

    Deliberately the last thing a send does, and deliberately swallowing every
    ApiError. The note is cosmetic — the file is already uploaded, and on the print
    run already queued — so letting a timeout or a rejected note abort the send would
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


def request_scope(copies: int | None, options: PrintOptions) -> PrintOptions:
    """The per-request overlay. ``copies`` is each dialog's own control for the same
    quantity, and wins over an ``options.quantity`` sent alongside it.

    The print run's per-request overlay (#78).
    """
    if copies is None:
        return options
    return options.model_copy(update={"quantity": copies})


def resolve_print_options(
    settings: StoredSettings, slug: str, printer_id: int | None, request_scope: PrintOptions
) -> PrintOptions:
    """global → per-printer → per-model → per-request, least specific first.

    The print run's merge (#124). The send bar no longer queues (#312), so it resolves
    none.
    """
    return resolve(
        settings.print_options,
        settings.printer_print_options.get(str(printer_id)) if printer_id is not None else None,
        settings.model_print_options.get(slug),
        request_scope,
    )


async def send_output(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
) -> SendResult:
    """Upload the 3MF to the library and note the edit link on it. Nothing is queued."""
    meta, filename = await upload_output(client, store, meta, settings)
    if meta.library_file_id is None:  # pragma: no cover - upload_output always records one
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "the upload did not return a library file id")
    library_file_id = meta.library_file_id
    return SendResult(
        library_file_id=library_file_id,
        filename=filename,
        bambuddy_url=client.config.web_url(LIBRARY_PATH),
        edit_url=await attach_edit_link(client, library_file_id, meta, settings),
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
