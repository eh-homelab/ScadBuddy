"""The two "send to Bambuddy" flows and the sidebar registration.

Kept out of the route module so they can be tested against respx recordings without
a FastAPI app, and so the route stays a thin adapter.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal, NamedTuple

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
from scadbuddy.render.recolour import recolour_3mf
from scadbuddy.render.split import normalise_colour

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


#: Marks a :attr:`Target.key` whose file was recolored for chosen spools (#476).
_RECOLORED = "~"


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
            key = f"{key}{_RECOLORED}{','.join(self.colours)}"
        return key

    @property
    def uncoloured_key(self) -> str:
        """:attr:`key` in the model's own colours: the plate and nozzle alone.

        What the project file filed on Generate is recorded under (#317). A print into
        that project reuses it when its spools are the model's own colours; spools in
        other colours get a copy in theirs (#476).
        """
        return Target(self.plate, self.nozzle_diameter).key


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
) -> Target:
    """The plate and nozzle the 3MF is laid out for.

    With an explicit ``printer_id`` and ``nozzle_diameter`` — the spool-first run, which
    has chosen both (spec 2026-09-27 §4) — no pipeline is read: the model comes from the
    printer list and the nozzle is the one chosen. Otherwise, as the send bar and the
    eligibility check need, from the pipeline in play. ``colours`` are the chosen spools'
    (#476), and only the spool-first run has any.
    """
    if printer_id is not None and nozzle_diameter is not None:
        printer = next((row for row in await client.printers() if row.id == printer_id), None)
        model = printer.model if printer is not None else None
        return Target(
            _plate_for_model(model),
            nozzle_diameter,
            tuple(colours) if colours is not None else None,
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
    return recolour_3mf(payload, target.colours) if target.colours is not None else payload


def is_inbox(folder_id: int | None, settings: StoredSettings) -> bool:
    """Whether ``folder_id`` is the inbox: the folder from Settings, where a send with no
    project lands (``None``, the library root, when Settings names none).

    The only folder ScadBuddy ever deletes from (#316). Every other folder a copy is in
    is a project's, and the file there is the user's record of what that project
    printed.
    """
    return folder_id == settings.library_folder_id


class EnsuredCopy(NamedTuple):
    """A copy :func:`ensure_copy` found or made."""

    library_file_id: int
    filename: str
    #: Uploaded by this call, rather than a recorded copy reused.
    created: bool


async def project_filename(
    client: BambuddyClient,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    folder_id: int,
    stem: str,
    target: Target,
) -> str:
    """``<stem>.3mf``, made unique among the files already in ``folder_id`` (#317).

    When this output already has a copy in the folder — laid out for another printer
    model, or it would have been reused — the new one is named after its model
    (``Name sign — Reagan (H2D).3mf``) so the two are told apart. Anything else that
    collides is numbered from 2.

    A listing that fails (a timeout, a 5xx, a body that is not a list) leaves the name
    unchecked rather than failing the print: naming is never worth the print.
    """
    try:
        taken = {row.filename.casefold() for row in await client.library_files(folder_id)}
    except (ApiError, ValueError) as error:
        logger.warning(
            "could not list the project folder; naming the copy without checking it",
            extra={"folder_id": folder_id, "error": str(error)},
        )
        return f"{stem}.3mf"
    candidates = [f"{stem}.3mf"]
    ours = any(copy.folder_id == folder_id for copy in await uploads.for_output(meta.id))
    if ours and target.plate.model:
        # The profile names the vendor too ("Bambu Lab H2D"); the model is what differs.
        candidates.append(f"{stem} ({target.plate.model.removeprefix('Bambu Lab ')}).3mf")
    for candidate in candidates:
        if candidate.casefold() not in taken:
            return candidate
    number = 2
    while f"{stem} ({number}).3mf".casefold() in taken:
        number += 1
    return f"{stem} ({number}).3mf"


async def upload_output(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    target: Target | None = None,
    folder_id: int | None = None,
    stem: str | None = None,
) -> tuple[int, str]:
    """Upload a new copy of ``model.3mf`` into ``folder_id`` (the inbox when ``None``);
    returns its library file id and file name.

    In a project's folder, ``stem`` names the file (:func:`project_filename`, #317): the
    template and the params that differ from its defaults, made unique in the folder.
    The inbox keeps :func:`download_filename`.

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

    filename = (
        await project_filename(client, uploads, meta, folder, stem, target)
        if stem is not None and folder is not None and not is_inbox(folder, settings)
        else download_filename(meta)
    )
    uploaded = await client.upload_library_file(filename, payload, folder_id=folder)
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


async def ensure_copy(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    target: Target | None,
    folder_id: int | None,
    stem: str | None = None,
) -> EnsuredCopy:
    """:func:`ensure_uploaded`, plus the file name Bambuddy holds the copy under and
    whether this call uploaded it.

    Outside the inbox a copy laid out for the same plate and nozzle in the model's own
    colours also serves (:attr:`Target.uncoloured_key`) when the chosen spools are the
    model's colours too: that is the project file Generate filed (#317), and a print
    into the project uses it rather than putting a second file beside it. Spools in
    other colours get a copy in theirs, as in the inbox (#476). An exact match is still
    preferred.

    Finding and uploading hold one lock per folder (per output and folder in the inbox),
    so Generate's filing and a print started while it runs cannot both upload the same
    copy, and two outputs filed into one project cannot both take the same free name.
    """
    target = target if target is not None else await target_for(client, settings, meta.slug)
    folder = folder_id if folder_id is not None else settings.library_folder_id
    async with _copy_lock(meta.id, folder, settings):
        for copy in await _reusable(uploads, meta, settings, target, folder):
            filename = await _still_there(client, uploads, meta, copy)
            if filename is not None:
                return EnsuredCopy(copy.id, filename, created=False)
        library_file_id, filename = await upload_output(
            client, store, uploads, meta, settings, target=target, folder_id=folder_id, stem=stem
        )
    return EnsuredCopy(library_file_id, filename, created=True)


#: One per folder while :func:`ensure_copy` holds it; weak, so an idle folder's lock is
#: dropped rather than kept for the life of the process.
_COPY_LOCKS: weakref.WeakValueDictionary[tuple[str | None, int | None], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _copy_lock(output_id: str, folder: int | None, settings: StoredSettings) -> asyncio.Lock:
    """A project folder's lock is shared by every output: :func:`project_filename` lists
    the folder and the upload takes a name from that listing, so two outputs must not
    interleave there. The inbox keeps :func:`download_filename`, which is not made
    unique, so there only one output's copies are serialized."""
    key = (output_id if is_inbox(folder, settings) else None, folder)
    lock = _COPY_LOCKS.get(key)
    if lock is None:
        lock = _COPY_LOCKS[key] = asyncio.Lock()
    return lock


async def _reusable(
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    target: Target,
    folder: int | None,
) -> list[LibraryCopy]:
    """The recorded copies in ``folder`` that serve ``target``, best first.

    Outside the inbox, when ``target`` is in the model's own colours, that also takes
    the uncoloured copy and then one recoloured for spools that were the model's own
    colours (a print whose spools matched the model), so a later filing with no spools
    reuses it rather than uploading a duplicate.
    """
    keys = [target.key]
    own = [normalise_colour(colour) for colour in meta.colors]
    own_colours = (
        target.colours is None or [normalise_colour(colour) for colour in target.colours] == own
    )
    recoloured = f"{target.uncoloured_key}{_RECOLORED}"
    extended = not is_inbox(folder, settings) and own_colours
    if extended and target.uncoloured_key != target.key:
        keys.append(target.uncoloured_key)

    def rank(copy: LibraryCopy) -> int | None:
        if copy.target_key in keys:
            return keys.index(copy.target_key)
        if extended and copy.target_key.startswith(recoloured):
            suffix = copy.target_key.removeprefix(recoloured).split(",")
            if [normalise_colour(colour) for colour in suffix] == own:
                return len(keys)
        return None

    ranked = [
        (order, copy)
        for copy in await uploads.for_output(meta.id)
        if copy.folder_id == folder and (order := rank(copy)) is not None
    ]
    return [copy for _, copy in sorted(ranked, key=lambda pair: pair[0])]


async def _still_there(
    client: BambuddyClient, uploads: BambuddyUploadStore, meta: OutputMeta, copy: LibraryCopy
) -> str | None:
    """The copy's file name, or ``None`` once it is found deleted in Bambuddy.

    Someone may have deleted it there since. Reusing a dead id would fail the slice or
    the slot read with an upstream 404, so it is read first, and a 404 is forgotten.
    """
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
        return None
    return found.filename


@dataclass(frozen=True)
class ReadableCopy:
    """A library file holding the output's 3MF, for reading its slots (#457)."""

    id: int
    #: Recolored for a run's spools (#476), so its filament colors are the spools', not
    #: the model's.
    recolored: bool


async def copy_to_read(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
) -> ReadableCopy:
    """Any recorded copy Bambuddy still has, else a new upload (#457).

    The filament step only reads the plate's slots out of the file, and they don't
    depend on the plate, nozzle or folder a copy was laid out for. So it reuses a run's
    copy rather than uploading one of its own, which the next run's inbox upload would
    supersede and the next open would upload again. Inbox copies come first; a copy in
    a project's folder is only read, never moved.
    """
    recorded = sorted(
        await uploads.for_output(meta.id), key=lambda copy: not is_inbox(copy.folder_id, settings)
    )
    for copy in recorded:
        if await _still_there(client, uploads, meta, copy) is not None:
            return ReadableCopy(copy.id, recolored=_RECOLORED in copy.target_key)
    library_file_id = await ensure_uploaded(client, store, uploads, meta, settings)
    return ReadableCopy(library_file_id, recolored=False)


async def ensure_uploaded(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    target: Target | None = None,
    folder_id: int | None = None,
    stem: str | None = None,
) -> int:
    """The library file id to slice, judge or print, uploading the 3MF where needed.

    ``folder_id`` is the folder the copy has to be in — a project's (#79) — and
    ``None`` means the inbox, the folder from Settings.

    An output is immutable once generated — changing a parameter produces a new one —
    so a recorded copy still describes this exact 3MF. What is *not* immutable is where
    it sits and what it was laid out for, so a copy is reused only where both still
    hold: the same folder, and the same :attr:`Target.key` (the plate, #105, and the
    nozzle, #126). A run with the same choices reuses the copy; the print dialog's
    slot read takes any copy at all (:func:`copy_to_read`, #457).

    Anything else uploads a **new copy** there (#316). Never a move: a file sent to
    project A is A's record of what it printed, and A's slices and archives stay in A,
    so moving the file to project B would leave A pointing at nothing. And never a
    delete outside the inbox; see :func:`upload_output`.
    """
    ensured = await ensure_copy(
        client, store, uploads, meta, settings, target=target, folder_id=folder_id, stem=stem
    )
    return ensured.library_file_id


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
    library_file_id, filename, _ = await ensure_copy(
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
