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
from typing import Literal, Protocol

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.orders.problems import PROBLEMS


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
    #: ``(spool id, cost_per_kg)`` of each spool, by id; a spool with no price has
    #: ``None``. A higher id is a spool Bambuddy recorded later.
    spools: list[tuple[int, float | None]] = Field(default_factory=list)


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


#: What of an archive an attempt can copy into its attachments (§8.2).
ArchiveMedia = Literal["finished_photo", "timelapse"]


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

    async def archive(self, archive_id: int) -> ArchiveFacts | None:
        """One archive's facts (§8.2); None for one Bambuddy deleted.
        :class:`NotLinkedError` without a Bambuddy."""
        ...

    async def download_archive_media(
        self, archive_id: int, what: ArchiveMedia, name: str | None, target: Path, limit: int
    ) -> tuple[str, int]:
        """The archive's ``finish_`` photo ``name`` or its timelapse written to
        ``target``, through Bambuddy's media routes; its sha256 and size.
        :class:`MediaTooLargeError` past ``limit`` bytes, with ``target`` removed."""
        ...


class NotLinked:
    """The seam without ``bambuddy_url``: nothing to mirror, nothing to import."""

    @property
    def linked(self) -> bool:
        return False

    async def spool_groups(self) -> list[SpoolGroup]:
        raise NotLinkedError

    async def archive(self, archive_id: int) -> ArchiveFacts | None:
        raise NotLinkedError

    async def download_archive_media(
        self, archive_id: int, what: ArchiveMedia, name: str | None, target: Path, limit: int
    ) -> tuple[str, int]:
        raise NotLinkedError


NOT_LINKED = NotLinked()


def choose(bambuddy_url: str | None, settings: SettingsStore) -> OrderIntegrations:
    """The seam for a running ``bambuddy_url``: Bambuddy's when it is set, else
    :data:`NOT_LINKED`."""
    if not bambuddy_url:
        return NOT_LINKED
    from scadbuddy.orders.bambuddy import BambuddyIntegrations

    return BambuddyIntegrations(settings)
