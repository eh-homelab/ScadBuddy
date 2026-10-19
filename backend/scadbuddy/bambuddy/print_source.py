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
from collections.abc import Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import BinaryIO, Protocol

import psycopg
from fastapi import status

from scadbuddy.bambuddy.client import THREE_MF_MEDIA_TYPE, BambuddyClient
from scadbuddy.bambuddy.concurrent import settled
from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.filaments import FilamentPlan, normalise_colour
from scadbuddy.bambuddy.filaments3mf import (
    LocalFilaments,
    SlicedProfile,
    UnreadableFilamentsError,
    read_filaments,
)
from scadbuddy.bambuddy.models import FilamentRequirements, LibraryFile
from scadbuddy.bambuddy.nozzle_plan import NozzlePlan
from scadbuddy.bambuddy.options import options_scope
from scadbuddy.bambuddy.print_links import PrintLinkStore, PrintSend
from scadbuddy.bambuddy.projects import folder_for
from scadbuddy.bambuddy.send import (
    Printable,
    Sink,
    copy_to_read,
    ensure_raw_copy,
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
from scadbuddy.print_library.copies import NATIVE_PROJECT_REFUSAL, ensure_native_copy
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.reads import LibraryReads, NativeCopies, reads_of
from scadbuddy.render.bambu3mf import MAX_UNCOMPRESSED_BYTES, FilamentMap, plates_of, stl_3mf
from scadbuddy.render.objects3mf import check_stl
from scadbuddy.render.read_budget import ReadBudget

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReadFile:
    """The library file the filament step reads slots from, and the colors to show
    in place of the file's own (a copy recolored for a run, #457). ``id`` is ``None``
    for an output whose slots were read from its own 3MF, with no copy in Bambuddy
    yet (#2720)."""

    id: int | None
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

    @property
    def is_sliced(self) -> bool:
        """A file sliced already (#2671): queued as it is, with nothing laid out, sliced
        or chosen for the slicer."""
        ...

    @property
    def sliced_profile(self) -> SlicedProfile | None:
        """What :attr:`is_sliced` file was sliced for, as far as it could be read."""
        ...

    async def recorded_slice(
        self, client: BambuddyClient | None, library_file_id: int, preset_key: str
    ) -> int | None:
        """A slice ScadBuddy already made of copy ``library_file_id`` with these presets
        (:meth:`record`) that Bambuddy still has, newest first: queued in place of
        slicing again (#2671). ``None`` when there is none.

        Matched on ``preset_key``, which leaves the filament colours out: a copy left
        uncoloured is shared by prints of other spools, so a reused slice keeps the
        colour metadata of the spools it was sliced for. That is #2169's trade-off for
        the background slice too: colours change no toolpath, and the queue carries
        the spools chosen now."""
        ...

    async def fetch_3mf(self, client: BambuddyClient | None) -> bytes | None:
        """The 3MF to lay out and slice: the one thing that differs between an output
        and a library file (#1752). ``None`` when there are no bytes to lay out: a
        library file past :data:`MAX_DOWNLOAD_BYTES`, or an STL that holds no mesh,
        which prints as it is."""
        ...

    async def fetched_3mf(self, client: BambuddyClient) -> bytes | None:
        """:meth:`fetch_3mf`, read once for the source and kept: what a check that also
        reads the file (#956) reads, rather than download it again (#2976)."""
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
        client: BambuddyClient | None,
        *,
        printer_id: int,
        nozzle_size: str,
        plan: FilamentPlan,
        project_id: int | None,
        folder_id: int | None = None,
        nozzle_stats: list[str] | None = None,
        nozzle_volume_type: list[str] | None = None,
        filament_map: FilamentMap | None = None,
        tray_colours: Mapping[int, str] | None = None,
        native_copy: bool = False,
        printer_model: str | None = None,
        attempt_since: datetime | None = None,
    ) -> PrintFile:
        """The library file the run slices: laid out for the printer and copied into
        Bambuddy's library, or with ``native_copy`` into ScadBuddy's own (#3818 tier A
        Task 4), which needs no ``client`` and lays out for ``printer_model``.
        ``attempt_since`` is when the run's first attempt was scheduled: only a native
        upload made since then is an earlier attempt's (#4312 re-review N-a)."""
        ...

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

    async def remember_project(
        self,
        project_id: int,
        *,
        printer_id: int,
        nozzle_size: str,
        nozzle_plan: NozzlePlan | None = None,
    ) -> None:
        """After a print into a project: what the next Generate into it lays its file
        out for (#317), its nozzle plan too (#2166)."""
        ...


