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
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.orders.models import FilamentEffect
from scadbuddy.orders.problems import PROBLEMS

if TYPE_CHECKING:
    from scadbuddy.bambuddy.projects import FolderlessProjects


class LocalDensity(BaseModel):
    """A Bambuddy local preset's name and ``filament_density``."""

    name: str
    density_g_cm3: str


class SpoolGroup(BaseModel):
    """One filament of the spool inventory (§8.3): every unarchived spool of the same
    material, subtype, brand and colour, as ``bambuddy/farm.py`` ``_filament_key`` groups
    them, in orders' own shape."""

    material: str
    subtype: str | None = None
    brand: str | None = None
    colour_name: str | None = None
    #: ``#RRGGBB`` from the spools' ``rgba``; ``None`` for a group known only by its
    #: colour's name, which a record cannot carry as its colour.
    colour: str | None = None
    #: The newest spool's ``rgba`` alpha byte (255 for a 6-digit ``rgba``), its
    #: ``extra_colors`` as ``#RRGGBB``/``#RRGGBBAA`` stops and its ``effect_type`` (#6209).
    alpha: int = 255
    extra_colours: list[str] = Field(default_factory=list)
    effect: FilamentEffect | None = None
    #: ``(spool id, cost_per_kg)`` of each spool, by id; a spool with no price has
    #: ``None``. A higher id is a spool Bambuddy recorded later.
    spools: list[tuple[int, float | None]] = Field(default_factory=list)
    #: What gives a record its density (#6136, `orders/density.py` `resolve`):
    #: the most recent spool's ``slicer_filament`` that names one (``GFA00``, or a
    #: numeric local preset id); ``None`` when no spool names one.
    slicer_filament: str | None = None
    #: The ``setting_id`` of each K-profile of the group's spools, newest spool first.
    k_profile_settings: list[str] = Field(default_factory=list)
    #: The ``tray_info_idx`` of the AMS tray a spool of the group is loaded in, read
    #: only for the spools that name neither of the above; the group uses it only
    #: after its own preset ids.
    tray_info_idx: str | None = None
    #: The Bambuddy local preset ``slicer_filament`` names, when it is one with a density.
    local_preset: LocalDensity | None = None
    #: Bambuddy's catalogue ``density`` for the group's material, brand and colour,
    #: when its entries of them whose ``name`` holds each word of the subtype give
    #: exactly one.
    catalogue_density: str | None = None


class ShelfSpool(BaseModel):
    """One spool of the inventory and where it is loaded (§5.4 "Spools", #4251), in
    orders' own shape."""

    id: int
    material: str
    subtype: str | None = None
    brand: str | None = None
    #: ``#RRGGBB`` from its ``rgba``; None for one known only by its colour's name, which
    #: is never taken for a record's colour.
    colour: str | None = None
    remaining_g: float = 0.0
    archived: bool = False
    #: The Bambuddy printer it is loaded in; None on the shelf.
    loaded_printer_id: int | None = None


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
        "Bambuddy is not linked: set its URL in Settings to import spools",
        type_=PROBLEMS + "not_linked",
    )


class OrderIntegrations(Protocol):
    #: Whether a Bambuddy is linked; the price book answers it as ``spool_import``.
    @property
    def linked(self) -> bool: ...

    async def spool_groups(self) -> list[SpoolGroup]:
        """The spool inventory grouped by filament (§8.3); :class:`NotLinkedError`
        without a Bambuddy."""
        ...

    async def spool_inventory(self) -> list[ShelfSpool]:
        """Every spool and the printer it is loaded in (§5.4 "Spools");
        :class:`NotLinkedError` without a Bambuddy."""
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

    async def spool_groups(self) -> list[SpoolGroup]:
        raise NotLinkedError

    async def spool_inventory(self) -> list[ShelfSpool]:
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
