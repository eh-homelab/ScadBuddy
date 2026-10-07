"""The send bar's library upload, the upload the print run shares, and the sidebar link.

Kept out of the route module so they can be tested against respx recordings without
a FastAPI app, and so the route stays a thin adapter. The send bar only uploads
(#312); slicing and queueing is the print dialog's run, in
``scadbuddy.bambuddy.print_run``.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple

from fastapi import status
from pydantic import BaseModel

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import NOT_FOUND_PROBLEM, PLATE_FIT_PROBLEM, not_configured
from scadbuddy.bambuddy.extruders import VOLUME_TYPE
from scadbuddy.bambuddy.models import ExternalLink
from scadbuddy.bambuddy.options import PrintOptions, resolve
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy
from scadbuddy.core.problems import ApiError
from scadbuddy.library.deeplink import edit_url, merge_edit_note
from scadbuddy.library.outputs import OutputFiles, OutputMeta, download_filename
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.bambu3mf import replate_3mf
from scadbuddy.render.plate import DEFAULT_PLATE as FALLBACK_PLATE
from scadbuddy.render.plate import PlateFitError, PlateGeometry, plate_for
from scadbuddy.render.recolour import recolour_3mf
from scadbuddy.render.split import normalise_colour

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
    #: Uploaded by this send, rather than a copy already in the inbox reused (#931).
    created: bool


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


async def read_3mf(store: OutputFiles, meta: OutputMeta) -> bytes:
    payload = await store.model_3mf(meta.id)
    if payload is None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"output {meta.id!r} has no 3MF to send",
            type_=NOT_FOUND_PROBLEM,
        )
    return payload


#: Marks a :attr:`Target.key` whose file was recolored for chosen spools (#476).
_RECOLORED = "~"
#: The flow the slicer assumes when the file states none (#484). So an all-Standard
#: :attr:`Target.key` is the key of a copy from before #484, which states no flow: the
#: bytes differ under one key, and reusing either is right only while Standard stays
#: the slicer's default.
_STANDARD = VOLUME_TYPE["standard"]


@dataclass(frozen=True)
class Target:
    """What the 3MF is laid out for: the target's plate and, when known, its nozzle."""

    plate: PlateGeometry
    #: The nozzle the print run chose (#126). ``None`` — the send bar, which chooses
    #: none — keeps the placeholder.
    nozzle_diameter: str | None = None
    #: One colour per filament, from the spools the run chose (#476). ``None`` — the
    #: send bar, which chooses no spools — keeps the model's own colours.
    colours: tuple[str, ...] | None = None
    #: Which extruders have the nozzle, in the slicer's order (#834). ``None`` — no run,
    #: or a printer whose status says nothing either way — lets the slicer choose.
    nozzle_stats: tuple[str, ...] | None = None
    #: Each extruder's flow, in the slicer's order (#484). ``None`` — the send bar,
    #: which chooses none — states none, and the slicer slices Standard.
    nozzle_volume_type: tuple[str, ...] | None = None

    @property
    def key(self) -> str:
        """Recorded as a copy's ``target_key``: a reused upload has to match both halves.

        Without a nozzle this is the plate's own key, so a file recorded before #126 is
        still reused for the same plate.
        """
        key = self.plate.key
        if self.nozzle_diameter is not None:
            key = f"{key}@{self.nozzle_diameter}"
        if self.nozzle_stats is not None:
            # A file that lets the slicer use either side must not be reused for one
            # that must keep to one side, nor the other way round.
            key = f"{key}^{','.join(self.nozzle_stats)}"
        if self.nozzle_volume_type is not None and any(
            flow != _STANDARD for flow in self.nozzle_volume_type
        ):
            # A High Flow file must not be reused for Standard, nor the other way round.
            # All Standard is what the slicer assumes without the key, so it keeps the key
            # it had, and a file recorded before #484 is still reused.
            key = f"{key}%{','.join(self.nozzle_volume_type)}"
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
        return Target(
            self.plate,
            self.nozzle_diameter,
            nozzle_stats=self.nozzle_stats,
            nozzle_volume_type=self.nozzle_volume_type,
        ).key


