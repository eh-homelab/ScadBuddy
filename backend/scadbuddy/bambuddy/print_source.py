"""What a print is of (#313): an output ScadBuddy rendered, or a file already in
Bambuddy's library. The run (`print_run.execute_run`) reads both through
:class:`PrintSource`, and the two differ only in how the 3MF to slice is obtained
(:meth:`PrintSource.fetch_3mf`, #1749, #1752): an output reads its ``model.3mf``, a
library file downloads its bytes, wrapping an STL in a 3MF of ScadBuddy's own.

Everything after that is one pipeline (:class:`PrintPipeline`), the same for both: the
3MF is laid out for the printer chosen as whoever laid it out allows (replated for the
plate, #105, when ScadBuddy did; an author's placement and plates kept), states the
nozzle side (#834) and each side's flow (#484), is recolored for the spools (#476) and
uploaded into the project's folder (#79) as a copy recorded under the subject (#316);
each queued plate is recorded by its :class:`PrintSubject` (#1750), its slice against
the copy, and a project remembers the printer and nozzle it printed on (#317).
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Protocol

import psycopg
from fastapi import status

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.filaments import FilamentPlan, normalise_colour
from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.bambuddy.options import options_scope
from scadbuddy.bambuddy.print_links import PrintLinkStore, PrintSend
from scadbuddy.bambuddy.projects import folder_for
from scadbuddy.bambuddy.send import (
    Printable,
    copy_to_read,
    ensure_uploaded,
    nozzles_stated,
    read_3mf,
    recorded_copy_to_read,
    target_for,
)
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.bambuddy.uploads import (
    BambuddyUploadStore,
    ProjectTarget,
    SlicedCopy,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.library.output_prints import OutputPrintStore
from scadbuddy.library.outputs import OutputFiles, OutputMeta, PlateSend, download_filename
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.bambu3mf import MAX_UNCOMPRESSED_BYTES, FilamentMap, plates_of, stl_3mf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReadFile:
    """The library file the filament step reads slots from, and the colors to show
    in place of the file's own (a copy recolored for a run, #457)."""

    id: int
    own_colours: list[str] | None = None


@dataclass(frozen=True)
class PrintFile:
    """The library file a run slices, and the folder it went into (#79)."""

    id: int
    folder_id: int | None = None


class PrintSource(Protocol):
    @property
    def subject(self) -> PrintSubject:
        """What the run prints: every record of it is keyed by this (#1750)."""
        ...

    @property
    def colours(self) -> list[str]:
        """One color per filament of the file, in slot order: what a slot with no
        spool keeps, and the fallback when Bambuddy reads no slots."""
        ...

    @property
    def options_scope(self) -> str | None:
        """What its remembered print options are kept under (#88, #1754): its subject's
        :func:`~scadbuddy.bambuddy.options.options_scope`."""
        ...

    @property
    def print_settings(self) -> dict[str, str]:
        """The template's default slicer settings (#770), the slice's process
        overrides; none for a file ScadBuddy did not render."""
        ...

    async def plate_ids(self, client: BambuddyClient) -> list[int]: ...

    @property
    def slices_whole_file(self) -> bool:
        """Whether its slice asks for the whole file rather than a plate
        (:attr:`~scadbuddy.bambuddy.dispatch.SlicePlan.whole_file`, #2180)."""
        ...

    async def fetch_3mf(self, client: BambuddyClient) -> bytes | None:
        """The 3MF to lay out and slice: the one thing that differs between an output
        and a library file (#1752). ``None`` when there are no bytes to lay out: a
        library file past :data:`MAX_DOWNLOAD_BYTES`, or an STL that holds no mesh,
        which prints as it is."""
        ...

    async def states_nozzles(self, client: BambuddyClient) -> bool:
        """Whether the file sliced states the side the slicer may use (#834) and each
        side's flow (#484), as chosen: anything but a 3MF nothing can be stated into
        (:func:`~scadbuddy.bambuddy.send.nozzles_stated`)."""
        ...

    async def file_to_read(self, client: BambuddyClient) -> ReadFile: ...

    async def file_read_already(self, client: BambuddyClient) -> ReadFile | None:
        """:meth:`file_to_read` when Bambuddy has it without an upload, else ``None``:
        what the print check reads slots from, since it uploads nothing (#1050)."""
        ...

    async def file_to_print(
        self,
        client: BambuddyClient,
        *,
        printer_id: int,
        nozzle_size: str,
        plan: FilamentPlan,
        project_id: int | None,
        nozzle_stats: list[str] | None = None,
        nozzle_volume_type: list[str] | None = None,
        filament_map: FilamentMap | None = None,
        tray_colours: Mapping[int, str] | None = None,
    ) -> PrintFile: ...

    async def record(
        self,
        library_file_id: int,
        plate_id: int,
        outcome: QueueOutcome,
        project_id: int | None,
        sent: list[PlateSend],
        *,
        run_id: str | None = None,
    ) -> list[PlateSend]: ...

    async def remember_project(self, project_id: int, *, printer_id: int, nozzle_size: str) -> None:
        """After a print into a project: what the next Generate into it lays its file
        out for (#317)."""
        ...


