"""What a print is of (#313): an output ScadBuddy rendered, or a file already in
Bambuddy's library. The run (`print_run.run_print`) reads everything that differs
between the two through :class:`PrintSource`; the resolver, slicing and queueing are
shared unchanged.

- :class:`OutputSource` is today's behavior, moved as is: the output's ``model.3mf``
  is uploaded on demand, replated for the printer (#105) and recolored for the spools
  (#476), and each queued plate is recorded on the output (#83).
- A library file prints as its author left it (``LibrarySource``, #313).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import status

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.filaments import FilamentPlan, normalise_colour
from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.bambuddy.projects import folder_for
from scadbuddy.bambuddy.send import copy_to_read, ensure_uploaded, target_for
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, SlicedCopy
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import MODEL_NAME, OutputMeta, OutputStore, PlateSend
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.bambu3mf import plate_filaments, plates_of


@dataclass(frozen=True)
class ReadFile:
    """The library file the filament step reads slots from, and the colors to show
    in place of the file's own (an output's copy recolored for a run, #457)."""

    id: int
    own_colours: list[str] | None = None


@dataclass(frozen=True)
class PrintFile:
    """The library file a run slices, and the folder it went into (#79)."""

    id: int
    folder_id: int | None = None


class PrintSource(Protocol):
    @property
    def colours(self) -> list[str]:
        """One color per filament of the file, in slot order: what a slot with no
        spool keeps, and the fallback when Bambuddy reads no slots."""
        ...

    @property
    def options_slug(self) -> str | None:
        """The model whose remembered print options apply (#88); ``None`` for none."""
        ...

    async def plate_ids(self, client: BambuddyClient) -> list[int]: ...

    async def used_slots(self, client: BambuddyClient, plate_ids: list[int]) -> set[int]:
        """The filament slots the printed plates use (#469), which the run's extruder
        check reads before anything is uploaded or sliced."""
        ...

    async def file_to_read(self, client: BambuddyClient) -> ReadFile: ...

    async def file_to_print(
        self,
        client: BambuddyClient,
        *,
        printer_id: int,
        nozzle_size: str,
        plan: FilamentPlan,
        project_id: int | None,
    ) -> PrintFile: ...

    async def record(
        self,
        library_file_id: int,
        plate_id: int,
        outcome: QueueOutcome,
        project_id: int | None,
        sent: list[PlateSend],
    ) -> list[PlateSend]: ...


async def _spool_colours(
    client: BambuddyClient, meta: OutputMeta, plan: FilamentPlan
) -> list[str] | None:
    """One colour per filament of the output: the chosen spool's, or the model's own
    for a slot with no spool (#476). ``None`` when no spool is chosen at all, which
    leaves the file in the model's colours."""
    if not plan.slots:
        return None
    rgba = {spool.id: normalise_colour(spool.rgba) for spool in await client.spools()}
    return [
        rgba.get(plan.spool_for(index + 1) or 0) or colour
        for index, colour in enumerate(meta.colors)
    ]


