"""`OrderIntegrations`: the one seam between orders and Bambuddy (#4253, spec 2026-10-18
§8, Rulings 3 and 7).

Everything orders does with Bambuddy is in ``orders/bambuddy.py``, reached only through
this seam. Without ``bambuddy_url`` (F10) the seam is :data:`NOT_LINKED`, which does
nothing and answers "not linked", so a deployment with no Bambuddy (the native path,
#3818) is missing these mirrors and nothing else. :func:`choose` picks the seam for the
running settings; ``OrdersComponent.integrations`` re-chooses it whenever the running
``bambuddy_url`` differs from the one it chose for, which ``api/runtime.py`` re-applies on
every ``settings.changed``, so setting the URL later lights it up without a restart.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.orders.models import FilamentEffect
from scadbuddy.orders.problems import PROBLEMS

if TYPE_CHECKING:
    from scadbuddy.bambuddy.models import SpoolAssignment
    from scadbuddy.bambuddy.projects import FolderlessProjects


class LocalDensity(BaseModel):
    """A Bambuddy local preset's name and ``filament_density``."""

    name: str
    density_g_cm3: str


class SpoolGroup(BaseModel):
    """One filament of ScadBuddy's spools (§8.3, #6262 Task 9): every unarchived SB spool
    of the same effective material, subtype, brand and ``#RRGGBBAA``
    (`orders/spool_import.py` `spool_groups`), in orders' own shape. An opaque and a
    translucent spool of one hue are two groups (#6316)."""

    material: str
    subtype: str | None = None
    brand: str | None = None
    colour_name: str | None = None
    #: ``#RRGGBB`` from the spools' ``rgba``; ``None`` for a group known only by its
    #: colour's name, which a record cannot carry as its colour.
    colour: str | None = None
    #: The spools' ``rgba`` alpha byte (255 opaque), the newest spool's colour stops
    #: (``#RRGGBBAA``) and effect (#6209).
    alpha: int = 255
    extra_colours: list[str] = Field(default_factory=list)
    effect: FilamentEffect | None = None
    #: ``(SB spool number, cost_per_kg)`` of each spool, by number; a spool with no
    #: effective price has ``None``. A higher number is a spool made later.
    spools: list[tuple[int, Decimal | None]] = Field(default_factory=list)
    #: What gives a record its density (#6136, `orders/density.py` `resolve`): the
    #: newest spool's effective preset id that names one (its H2C 0.4 preset first);
    #: ``None`` when no spool names one.
    slicer_filament: str | None = None
    #: The Bambuddy local preset ``slicer_filament`` names, when it is one with a density
    #: (a preset the Bambuddy pull carried, #6262 Task 7 ruling R1).
    local_preset: LocalDensity | None = None
    #: The newest spool's own effective density (its, else its type's, subtype's or
    #: material's), before every preset (Task 9 review ruling).
    spool_density: SpoolDensity | None = None
    #: The preset id (``tray_info_idx``) of the tray a registry printer's latest report
    #: holds the newest held spool in: the last step, after the spool's own presets.
    tray_filament: str | None = None
    #: The Bambuddy local preset ``tray_filament`` names, when it is one with a density.
    tray_local_preset: LocalDensity | None = None

    @property
    def rgba(self) -> str | None:
        """The group's ``#RRGGBBAA``; ``None`` for one with no colour."""
        return None if self.colour is None else f"{self.colour.upper()}{self.alpha:02X}"


class SpoolDensity(BaseModel):
    """An SB spool's effective density, and its number, which the book names it by."""

    number: int
    density_g_cm3: str


class ShelfSpool(BaseModel):
    """One SB spool and where it is loaded (§5.4 "Spools", #4251, #6262 Task 9), in
    orders' own shape (`orders/spools.py` `shelf_spools`)."""

    #: The SB spool's ``number``, what a plan names it by (``ids`` ``"sb"``).
    id: int
    material: str
    subtype: str | None = None
    brand: str | None = None
    #: ``#RRGGBBAA``; None for one with no colour, which is never taken for a record's.
    rgba: str | None = None
    remaining_g: float = 0.0
    archived: bool = False
    #: The registry printer it is loaded in; None on the shelf or in a printer the
    #: registry does not hold.
    loaded_printer: str | None = None
    #: In a tray of any printer (a registry printer's, or Bambuddy's assignment to one
    #: the registry does not hold); False on the shelf.
    loaded: bool = False


class ArchiveSlot(BaseModel):
    """One filament slot a plate archive's sliced file used (``extra_data.filament_slots``):
    ``slot_id`` from 1, the slicer's grams, and the colour the file names."""

    slot_id: int
    used_g: float | None = None
    color: str | None = None


class ArchiveFacts(BaseModel):
    """One Bambuddy archive (one plate's print) as an attempt copies it (§8.2), in
    orders' own shape."""

    archive_id: int
    #: Bambuddy's ``status`` (``completed``, ``failed``, …), shown, never counted.
    status: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    actual_seconds: int | None = None
    #: ``total_filament_actual_grams``, else ``filament_used_grams``.
    grams: float | None = None
    #: The photos Bambuddy captured as the print finished (``finish_``), oldest first.
    finish_photos: list[str] = Field(default_factory=list)
    #: Whether the archive has a timelapse (pulled off the printer) to copy.
    timelapse: bool = False
    #: The name its timelapse is attached under (``timelapse-<id>.mp4``), so the copy
    #: needs no second read of the archive (#5263).
    timelapse_name: str | None = None
    #: The plate of its file it printed (from 1); None when Bambuddy did not say.
    plate_id: int | None = None
    #: The slicer's estimate of the print time (``print_time_seconds``).
    print_seconds: int | None = None
    #: The printed file's name, as Bambuddy archived it.
    filename: str | None = None
    #: The filament slots the sliced file used, with the slicer's grams (#4777).
    slots: list[ArchiveSlot] = Field(default_factory=list)