class _Fetching(Protocol):
    """What :class:`PrintPipeline` needs of the source it is mixed into."""

    @property
    def subject(self) -> PrintSubject: ...

    @property
    def colours(self) -> list[str]: ...

    @property
    def inbox_name(self) -> str: ...

    @property
    def origin_id(self) -> int | None: ...

    uploads: BambuddyUploadStore | None
    settings: StoredSettings | None
    sends: PrintLinkStore | None
    prints: OutputPrintStore | None
    stem: str | None
    _fetched: _Fetched | None

    async def fetch_3mf(self, client: BambuddyClient) -> bytes | None: ...


@dataclass(frozen=True)
class _Fetched:
    """:meth:`PrintSource.fetch_3mf`'s answer, read once per source, and whether
    anything can be stated into it."""

    payload: bytes | None
    stated: bool


async def _spool_colours(
    client: BambuddyClient,
    colours: list[str],
    plan: FilamentPlan,
    trays: Mapping[int, str] | None = None,
) -> list[str] | None:
    """One colour per filament of the file: the chosen spool's, or the file's own for a
    slot with no spool (#476). ``None`` when no spool is chosen at all, which leaves the
    file in its own colours."""
    if not plan.slots:
        return None
    rgba: dict[int, str | None] = {
        spool.id: normalise_colour(spool.rgba) for spool in await client.spools()
    }
    # A tray chosen for itself (#2164) prints in the tray's colour.
    rgba.update(trays or {})
    return [
        rgba.get(plan.spool_for(index + 1) or 0) or colour for index, colour in enumerate(colours)
    ]


async def record_sends(
    sends: PrintLinkStore | None,
    subject: PrintSubject,
    plate_id: int,
    outcome: QueueOutcome,
    project_id: int | None,
    run_id: str | None = None,
) -> None:
    """Record one plate's queue items under ``subject`` (#1750), for either source, with
    the run that queued them (#1751). Best effort: the plate is queued, and failing the
    run over its record would tell the user it was not (#976)."""
    if sends is None:
        return
    try:
        await sends.record_sends(
            subject,
            [
                PrintSend(
                    queue_item_id=item,
                    plate_id=plate_id,
                    printer_id=outcome.printer_id,
                    project_id=project_id,
                    slice_job_id=outcome.slice_job_id,
                    run_id=run_id,
                )
                for item in outcome.queue_item_ids
            ],
        )
    except psycopg.Error as exc:
        logger.warning("could not record the sends of %s: %s", subject.key, type(exc).__name__)