def _misuse(message: str) -> ApiError:
    """A caller's mistake the print activities reach: an `ApiError`, which they raise as
    non-retryable (`raised_as`), so it fails the run once rather than being retried to
    the end of the policy as a `TypeError` would (#4312 review M6)."""
    return ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, message)


MAX_RECORDED_SLICES = 3
"""How many of ScadBuddy's recorded slices of a copy a print checks for one Bambuddy
still has, newest first (#2955): past that it slices again."""


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
    native: NativeCopies | None
    _fetched: _Fetched | None

    async def fetch_3mf(self, client: BambuddyClient | None) -> bytes | None: ...


@dataclass(frozen=True)
class _Fetched:
    """:meth:`PrintSource.fetch_3mf`'s answer, read once per source, and whether
    anything can be stated into it."""

    payload: bytes | None
    stated: bool


async def _spool_colours(
    client: BambuddyClient | None,
    colours: list[str],
    plan: FilamentPlan,
    trays: Mapping[int, str] | None = None,
) -> list[str] | None:
    """One colour per filament of the file: the chosen spool's, or the file's own for a
    slot with no spool (#476). ``None`` when no spool is chosen at all, which leaves the
    file in its own colours. Without a ``client`` (a native run, #3818) only trays are
    chosen, so Bambuddy's spools are not read."""
    if not plan.slots:
        return None
    rgba: dict[int, str | None] = (
        {spool.id: normalise_colour(spool.rgba) for spool in await client.spools()}
        if client is not None
        else {}
    )
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
    #: ScadBuddy's own library, where a native run's copy goes (#3818 tier A Task 4):
    #: `NativeLibrary` in the API's process, `RemoteLibrary` on the print worker.
    native: NativeCopies | None = None
    _fetched: _Fetched | None = field(default=None, init=False, repr=False, compare=False)

    async def _fetch(self: _Fetching, client: BambuddyClient | None) -> _Fetched:
        if self._fetched is None:
            payload = await self.fetch_3mf(client)
            stated = payload is not None and await asyncio.to_thread(nozzles_stated, payload)
            self._fetched = _Fetched(payload, stated)
        return self._fetched

    def _printable(self: _Fetching, client: BambuddyClient | None) -> Printable:
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

    async def fetched_3mf(self: _Fetching, client: BambuddyClient) -> bytes | None:
        return (await PrintPipeline._fetch(self, client)).payload

    async def recorded_slice(
        self: _Fetching, client: BambuddyClient | None, library_file_id: int, preset_key: str
    ) -> int | None:
        uploads = self.uploads
        if uploads is None:
            return None
        recorded = await uploads.slices_of(self.subject.run_subject, library_file_id, preset_key)
        # Bambuddy hands a deleted file's id to the next file, so an id alone could
        # queue another model's G-code (#2691 review): a slice is reused only while the
        # file at its id has the hash it was recorded with. One recorded with none
        # (its read failed, or it predates the check) is never reused.
        known = [sliced for sliced in recorded if sliced.file_hash is not None]
        # Only the newest few: each is a GET, one after another. A row whose file is
        # gone, or whose id now holds another file, is forgotten as it is found
        # (#2955), so the next reprint does not read it again.
        for sliced in known[:MAX_RECORDED_SLICES]:
            # Each id in its own library (Review Focus 2): a native copy's slice is a
            # native file (`print_sliced_upload`), never asked of Bambuddy.
            reads: LibraryReads | None = self.native if is_native_id(sliced.id) else client
            if reads is None:
                continue
            try:
                file = await reads.library_file(sliced.id)
            except ApiError as error:
                # Deleted in Bambuddy since, or unreadable: only a slice saved, so the
                # print slices again rather than fail over it.
                logger.info(
                    "a recorded slice is not reused",
                    extra={"library_file_id": sliced.id, "status": error.status},
                )
                if error.status == status.HTTP_404_NOT_FOUND:
                    await _forget_slice(uploads, self.subject.run_subject, sliced.id)
                continue
            # A native id is never reissued, so the recorded hash alone is the check
            # (#4312 review I2): a native slice is typed `gcode.3mf` by its name (#4207,
            # 19db20ab1), but one uploaded before that read as a plain 3MF.
            sliced_kind = is_native_id(sliced.id) or is_sliced(file.file_type)
            if sliced_kind and file.file_hash == sliced.file_hash:
                return sliced.id
            if file.file_hash is None or file.file_hash == sliced.file_hash:
                # No hash to compare, or the same file read as another type for now:
                # nothing says the id holds another file, so the row stays (#3896).
                logger.info(
                    "a recorded slice cannot be checked now; it is not reused",
                    extra={"library_file_id": sliced.id},
                )
                continue
            logger.info(
                "a recorded slice's id now holds another file; it is not reused",
                extra={"library_file_id": sliced.id},
            )
            await _forget_slice(uploads, self.subject.run_subject, sliced.id)
        return None

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
        client: BambuddyClient | None,
        *,
        printer_id: int,
        nozzle_size: str,
        plan: FilamentPlan,
        project_id: int | None,
        folder_id: int | None = None,
        nozzle_stats: list[str] | None = None,
        nozzle_volume_type: list[str] | None = None,
        filament_map: FilamentMap | None = None,
        tray_colours: Mapping[int, str] | None = None,
        native_copy: bool = False,
        printer_model: str | None = None,
        attempt_since: datetime | None = None,
    ) -> PrintFile:
        uploads, settings = PrintPipeline._stores(self)
        if native_copy:
            return await PrintPipeline._native_file_to_print(
                self,
                client,
                printer_id=printer_id,
                nozzle_size=nozzle_size,
                plan=plan,
                project_id=project_id,
                folder_id=folder_id,
                nozzle_stats=nozzle_stats,
                nozzle_volume_type=nozzle_volume_type,
                filament_map=filament_map,
                tray_colours=tray_colours,
                printer_model=printer_model,
                attempt_since=attempt_since,
            )
        if client is None:
            raise _misuse("a copy in Bambuddy's library needs a Bambuddy client")
        # A project's folder replaces the one from Settings for this send, which is what
        # puts the 3MF on Bambuddy's project page (#79). Resolved before the upload,
        # because the copy is looked up by (folder, target): a project gets a copy of its
        # own, and one another project printed from is neither moved nor deleted (#316).
        # A library folder chosen instead of a project (#3651) is used as it is.
        if project_id is not None:
            folder_id = await folder_for(client, project_id)
        fetched = await PrintPipeline._fetch(self, client)
        if fetched.payload is None and self.origin_id is None and isinstance(self, LibrarySource):
            # A native file (#3827): Bambuddy has no file to take as it is, so the file's
            # bytes go to it unchanged.
            return PrintFile(await self.raw_copy(client, folder_id), folder_id)
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

    async def _native_file_to_print(
        self: _Fetching,
        client: BambuddyClient | None,
        *,
        printer_id: int,
        nozzle_size: str,
        plan: FilamentPlan,
        project_id: int | None,
        folder_id: int | None,
        nozzle_stats: list[str] | None,
        nozzle_volume_type: list[str] | None,
        filament_map: FilamentMap | None,
        tray_colours: Mapping[int, str] | None,
        printer_model: str | None,
        attempt_since: datetime | None,
    ) -> PrintFile:
        """:meth:`file_to_print` on a native run (#3818 tier A Task 4): the copy is laid
        out as Bambuddy's is, for ``printer_model`` with no printer list read, and goes
        into ScadBuddy's own library (`ensure_native_copy`), into ``folder_id`` or the
        top level. A native library file with nothing to lay out is printed as it is: the
        native slicer reads it where it is, so it needs no copy at all."""
        uploads, settings = PrintPipeline._stores(self)
        native = self.native
        if native is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "a native print needs ScadBuddy's own library, which does not serve here",
            )
        if project_id is not None:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, NATIVE_PROJECT_REFUSAL)
        own_id = self.file_id if isinstance(self, LibrarySource) else None
        if own_id is not None and not is_native_id(own_id):
            raise _misuse(f"library file {own_id} is Bambuddy's: a native print has none")
        fetched = await PrintPipeline._fetch(self, client)
        if fetched.payload is None:
            if own_id is None:
                raise ApiError(status.HTTP_409_CONFLICT, f"{self.subject.key} has no 3MF to print")
            return PrintFile(own_id, folder_id)
        target = await target_for(
            None,
            settings,
            printer_id=printer_id,
            nozzle_diameter=nozzle_size,
            colours=await _spool_colours(client, list(self.colours), plan, tray_colours),
            nozzle_stats=nozzle_stats,
            nozzle_volume_type=nozzle_volume_type,
            filament_map=filament_map,
            model=printer_model,
        )
        file_id = await ensure_native_copy(
            native,
            uploads,
            PrintPipeline._printable(self, client),
            target=target,
            folder_id=folder_id,
            source_id=own_id,
            attempt_since=attempt_since,
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
            SlicedCopy(
                id=outcome.sliced_library_file_id,
                preset_key=outcome.preset_key,
                sliced_blob_key=outcome.sliced_blob_key,
            ),
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
        self: _Fetching,
        project_id: int,
        *,
        printer_id: int,
        nozzle_size: str,
        nozzle_plan: NozzlePlan | None = None,
    ) -> None:
        assert self.uploads is not None, "a source that remembers needs the uploads"
        await self.uploads.remember_project_target(
            project_id,
            ProjectTarget(
                printer_id=printer_id,
                nozzle_diameter=nozzle_size,
                nozzle_plan=nozzle_plan.model_dump() if nozzle_plan is not None else None,
            ),
        )