class MaintenanceTask(BaseModel):
    """A task of Bambuddy's maintenance tracker, in orders' own shape (``MaintenanceStatus``
    in Bambuddy's ``backend/app/schemas/maintenance.py``), for a machine rate (#4625)."""

    task: str
    #: Print-hours for an ``hours`` task, calendar days for a ``days`` one.
    interval_hours: float
    interval_type: Literal["hours", "days"] = "hours"
    enabled: bool = True


#: What of an archive can be copied: an attempt's finish photo and timelapse (§8.2), or
#: the printed file itself, which a past print's item copies into the native library
#: (#4777, order operations spec §3.2).
ArchiveMedia = Literal["finished_photo", "timelapse", "file"]


class CopiedMedia(BaseModel):
    """An archive's file written to disk: the name it is attached under, its sha256 and
    its size."""

    filename: str
    sha256: str
    size: int


class MediaTooLargeError(Exception):
    """The archive's file is larger than an attachment may be."""


class NotLinkedError(Exception):
    """The seam has no integration to ask: ``bambuddy_url`` is not set."""


def not_linked() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "Bambuddy is not linked: set its URL in Settings",
        type_=PROBLEMS + "not_linked",
    )


class OrderIntegrations(Protocol):
    #: Whether a Bambuddy is linked.
    @property
    def linked(self) -> bool: ...

    async def local_densities(self) -> dict[str, LocalDensity]:
        """Bambuddy's local filament presets with a density, by the id an SB spool's
        preset names them by (#6136); :class:`NotLinkedError` without a Bambuddy."""
        ...

    async def spool_assignments(self) -> list[SpoolAssignment]:
        """Bambuddy's tray assignments, which place a linked SB spool no registry printer
        reports (plan Decision 3, until #6263); none when Bambuddy refuses, so the spools
        are placed by the reports alone. :class:`NotLinkedError` without a Bambuddy."""
        ...

    async def archive(self, archive_id: int) -> ArchiveFacts | None:
        """One archive's facts (§8.2); None for one Bambuddy deleted.
        :class:`NotLinkedError` without a Bambuddy."""
        ...

    async def archive_cost(self, archive_id: int) -> float | None:
        """Bambuddy's cost of one archive, corrected for suspect filament readings
        (``bambuddy/costs.py``, §6.4); None for one Bambuddy deleted or never priced.
        :class:`NotLinkedError` without a Bambuddy."""
        ...

    async def download_archive_media(
        self, archive_id: int, what: ArchiveMedia, name: str | None, target: Path, limit: int
    ) -> CopiedMedia:
        """The archive's ``finish_`` photo ``name``, its timelapse (attached as ``name``,
        else as its archive names it) or its printed file (``file``, named as archived)
        written to
        ``target``, through Bambuddy's media routes. :class:`MediaTooLargeError` past
        ``limit`` bytes, with ``target`` removed."""
        ...

    async def maintenance_tasks(self, printer_id: int) -> list[MaintenanceTask]:
        """Bambuddy printer ``printer_id``'s maintenance tasks and their intervals
        (#4625). :class:`NotLinkedError` without a Bambuddy."""
        ...

    async def project_exists(self, project_id: int) -> bool:
        """Whether Bambuddy still has project ``project_id`` (#5263: one deleted there is
        made again). :class:`NotLinkedError` without a Bambuddy."""
        ...

    async def ensure_project(self, name: str, parent_id: int | None) -> int:
        """A new Bambuddy project named ``name``, nested under ``parent_id``, with its
        library folder (§8.1, ``bambuddy/projects.py`` ``ensure_project``); its id.
        :class:`NotLinkedError` without a Bambuddy."""
        ...


class NotLinked:
    """The seam without ``bambuddy_url``: nothing to mirror, nothing to import."""

    @property
    def linked(self) -> bool:
        return False

    async def local_densities(self) -> dict[str, LocalDensity]:
        raise NotLinkedError

    async def spool_assignments(self) -> list[SpoolAssignment]:
        raise NotLinkedError

    async def archive(self, archive_id: int) -> ArchiveFacts | None:
        raise NotLinkedError

    async def archive_cost(self, archive_id: int) -> float | None:
        raise NotLinkedError

    async def download_archive_media(
        self, archive_id: int, what: ArchiveMedia, name: str | None, target: Path, limit: int
    ) -> CopiedMedia:
        raise NotLinkedError

    async def maintenance_tasks(self, printer_id: int) -> list[MaintenanceTask]:
        raise NotLinkedError

    async def project_exists(self, project_id: int) -> bool:
        raise NotLinkedError

    async def ensure_project(self, name: str, parent_id: int | None) -> int:
        raise NotLinkedError


NOT_LINKED = NotLinked()


def choose(
    bambuddy_url: str | None,
    settings: SettingsStore,
    folderless: FolderlessProjects | None = None,
) -> OrderIntegrations:
    """The seam for a running ``bambuddy_url``: Bambuddy's when it is set, else
    :data:`NOT_LINKED`. ``folderless`` is the record that keeps a project create whose
    folder step failed from making a second project when it is tried again (#2263)."""
    if not bambuddy_url:
        return NOT_LINKED
    from scadbuddy.orders.bambuddy import BambuddyIntegrations

    return BambuddyIntegrations(settings, folderless)