@dataclass(kw_only=True)
class PrintPipeline:
    """Everything a print does with its 3MF once it has it, the same for every source
    (#1752). A source mixes it in and adds only :meth:`PrintSource.fetch_3mf` and what
    it knows of itself (its subject, colors, plates and names)."""

    #: Where the copies are recorded (#316); every source that reads or prints has one.
    uploads: BambuddyUploadStore | None = None
    #: The run's settings, whose inbox a copy goes into; a source that only records has
    #: none.
    settings: StoredSettings | None = None
    #: Where each queued plate's items are recorded by subject (#1750).
    sends: PrintLinkStore | None = None
    #: Where :meth:`record` writes an output's last print (#1060); a source that only
    #: reads (the print dialog's options and checks) has none.
    prints: OutputPrintStore | None = None
    #: Names a copy uploaded into a project's folder (``project_filename``, #317).
    stem: str | None = None
    _fetched: _Fetched | None = field(default=None, init=False, repr=False, compare=False)

    async def _fetch(self: _Fetching, client: BambuddyClient) -> _Fetched:
        if self._fetched is None:
            payload = await self.fetch_3mf(client)
            stated = payload is not None and await asyncio.to_thread(nozzles_stated, payload)
            self._fetched = _Fetched(payload, stated)
        return self._fetched

    def _printable(self: _Fetching, client: BambuddyClient) -> Printable:
        async def fetch() -> bytes:
            fetched = await PrintPipeline._fetch(self, client)
            if fetched.payload is None:
                # file_to_print prints the library file itself rather than ask; nothing
                # else uploads for a source that has one.
                raise ApiError(status.HTTP_409_CONFLICT, f"{self.subject.key} has no 3MF to upload")
            return fetched.payload

        return Printable(
            key=self.subject.run_subject,
            colours=tuple(self.colours),
            inbox_name=self.inbox_name,
            fetch=fetch,
            origin_id=self.origin_id,
        )

    def _stores(self: _Fetching) -> tuple[BambuddyUploadStore, StoredSettings]:
        assert self.uploads is not None, "a source that reads or prints needs the uploads"
        assert self.settings is not None, "a source that reads or prints needs the settings"
        return self.uploads, self.settings

    async def states_nozzles(self: _Fetching, client: BambuddyClient) -> bool:
        return (await PrintPipeline._fetch(self, client)).stated

    async def file_to_read(self: _Fetching, client: BambuddyClient) -> ReadFile:
        uploads, settings = PrintPipeline._stores(self)
        copy = await copy_to_read(client, uploads, PrintPipeline._printable(self, client), settings)
        return ReadFile(copy.id, own_colours=list(self.colours) if copy.recolored else None)

    async def file_read_already(self: _Fetching, client: BambuddyClient) -> ReadFile | None:
        uploads, settings = PrintPipeline._stores(self)
        copy = await recorded_copy_to_read(
            client, uploads, PrintPipeline._printable(self, client), settings
        )
        if copy is None:
            return None
        return ReadFile(copy.id, own_colours=list(self.colours) if copy.recolored else None)

    async def file_to_print(
        self: _Fetching,
        client: BambuddyClient,
        *,
        printer_id: int,
        nozzle_size: str,
        plan: FilamentPlan,
        project_id: int | None,
        nozzle_stats: list[str] | None = None,
        nozzle_volume_type: list[str] | None = None,
        filament_map: FilamentMap | None = None,
        tray_colours: Mapping[int, str] | None = None,
    ) -> PrintFile:
        uploads, settings = PrintPipeline._stores(self)
        # A project's folder replaces the one from Settings for this send, which is what
        # puts the 3MF on Bambuddy's project page (#79). Resolved before the upload,
        # because the copy is looked up by (folder, target): a project gets a copy of its
        # own, and one another project printed from is neither moved nor deleted (#316).
        folder_id = await folder_for(client, project_id) if project_id is not None else None
        fetched = await PrintPipeline._fetch(self, client)
        if self.origin_id is not None and (
            fetched.payload is None or (not fetched.stated and folder_id is None)
        ):
            # Nothing to lay out, or nothing that would change it: the file in Bambuddy
            # already is what a copy in the inbox would be, and the run judges it as
            # Standard (``states_nozzles``).
            return PrintFile(self.origin_id)
        # Placed for the chosen printer's plate, stating the chosen nozzle (#105, #126).
        target = await target_for(
            client,
            settings,
            printer_id=printer_id,
            nozzle_diameter=nozzle_size,
            colours=await _spool_colours(client, list(self.colours), plan, tray_colours),
            nozzle_stats=nozzle_stats,
            nozzle_volume_type=nozzle_volume_type,
            filament_map=filament_map,
        )
        file_id = await ensure_uploaded(
            client,
            uploads,
            PrintPipeline._printable(self, client),
            settings,
            target=target,
            folder_id=folder_id,
            stem=self.stem,
        )
        return PrintFile(file_id, folder_id)

    async def record(
        self: _Fetching,
        library_file_id: int,
        plate_id: int,
        outcome: QueueOutcome,
        project_id: int | None,
        sent: list[PlateSend],
        *,
        run_id: str | None = None,
    ) -> list[PlateSend]:
        """Record one plate's queue items as soon as it is queued; returns every plate
        so far.

        Recorded per plate, not after the last one: a later plate failing to slice must
        not leave the plates already on Bambuddy's queue unknown (#83). ``plates``
        carries every plate of this print, since the single ids hold only the last. The
        plate's sliced file is recorded against the copy it was sliced from (#316), and
        the items by subject (#1750).
        """
        assert self.uploads is not None, "a source that records needs the uploads"
        await self.uploads.record_sliced(
            self.subject.run_subject,
            library_file_id,
            SlicedCopy(id=outcome.sliced_library_file_id, preset_key=outcome.preset_key),
        )
        sent = sent + [
            PlateSend(plate_id=plate_id, queue_item_id=item, slice_job_id=outcome.slice_job_id)
            for item in outcome.queue_item_ids
        ]
        output_id = self.subject.output_id
        if self.prints is not None and output_id is not None:
            # The output page's own last print (#1060), beside the subject's record.
            for queue_item_id in outcome.queue_item_ids:
                await asyncio.to_thread(
                    self.prints.record,
                    output_id,
                    queue_item_id=queue_item_id,
                    slice_job_id=outcome.slice_job_id,
                    project_id=project_id,
                    plates=sent,
                )
        await record_sends(self.sends, self.subject, plate_id, outcome, project_id, run_id)
        return sent

    async def remember_project(
        self: _Fetching, project_id: int, *, printer_id: int, nozzle_size: str
    ) -> None:
        assert self.uploads is not None, "a source that remembers needs the uploads"
        await self.uploads.remember_project_target(
            project_id, ProjectTarget(printer_id=printer_id, nozzle_diameter=nozzle_size)
        )