async def target_for(
    client: BambuddyClient,
    settings: StoredSettings,
    *,
    printer_id: int | None = None,
    nozzle_diameter: str | None = None,
    colours: Sequence[str] | None = None,
    nozzle_stats: Sequence[str] | None = None,
    nozzle_volume_type: Sequence[str] | None = None,
) -> Target:
    """The plate and nozzle the 3MF is laid out for.

    The plate is ``printer_id``'s, else the printer set in Settings, else the fallback
    plate; a printer Bambuddy reports without a model falls back the same way. The
    nozzle is stated only when the caller chose one: the print run does (spec
    2026-09-27 §4), the send bar does not. ``colours`` are the chosen spools' (#476),
    and only the print run has any. ``nozzle_stats`` (#834) are not the print run's
    alone: Generate's ``generate_target`` computes them too, so the file Generate lays
    out is the one the project's next print reuses. So are ``nozzle_volume_type``
    (#484), each extruder's flow.
    """
    chosen = tuple(colours) if colours is not None else None
    stats = tuple(nozzle_stats) if nozzle_stats is not None else None
    flows = tuple(nozzle_volume_type) if nozzle_volume_type is not None else None
    printer_id = printer_id if printer_id is not None else settings.printer_id
    if printer_id is None:
        # Nothing to resolve against, so do not spend a round trip finding out.
        return Target(_plate_for_model(None), nozzle_diameter, chosen, stats, flows)
    printer = next((row for row in await client.printers() if row.id == printer_id), None)
    model = printer.model if printer is not None else None
    return Target(_plate_for_model(model), nozzle_diameter, chosen, stats, flows)


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
        payload = replate_3mf(
            payload,
            target.plate,
            nozzle_diameter=target.nozzle_diameter,
            nozzle_stats=target.nozzle_stats,
            nozzle_volume_type=target.nozzle_volume_type,
        )
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


class CopyFound(NamedTuple):
    """A recorded copy Bambuddy still has (:func:`_still_there`)."""

    #: ``None`` when the check failed in a way that does not say the copy is gone, so
    #: its name is unknown (#863).
    filename: str | None


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
    store: OutputFiles,
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
    target = target if target is not None else await target_for(client, settings)
    payload = _laid_out_for(await read_3mf(store, meta), target)
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
    store: OutputFiles,
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
    The lock is taken in this process first, then in the database, so it holds across
    replicas too without every waiter here holding a database connection.

    A reused copy is read from Bambuddy first, to drop one that was deleted there, and
    that read also gives the file name, so any failure of it but a 404 raises here.
    :func:`ensure_uploaded` does not need the name and tolerates it (#863).
    """
    found = await _ensure_copy(
        client,
        store,
        uploads,
        meta,
        settings,
        target=target,
        folder_id=folder_id,
        stem=stem,
        tolerate_errors=False,
    )
    assert found.filename is not None  # only a tolerated check failure leaves it unknown
    return EnsuredCopy(found.library_file_id, found.filename, found.created)


class _Ensured(NamedTuple):
    library_file_id: int
    #: ``None`` only for a reused copy whose check failed and was tolerated.
    filename: str | None
    created: bool


async def _ensure_copy(
    client: BambuddyClient,
    store: OutputFiles,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    target: Target | None,
    folder_id: int | None,
    stem: str | None,
    tolerate_errors: bool,
) -> _Ensured:
    target = target if target is not None else await target_for(client, settings)
    folder = folder_id if folder_id is not None else settings.library_folder_id
    key = _copy_lock_key(meta.id, folder, settings)
    async with _copy_lock(key), uploads.copy_lock(f"{key[0] or ''}:{key[1]}"):
        for copy in await _reusable(uploads, meta, settings, target, folder):
            found = await _still_there(client, uploads, meta, copy, tolerate_errors=tolerate_errors)
            if found is not None:
                return _Ensured(copy.id, found.filename, created=False)
        library_file_id, filename = await upload_output(
            client, store, uploads, meta, settings, target=target, folder_id=folder_id, stem=stem
        )
    return _Ensured(library_file_id, filename, created=True)


#: One per folder while :func:`ensure_copy` holds it; weak, so an idle folder's lock is
#: dropped rather than kept for the life of the process.
_COPY_LOCKS: weakref.WeakValueDictionary[tuple[str | None, int | None], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _copy_lock_key(
    output_id: str, folder: int | None, settings: StoredSettings
) -> tuple[str | None, int | None]:
    """A project folder's lock is shared by every output: :func:`project_filename` lists
    the folder and the upload takes a name from that listing, so two outputs must not
    interleave there. The inbox keeps :func:`download_filename`, which is not made
    unique, so there only one output's copies are serialized."""
    return (output_id if is_inbox(folder, settings) else None, folder)


def _copy_lock(key: tuple[str | None, int | None]) -> asyncio.Lock:
    """This process's lock for a :func:`_copy_lock_key` key."""
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
    client: BambuddyClient,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    copy: LibraryCopy,
    *,
    tolerate_errors: bool = False,
) -> CopyFound | None:
    """``None`` once the copy is found deleted in Bambuddy, else a :class:`CopyFound`.

    Someone may have deleted it there since. Reusing a dead id would fail the slice or
    the slot read with an upstream 404, so it is read first, and a 404 is forgotten.

    Only a 404 says the copy is gone. With ``tolerate_errors`` any other failure keeps
    the copy and returns a :class:`CopyFound` with no ``filename``, so a transient Bambuddy error
    here is not a new way for a print or the dialog to fail (#863).
    """
    try:
        found = await client.library_file(copy.id)
    except ApiError as error:
        if error.status != status.HTTP_404_NOT_FOUND:
            if not tolerate_errors:
                raise
            logger.warning(
                "could not check a recorded library copy; using it as recorded",
                extra={"library_file_id": copy.id, "status": error.status},
            )
            return CopyFound(filename=None)
        logger.info(
            "a recorded library copy was deleted in Bambuddy; uploading it again",
            extra={"library_file_id": copy.id},
        )
        await uploads.forget(meta.id, copy.id)
        return None
    return CopyFound(filename=found.filename)