@dataclass
class OutputSource(PrintPipeline):
    """An output ScadBuddy rendered: its ``model.3mf``, read from the output store."""

    store: OutputFiles
    meta: OutputMeta
    #: The template's ``print_settings`` as they are now (``Catalogue.print_settings``).
    print_settings: dict[str, str] = field(default_factory=dict)
    #: The 3MF's filament slots, once :meth:`read_local` has read them (#2720).
    local: LocalFilaments | None = None
    _local_unreadable: bool = field(default=False, repr=False)
    #: The output's 3MF, read once for every reader of it in a request (#2780 review).
    #: Held as long as the source lives: a request's, or one activity's in the print
    #: and preview-slice workflows (print_activities ``_output_source``), each of which
    #: reads, uploads and ends. Build a source per request or activity, never one kept
    #: across them, or the file stays in memory with it.
    _payload: bytes | None = field(default=None, repr=False)

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

    async def _own_3mf(self) -> bytes:
        """The output's 3MF, read from the store once: its slots, plates, preview slice
        and run all read the same file in one request."""
        if self._payload is None:
            self._payload = await read_3mf(self.store, self.meta)
        return self._payload

    @property
    def is_sliced(self) -> bool:
        return False

    @property
    def sliced_profile(self) -> SlicedProfile | None:
        return None

    async def plate_ids(self, client: BambuddyClient) -> list[int]:
        payload = await self._own_3mf()
        return [plate.index for plate in plates_of(io.BytesIO(payload))]

    async def fetch_3mf(self, client: BambuddyClient | None) -> bytes | None:
        return await self._own_3mf()

    async def file_to_read(self, client: BambuddyClient) -> ReadFile:
        """The slots are the output's own 3MF's (#2720): reading them needs no copy in
        Bambuddy, whose upload (the 3MF laid out for the printer) kept an output's first
        dialog waiting ~20 s. Every copy of the output has the same slots. Only a 3MF
        they cannot be read from is uploaded, for Bambuddy to read."""
        if await self.read_local() is not None:
            return ReadFile(None)
        return await PrintPipeline.file_to_read(self, client)

    async def file_read_already(self, client: BambuddyClient) -> ReadFile | None:
        if await self.read_local() is not None:
            return ReadFile(None)
        return await PrintPipeline.file_read_already(self, client)

    async def read_local(self) -> LocalFilaments | None:
        """The 3MF's slots, read once (:attr:`local`); ``None`` when unreadable."""
        if self.local is None and not self._local_unreadable:
            payload = await self._own_3mf()
            try:
                self.local = await asyncio.to_thread(read_filaments, io.BytesIO(payload))
            except UnreadableFilamentsError as error:
                logger.info(
                    "could not read the output's filament slots; Bambuddy is asked instead",
                    extra={"output_id": self.meta.id, "reason": str(error)},
                )
                self._local_unreadable = True
        return self.local