@dataclass
class OutputSource(PrintPipeline):
    """An output ScadBuddy rendered: its ``model.3mf``, read from the output store."""

    store: OutputFiles
    meta: OutputMeta
    #: The template's ``print_settings`` as they are now (``Catalogue.print_settings``).
    print_settings: dict[str, str] = field(default_factory=dict)

    @property
    def subject(self) -> PrintSubject:
        return PrintSubject.output(self.meta.id)

    @property
    def colours(self) -> list[str]:
        return list(self.meta.colors)

    @property
    def options_scope(self) -> str | None:
        return options_scope(self.subject, self.meta.slug)

    @property
    def inbox_name(self) -> str:
        return download_filename(self.meta)

    @property
    def slices_whole_file(self) -> bool:
        """Never: only a library file is sliced whole (#2180); a render names its plate
        as it always has."""
        return False

    @property
    def origin_id(self) -> int | None:
        return None

    async def plate_ids(self, client: BambuddyClient) -> list[int]:
        payload = await read_3mf(self.store, self.meta)
        return [plate.index for plate in plates_of(io.BytesIO(payload))]

    async def fetch_3mf(self, client: BambuddyClient) -> bytes | None:
        return await read_3mf(self.store, self.meta)


#: The ``file_type`` values the dialog prints from the library (spec 2026-09-28 §2). An
#: STL is one plate of one filament; Bambuddy's slice route takes it (the #313 probe,
#: recordings/README.md).
PRINTABLE_TYPES: frozenset[str] = frozenset({"3mf", "stl"})
#: A sliced file: printed from Bambuddy directly, never through the dialog.
SLICED_TYPE = "gcode.3mf"
#: The color of the one filament of a file Bambuddy reads none from (an STL, a 3MF
#: without slice metadata). ``normalise_colour`` reads it as unknown.
UNKNOWN_COLOUR = ""
#: A library file is untrusted; one larger than the archive cap could not be laid out
#: anyway, so it is never held whole (a 3MF's compressed size is below its inflated one).
MAX_DOWNLOAD_BYTES = MAX_UNCOMPRESSED_BYTES


def printable(file_type: str | None) -> bool:
    return (file_type or "").lower() in PRINTABLE_TYPES


def _refusal(file: LibraryFile) -> str:
    if (file.file_type or "").lower() == SLICED_TYPE:
        return f"{file.filename} is sliced already. Print it from Bambuddy."
    kind = file.file_type or "file of unknown type"
    return (
        f"ScadBuddy prints only 3MF and STL files from the library, and "
        f"{file.filename} is a {kind}."
    )