@dataclass
class OutputSource:
    store: OutputStore
    uploads: BambuddyUploadStore
    meta: OutputMeta
    settings: StoredSettings

    @property
    def colours(self) -> list[str]:
        return list(self.meta.colors)

    @property
    def options_slug(self) -> str | None:
        return self.meta.slug

    async def plate_ids(self, client: BambuddyClient) -> list[int]:
        return [plate.index for plate in plates_of(self.store.directory(self.meta.id) / MODEL_NAME)]

    async def used_slots(self, client: BambuddyClient, plate_ids: list[int]) -> set[int]:
        """Read from the local 3MF so the check still runs before the upload. A plate
        the file doesn't say about counts as using every filament of the model."""
        every = set(range(1, len(self.meta.colors) + 1))
        by_plate = plate_filaments(self.store.directory(self.meta.id) / MODEL_NAME)
        return set().union(*(by_plate.get(plate, every) for plate in plate_ids)) & every

    async def file_to_read(self, client: BambuddyClient) -> ReadFile:
        copy = await copy_to_read(client, self.store, self.uploads, self.meta, self.settings)
        return ReadFile(copy.id, own_colours=list(self.meta.colors) if copy.recolored else None)

    async def file_to_print(
        self,
        client: BambuddyClient,
        *,
        printer_id: int,
        nozzle_size: str,
        plan: FilamentPlan,
        project_id: int | None,
    ) -> PrintFile:
        # Placed for the chosen printer's plate, stating the chosen nozzle (#105, #126).
        target = await target_for(
            client,
            self.settings,
            printer_id=printer_id,
            nozzle_diameter=nozzle_size,
            colours=await _spool_colours(client, self.meta, plan),
        )
        # A project's folder replaces the one from Settings for this send, which is what
        # puts the 3MF on Bambuddy's project page (#79). Resolved before the upload,
        # because the copy is looked up by (folder, target): a project gets a copy of its
        # own, and one another project printed from is neither moved nor deleted (#316).
        folder_id = await folder_for(client, project_id) if project_id is not None else None
        file_id = await ensure_uploaded(
            client,
            self.store,
            self.uploads,
            self.meta,
            self.settings,
            target=target,
            folder_id=folder_id,
        )
        return PrintFile(file_id, folder_id)

    async def record(
        self,
        library_file_id: int,
        plate_id: int,
        outcome: QueueOutcome,
        project_id: int | None,
        sent: list[PlateSend],
    ) -> list[PlateSend]:
        """Record one plate's queue items as soon as it is queued; returns every plate
        so far.

        Recorded per plate, not after the last one: a later plate failing to slice must
        not leave the plates already on Bambuddy's queue unknown to the output (#83).
        ``plates`` carries every plate of this print, since the single ids hold only the
        last. The plate's sliced file is recorded against the copy it was sliced from
        (#316).
        """
        await self.uploads.record_sliced(
            self.meta.id,
            library_file_id,
            SlicedCopy(id=outcome.sliced_library_file_id, preset_key=outcome.preset_key),
        )
        sent = sent + [
            PlateSend(plate_id=plate_id, queue_item_id=item, slice_job_id=outcome.slice_job_id)
            for item in outcome.queue_item_ids
        ]
        for queue_item_id in outcome.queue_item_ids:
            self.store.record_send(
                self.meta.id,
                queue_item_id=queue_item_id,
                print_route="slice_queue",
                slice_job_id=outcome.slice_job_id,
                project_id=project_id,
                plates=sent,
            )
        return sent


#: The ``file_type`` values the dialog prints from the library (spec 2026-09-28 §2).
PRINTABLE_TYPES: frozenset[str] = frozenset({"3mf"})
#: A sliced file: printed from Bambuddy directly, never through the dialog.
SLICED_TYPE = "gcode.3mf"
#: The color of the one filament of a file Bambuddy reads none from (an STL, a 3MF
#: without slice metadata). ``normalise_colour`` reads it as unknown.
UNKNOWN_COLOUR = ""


def printable(file_type: str | None) -> bool:
    return (file_type or "").lower() in PRINTABLE_TYPES


def _refusal(file: LibraryFile) -> str:
    if file.file_type == SLICED_TYPE:
        return f"{file.filename} is sliced already. Print it from Bambuddy."
    kind = file.file_type or "file of unknown type"
    return f"ScadBuddy prints only 3MF files from the library, and {file.filename} is a {kind}."


@dataclass(frozen=True)
class LibrarySource:
    """A file already in Bambuddy's library (#313), printed as its author left it:
    never uploaded, replated or recolored, and recorded nowhere in ScadBuddy."""

    file_id: int
    colours: list[str]
    plates: list[int]
    options_slug: str | None = None

    @classmethod
    async def load(cls, client: BambuddyClient, file_id: int) -> LibrarySource:
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
        return cls(file_id=file_id, colours=colours or [UNKNOWN_COLOUR], plates=plates or [1])

    async def plate_ids(self, client: BambuddyClient) -> list[int]:
        return list(self.plates)

    async def used_slots(self, client: BambuddyClient, plate_ids: list[int]) -> set[int]:
        """The slots Bambuddy marks used on each printed plate. A plate it reads no
        slots for counts as using every filament of the file, as an output's does."""
        every = set(range(1, len(self.colours) + 1))
        used: set[int] = set()
        for plate_id in plate_ids:
            answer = await client.filament_requirements(self.file_id, plate_id=plate_id)
            own = {need.slot_id for need in answer.filaments if need.used_in_plate}
            used |= own or every
        return used & every

    async def file_to_read(self, client: BambuddyClient) -> ReadFile:
        return ReadFile(self.file_id)

    async def file_to_print(
        self,
        client: BambuddyClient,
        *,
        printer_id: int,
        nozzle_size: str,
        plan: FilamentPlan,
        project_id: int | None,
    ) -> PrintFile:
        return PrintFile(self.file_id)

    async def record(
        self,
        library_file_id: int,
        plate_id: int,
        outcome: QueueOutcome,
        project_id: int | None,
        sent: list[PlateSend],
    ) -> list[PlateSend]:
        # Recorded nowhere in ScadBuddy: Bambuddy's queue and archives are the record
        # (print history is #305).
        return sent