@dataclass(frozen=True)
class ReadableCopy:
    """A library file holding the output's 3MF, for reading its slots (#457)."""

    id: int
    #: Recolored for a run's spools (#476), so its filament colors are the spools', not
    #: the model's.
    recolored: bool


async def copy_to_read(
    client: BambuddyClient,
    store: OutputFiles,
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
        if await _still_there(client, uploads, meta, copy, tolerate_errors=True) is not None:
            return ReadableCopy(copy.id, recolored=_RECOLORED in copy.target_key)
    library_file_id = await ensure_uploaded(client, store, uploads, meta, settings)
    return ReadableCopy(library_file_id, recolored=False)


async def ensure_uploaded(
    client: BambuddyClient,
    store: OutputFiles,
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
    ensured = await _ensure_copy(
        client,
        store,
        uploads,
        meta,
        settings,
        target=target,
        folder_id=folder_id,
        stem=stem,
        tolerate_errors=True,
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
    settings: StoredSettings, slug: str | None, printer_id: int | None, request_scope: PrintOptions
) -> PrintOptions:
    """global → per-printer → per-model → per-request, least specific first.

    The print run's merge (#124). The send bar no longer queues (#312), so it resolves
    none. A library file (#313) has no model, so its per-model layer is empty.
    """
    return resolve(
        settings.print_options,
        settings.printer_print_options.get(str(printer_id)) if printer_id is not None else None,
        settings.model_print_options.get(slug) if slug is not None else None,
        request_scope,
    )


async def send_output(
    client: BambuddyClient,
    store: OutputFiles,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
) -> SendResult:
    """Upload the 3MF to the library and note the edit link on it. Nothing is queued.

    A copy already in the inbox for the same target is reused rather than uploaded again.
    """
    library_file_id, filename, created = await ensure_copy(
        client, store, uploads, meta, settings, target=None, folder_id=None
    )
    return SendResult(
        library_file_id=library_file_id,
        filename=filename,
        created=created,
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
