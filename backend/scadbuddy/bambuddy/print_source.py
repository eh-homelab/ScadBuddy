"""What a print is of (#313): an output ScadBuddy rendered, or a file already in
Bambuddy's library. The run (`print_run.execute_run`) reads everything that differs
between the two through :class:`PrintSource`; the resolver, slicing and queueing are
shared unchanged.

- :class:`OutputSource` is today's behavior, moved as is: the output's ``model.3mf``
  is uploaded on demand, replated for the printer (#105) and recolored for the spools
  (#476), and each queued plate is recorded on the output (#83).
- A library file prints as its author left it (``LibrarySource``, #313).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from fastapi import status

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.filaments import FilamentPlan, normalise_colour
from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.bambuddy.projects import folder_for
from scadbuddy.bambuddy.send import copy_to_read, ensure_uploaded, target_for
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, ProjectTarget, SlicedCopy
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import MODEL_NAME, OutputMeta, OutputStore, PlateSend
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.bambu3mf import plates_of


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

    @property
    def print_settings(self) -> dict[str, str]:
        """The template's default slicer settings (#770), the slice's process
        overrides; none for a file ScadBuddy did not render."""
        ...

    @property
    def lays_out(self) -> bool:
        """Whether the run lays the file out for the printer (#105), so it states the
        side the slicer may use (#834) and each side's flow (#484); a library file
        prints as its author left it."""
        ...

    async def plate_ids(self, client: BambuddyClient) -> list[int]: ...

    async def file_to_read(self, client: BambuddyClient) -> ReadFile: ...

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
    ) -> PrintFile: ...

    async def record(
        self,
        library_file_id: int,
        plate_id: int,
        outcome: QueueOutcome,
        project_id: int | None,
        sent: list[PlateSend],
    ) -> list[PlateSend]: ...

    async def remember_project(self, project_id: int, *, printer_id: int, nozzle_size: str) -> None:
        """After a print into a project: what the next Generate into it lays its file
        out for (#317)."""
        ...


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
    #: Names a copy uploaded into a project's folder (``project_filename``, #317).
    stem: str | None = None
    #: The template's ``print_settings`` as they are now (``Catalogue.print_settings``).
    print_settings: dict[str, str] = field(default_factory=dict)

    @property
    def colours(self) -> list[str]:
        return list(self.meta.colors)

    @property
    def options_slug(self) -> str | None:
        return self.meta.slug

    @property
    def lays_out(self) -> bool:
        return True

    async def plate_ids(self, client: BambuddyClient) -> list[int]:
        return [plate.index for plate in plates_of(self.store.directory(self.meta.id) / MODEL_NAME)]

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
        nozzle_stats: list[str] | None = None,
        nozzle_volume_type: list[str] | None = None,
    ) -> PrintFile:
        # Placed for the chosen printer's plate, stating the chosen nozzle (#105, #126).
        target = await target_for(
            client,
            self.settings,
            printer_id=printer_id,
            nozzle_diameter=nozzle_size,
            colours=await _spool_colours(client, self.meta, plan),
            nozzle_stats=nozzle_stats,
            nozzle_volume_type=nozzle_volume_type,
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
            stem=self.stem,
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

    async def remember_project(self, project_id: int, *, printer_id: int, nozzle_size: str) -> None:
        await self.uploads.remember_project_target(
            project_id, ProjectTarget(printer_id=printer_id, nozzle_diameter=nozzle_size)
        )


#: The ``file_type`` values the dialog prints from the library (spec 2026-09-28 §2). An
#: STL is one plate of one filament; Bambuddy's slice route takes it (the #313 probe,
#: recordings/README.md).
PRINTABLE_TYPES: frozenset[str] = frozenset({"3mf", "stl"})
#: A sliced file: printed from Bambuddy directly, never through the dialog.
SLICED_TYPE = "gcode.3mf"
#: The color of the one filament of a file Bambuddy reads none from (an STL, a 3MF
#: without slice metadata). ``normalise_colour`` reads it as unknown.
UNKNOWN_COLOUR = ""


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

    @property
    def print_settings(self) -> dict[str, str]:
        return {}

    @property
    def lays_out(self) -> bool:
        return False

    async def plate_ids(self, client: BambuddyClient) -> list[int]:
        return list(self.plates)

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
        nozzle_stats: list[str] | None = None,
        nozzle_volume_type: list[str] | None = None,
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

    async def remember_project(self, project_id: int, *, printer_id: int, nozzle_size: str) -> None:
        # A library file is not laid out by ScadBuddy, so it says nothing about what the
        # project's next Generate should target.
        return None