#: A sliced file: queued as it is, never sliced again (#2671).
SLICED_TYPE = "gcode.3mf"
#: The ``file_type`` values the dialog prints from the library (spec 2026-09-28 §2). An
#: STL is one plate of one filament; Bambuddy's slice route takes it (the #313 probe,
#: recordings/README.md). A sliced file is queued as it is (#2671).
PRINTABLE_TYPES: frozenset[str] = frozenset({"3mf", "stl", SLICED_TYPE})
#: The color of the one filament of a file Bambuddy reads none from (an STL, a 3MF
#: without slice metadata). ``normalise_colour`` reads it as unknown.
UNKNOWN_COLOUR = ""
#: A library file is untrusted; one larger than the archive cap could not be rewritten
#: for the print anyway, so it is never held whole here, and prints as it is (a 3MF's
#: compressed size is below its inflated one). Arrange and a library file's preview
#: read one of any size (:meth:`LibrarySource.write_3mf`).
MAX_DOWNLOAD_BYTES = MAX_UNCOMPRESSED_BYTES


class SpoolFullError(Exception):
    """A library file's download stopped: the volume it is spooled to is nearly full.
    Not the file's fault, so never kept as its refusal."""

    def __init__(self) -> None:
        super().__init__(
            "ScadBuddy's cache volume has too little free space to download it; free some"
            " space, or give the volume more, and try again"
        )