def _file_stem(filename: str) -> str:
    for suffix in (".3mf", ".stl"):
        if filename.lower().endswith(suffix):
            return filename[: -len(suffix)]
    return filename


@dataclass
class LibrarySource(PrintPipeline):
    """A file already in Bambuddy's library (#313), printed as any print is (#1752):
    its bytes are downloaded and go through the same pipeline as an output's. The file
    itself is never changed in place, moved or deleted; what is printed is a copy of it
    (:attr:`origin_id` is never taken for one)."""

    file_id: int
    colours: list[str]
    plates: list[int]
    #: The file's name and type, which name its copies and say whether it is an STL.
    filename: str = ""
    file_type: str = ""

    @classmethod
    async def load(
        cls,
        client: BambuddyClient,
        file_id: int,
        *,
        uploads: BambuddyUploadStore | None = None,
        settings: StoredSettings | None = None,
        sends: PrintLinkStore | None = None,
    ) -> LibrarySource:
        """Read the file, its plates and its filaments. A file deleted in Bambuddy is
        its 404; one the dialog cannot print is a 422 before anything else is read."""
        file = await client.library_file(file_id)
        if not printable(file.file_type):
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, _refusal(file))
        plates = sorted(plate.index for plate in (await client.library_plates(file_id)).plates)
        # Slots are 1-based; a slot Bambuddy numbers below 1 is not one the dialog can fill.
        needs = [
            need
            for need in (await client.filament_requirements(file_id)).filaments
            if need.slot_id >= 1
        ]
        colours = [UNKNOWN_COLOUR] * max((need.slot_id for need in needs), default=0)
        for need in needs:
            colours[need.slot_id - 1] = normalise_colour(need.color) or UNKNOWN_COLOUR
        # No plate metadata is one plate, and no filaments is one of unknown color: a
        # file laid out that way still has something on the bed to print.
        return cls(
            file_id=file_id,
            colours=colours or [UNKNOWN_COLOUR],
            plates=plates or [1],
            filename=file.filename,
            file_type=(file.file_type or "").lower(),
            uploads=uploads,
            settings=settings,
            sends=sends,
            stem=_file_stem(file.filename),
        )

    @property
    def subject(self) -> PrintSubject:
        return PrintSubject.library(self.file_id)

    @property
    def options_scope(self) -> str | None:
        return options_scope(self.subject, None)

    @property
    def print_settings(self) -> dict[str, str]:
        return {}

    @property
    def slices_whole_file(self) -> bool:
        """A file of one plate (#2180). Plate 0 slices every plate, so a file with more
        keeps naming the one chosen."""
        return len(self.plates) == 1

    @property
    def inbox_name(self) -> str:
        """Told apart from the file it is a copy of, which may sit in the same folder."""
        return f"{_file_stem(self.filename) or self.file_id} (ScadBuddy).3mf"

    @property
    def origin_id(self) -> int | None:
        return self.file_id

    async def plate_ids(self, client: BambuddyClient) -> list[int]:
        return list(self.plates)

    async def fetch_3mf(self, client: BambuddyClient) -> bytes | None:
        """The file's bytes, an STL wrapped in a 3MF of ScadBuddy's own (#1752)."""
        payload = await self._download(client)
        if payload is None or self.file_type != "stl":
            return payload
        try:
            return await asyncio.to_thread(
                stl_3mf, payload, model_name=_file_stem(self.filename) or "model"
            )
        except Exception as exc:
            # Untrusted bytes: whatever the mesh reader refuses, the STL prints as it is.
            logger.warning(
                "could not read the STL's mesh; printing it as it is",
                extra={"library_file_id": self.file_id, "error": type(exc).__name__},
            )
            return None

    async def _download(self, client: BambuddyClient) -> bytes | None:
        """The file's bytes, counted as they stream in; ``None`` past
        :data:`MAX_DOWNLOAD_BYTES`, the stream closed there and nothing kept."""
        data = bytearray()
        async with aclosing(client.download_library_file(self.file_id)) as chunks:
            async for chunk in chunks:
                data += chunk
                if len(data) > MAX_DOWNLOAD_BYTES:
                    logger.warning(
                        "library file too large to lay out; printing it as it is",
                        extra={"library_file_id": self.file_id},
                    )
                    return None
        return bytes(data)