class ReadOutOfMemoryError(Exception):
    """A 3MF (a library file, or an output's plate) this process ran out of memory
    reading. With no triangle limit (the default) nothing refuses a file for its size,
    so a huge one can; the file is not at fault and a smaller budget or more memory
    would read it, so this is never kept as its refusal."""

    def __init__(self) -> None:
        super().__init__(
            "ScadBuddy ran out of memory reading it; set a triangle limit under Settings →"
            " Projects & files (SCADBUDDY_READ_MAX_TRIANGLES) or give the server more memory"
        )


def printable(file_type: str | None) -> bool:
    return (file_type or "").lower() in PRINTABLE_TYPES


def is_sliced(file_type: str | None) -> bool:
    return (file_type or "").lower() == SLICED_TYPE


def _refusal(file: LibraryFile) -> str:
    kind = file.file_type or "file of unknown type"
    return (
        f"ScadBuddy prints only 3MF and STL files from the library, and "
        f"{file.filename} is a {kind}."
    )


#: Reads a library file's filament slots from the file itself (#2550):
#: :func:`~scadbuddy.bambuddy.library_view.library_filaments`, bound to its cache.
#: ``None`` for a file it cannot read them from.
FilamentReader = Callable[[LibraryFile], Awaitable[LocalFilaments | None]]


def given_filaments(local: LocalFilaments) -> FilamentReader:
    """A :data:`FilamentReader` answering ``local``, read already (#2550)."""

    async def read(file: LibraryFile) -> LocalFilaments | None:
        return local

    return read


async def _requirements(
    reads: LibraryReads, file: LibraryFile, read: FilamentReader | None
) -> tuple[FilamentRequirements, LocalFilaments | None]:
    """The whole file's slots: read from the file itself where ``read`` can, else
    ``reads``' ``filament-requirements``: Bambuddy's, which for a large unsliced 3MF can
    outlast the request (#2550), or the native library's, which reads none (#3827)."""
    local = await read(file) if read is not None else None
    if local is not None:
        return local.for_plate(None), local
    return await reads.filament_requirements(file.id), None


def _file_stem(filename: str) -> str:
    for suffix in (".3mf", ".stl"):
        if filename.lower().endswith(suffix):
            return filename[: -len(suffix)]
    return filename


#: A raw copy's ending and media type by the file's type (#3827): what it is, unchanged.
_RAW_KINDS = {
    SLICED_TYPE: (".gcode.3mf", THREE_MF_MEDIA_TYPE),
    "stl": (".stl", "model/stl"),
}


@dataclass
class LibrarySource(PrintPipeline):
    """A file already in Bambuddy's library (#313), or in ScadBuddy's own (#3827, read
    through :attr:`files`), printed as any print is (#1752): its bytes are downloaded and
    go through the same pipeline as an output's. The file itself is never changed in
    place, moved or deleted; what is printed is a copy of it (:attr:`origin_id` is never
    taken for one)."""

    file_id: int
    colours: list[str]
    plates: list[int]
    #: The file's name and type, which name its copies and say whether it is an STL.
    filename: str = ""
    file_type: str = ""
    #: The file's filament slots as read from the file itself, which the filament step
    #: then reads too, rather than asking Bambuddy per plate (#2550). ``None`` when
    #: they came from Bambuddy.
    local: LocalFilaments | None = None
    #: Where the file itself is read: the native library for a native file (#3827);
    #: ``None`` reads it from Bambuddy, the client every call is given.
    files: LibraryReads | None = None

    @classmethod
    async def load(
        cls,
        client: BambuddyClient | None,
        file_id: int,
        *,
        uploads: BambuddyUploadStore | None = None,
        settings: StoredSettings | None = None,
        sends: PrintLinkStore | None = None,
        filaments: FilamentReader | None = None,
        read_own: bool = False,
        files: LibraryReads | None = None,
        native: NativeCopies | None = None,
    ) -> LibrarySource:
        """Read the file, its plates and its filaments. A file deleted in Bambuddy is
        its 404; one the dialog cannot print is a 422 before anything else is read. The
        plates and filaments are read together (#2504). With ``filaments``, the slots
        are read from the file itself, and Bambuddy is asked only for one it cannot
        read (#2550). ``read_own`` reads them from the source's own download instead,
        the one its print uploads (:meth:`fetch_3mf`): where there is no cache to read,
        as in the print worker, which mounts no volume. ``files`` is where the file itself
        is read, when not from Bambuddy (#3827)."""
        reads = reads_of(files, client)
        file = await reads.library_file(file_id)
        if not printable(file.file_type):
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, _refusal(file))
        source = cls(
            file_id=file_id,
            colours=[],
            plates=[],
            filename=file.filename,
            file_type=(file.file_type or "").lower(),
            uploads=uploads,
            settings=settings,
            sends=sends,
            stem=_file_stem(file.filename),
            files=files,
            native=native,
        )
        if filaments is None and read_own:
            filaments = partial(source._own_filaments, client)
        layout, read = await asyncio.gather(
            reads.library_plates(file_id),
            _requirements(reads, file, filaments),
            return_exceptions=True,
        )
        plates = sorted(plate.index for plate in settled(layout).plates)
        requirements, local = settled(read)
        # Slots are 1-based; a slot Bambuddy numbers below 1 is not one the dialog can fill.
        needs = [need for need in requirements.filaments if need.slot_id >= 1]
        colours = [UNKNOWN_COLOUR] * max((need.slot_id for need in needs), default=0)
        for need in needs:
            colours[need.slot_id - 1] = normalise_colour(need.color) or UNKNOWN_COLOUR
        # No plate metadata is one plate, and no filaments is one of unknown color: a
        # file laid out that way still has something on the bed to print.
        source.colours = colours or [UNKNOWN_COLOUR]
        source.local = local
        held = source.sliced_profile
        # A sliced file prints only the plates it holds G-code for (#2671).
        source.plates = ([plate.index for plate in held.plates] if held else []) or plates or [1]
        return source

    async def _own_filaments(
        self, client: BambuddyClient | None, file: LibraryFile
    ) -> LocalFilaments | None:
        """The slots read from the bytes this source prints, downloaded once and kept for
        the print (:meth:`PrintPipeline._fetch`); ``None`` for a file they cannot be read
        from (an STL, one past :data:`MAX_DOWNLOAD_BYTES`, an unreadable 3MF)."""
        if self.is_sliced:
            # Never laid out, so not fetched for the print (:meth:`fetch_3mf`): read here.
            payload = await self.download(client)
        elif self.file_type == "3mf":
            payload = (await PrintPipeline._fetch(self, client)).payload
        else:
            return None
        if payload is None:
            return None
        try:
            return await asyncio.to_thread(
                partial(read_filaments, sliced=self.is_sliced), io.BytesIO(payload)
            )
        except UnreadableFilamentsError as error:
            logger.info(
                "could not read the library file's filament slots; Bambuddy is asked instead",
                extra={"library_file_id": file.id, "reason": str(error)},
            )
            return None

    @property
    def subject(self) -> PrintSubject:
        return PrintSubject.library(self.file_id)

    @property
    def is_sliced(self) -> bool:
        return self.file_type == SLICED_TYPE

    @property
    def sliced_profile(self) -> SlicedProfile | None:
        return self.local.profile if self.is_sliced and self.local is not None else None

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
        """``None`` for a native file (#3827): Bambuddy has no file of that id."""
        return None if is_native_id(self.file_id) else self.file_id

    def _reads(self, client: BambuddyClient | None) -> LibraryReads:
        return reads_of(self.files, client)

    async def file_to_read(self, client: BambuddyClient) -> ReadFile:
        """A native file's slots read from the file itself need no copy in Bambuddy (as
        an output's, #2720); one with no 3MF to upload is read from its raw copy."""
        if self.origin_id is None:
            if self.local is not None:
                return ReadFile(None)
            if (await PrintPipeline._fetch(self, client)).payload is None:
                return ReadFile(await self.raw_copy(client, None))
        return await PrintPipeline.file_to_read(self, client)

    async def file_read_already(self, client: BambuddyClient) -> ReadFile | None:
        if self.origin_id is None and self.local is not None:
            return ReadFile(None)
        return await PrintPipeline.file_read_already(self, client)

    async def raw_copy(self, client: BambuddyClient, folder_id: int | None) -> int:
        """A native file's bytes uploaded to Bambuddy as they are, into ``folder_id`` or
        the inbox (#3827): what prints a native file with no 3MF to lay out (a sliced
        file, an STL that will not wrap, one past :data:`MAX_DOWNLOAD_BYTES`). Recorded
        under the run subject, so a reprint reuses it while Bambuddy has it."""
        if self.origin_id is not None:
            raise AssertionError(f"library file {self.file_id} is Bambuddy's own: print it")
        uploads, settings = PrintPipeline._stores(self)
        ending, media_type = _RAW_KINDS.get(self.file_type, (".3mf", THREE_MF_MEDIA_TYPE))
        stem = self.filename
        if stem.lower().endswith(ending):
            stem = stem[: -len(ending)]
        reads = self._reads(client)

        async def write(out: Sink) -> None:
            async with aclosing(reads.download_library_file(self.file_id)) as chunks:
                async for chunk in chunks:
                    await asyncio.to_thread(out.write, chunk)

        return await ensure_raw_copy(
            client,
            uploads,
            self.subject.run_subject,
            settings,
            folder_id=folder_id,
            filename=f"{stem or self.file_id} (ScadBuddy){ending}",
            media_type=media_type,
            write=write,
        )

    async def plate_ids(self, client: BambuddyClient) -> list[int]:
        return list(self.plates)

    async def fetch_3mf(self, client: BambuddyClient | None) -> bytes | None:
        """The file's bytes, an STL wrapped in a 3MF of ScadBuddy's own (#1752). None for
        a sliced file, which is queued as it is (#2671)."""
        if self.is_sliced:
            return None
        payload = await self.download(client)
        if payload is None or self.file_type != "stl":
            return payload
        try:
            return await asyncio.to_thread(
                stl_3mf, payload, model_name=_file_stem(self.filename) or "model"
            )
        except MemoryError:
            # Deliberately the same fallback (#2502): the STL prints as it is, which
            # Bambuddy's slicer reads itself, so a print needs no wrap. Unlike Arrange's
            # read (:meth:`write_3mf`) nothing here is kept as the file's refusal, so
            # there is nothing to protect by refusing; only the log names the cause.
            logger.warning(
                "ran out of memory wrapping the STL; printing it as it is",
                extra={"library_file_id": self.file_id, "error": "MemoryError"},
            )
            return None
        except Exception as exc:
            # Untrusted bytes: whatever the mesh reader refuses, the STL prints as it is.
            logger.warning(
                "could not read the STL's mesh; printing it as it is",
                extra={"library_file_id": self.file_id, "error": type(exc).__name__},
            )
            return None

    async def write_3mf(
        self,
        client: BambuddyClient | None,
        out: BinaryIO,
        budget: ReadBudget,
        room: Callable[[], bool] = lambda: True,
    ) -> bool:
        """Write the 3MF :meth:`fetch_3mf` would answer into ``out``, whatever its size:
        what Arrange and a library file's preview read, which spend ``budget`` rather
        than refuse a file for being large, so only a chunk at a time is held here. An
        STL is wrapped only within ``budget`` (:func:`check_stl`, raising
        :class:`OverBudgetError`), since wrapping loads it whole: the STL and its 3MF are
        then both in memory, a peak ``budget`` bounds. The download stops, as
        :class:`SpoolFullError`, once ``room`` says the volume ``out`` is on is too full
        to go on. ``False`` for an STL that holds no mesh."""
        async with aclosing(self._reads(client).download_library_file(self.file_id)) as chunks:
            async for chunk in chunks:
                if not await asyncio.to_thread(room):
                    raise SpoolFullError
                await asyncio.to_thread(out.write, chunk)
        logger.info(
            "spooled a library file",
            extra={"library_file_id": self.file_id, "bytes": await asyncio.to_thread(out.tell)},
        )
        if self.file_type != "stl":
            return True
        await asyncio.to_thread(check_stl, out, budget)
        try:
            await asyncio.to_thread(self._wrap_stl, out)
        except MemoryError:
            # Not "holds no mesh": that would be kept as the file's refusal for good.
            raise ReadOutOfMemoryError from None
        except Exception as exc:
            logger.warning(
                "could not read the STL's mesh",
                extra={"library_file_id": self.file_id, "error": type(exc).__name__},
            )
            return False
        return True

    def _wrap_stl(self, out: BinaryIO) -> None:
        out.seek(0)
        wrapped = stl_3mf(out.read(), model_name=_file_stem(self.filename) or "model")
        out.seek(0)
        out.truncate()
        out.write(wrapped)

    async def download(self, client: BambuddyClient | None) -> bytes | None:
        """The file's bytes, counted as they stream in; ``None`` past
        :data:`MAX_DOWNLOAD_BYTES`, the stream closed there and nothing kept. ``client``
        may be ``None`` for a source with ``files``."""
        data = bytearray()
        async with aclosing(self._reads(client).download_library_file(self.file_id)) as chunks:
            async for chunk in chunks:
                data += chunk
                if len(data) > MAX_DOWNLOAD_BYTES:
                    logger.warning(
                        "library file too large to lay out; printing it as it is",
                        extra={"library_file_id": self.file_id},
                    )
                    return None
        return bytes(data)


async def _forget_slice(uploads: BambuddyUploadStore, output_id: str, sliced_id: int) -> None:
    """Forget a recorded slice not to reuse, best effort: it only spares a later reprint
    the read, so a failure never fails this plan (#3495 review)."""
    try:
        await uploads.forget_slice(output_id, sliced_id)
    except Exception:
        logger.warning(
            "could not forget a recorded slice",
            extra={"library_file_id": sliced_id},
            exc_info=True,
        )
