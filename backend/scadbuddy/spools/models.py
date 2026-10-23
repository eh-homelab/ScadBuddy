"""What the spool overlay is made of (plan 2026-10-18 D1).

A tray holds one physical spool, and each spool provider keeps its own record of it. A
spool is told by the AMS tray's ``tray_uuid`` and ``tag_uid``, the RFID fields of each
``ams.ams[].tray[]`` entry and of ``vt_tray`` (Doridian/OpenBambuAPI ``mqtt.md`` at
``cc383a2c``, the ``pushing.pushall`` example; ha-bambulab ``pybambu/models.py``
``AMSTray.tag_uid``/``tray_uuid`` at ``0e027ff1``; both read 2026-10-18). An untagged
spool reports all zeros (16 for ``tag_uid``, 32 for ``tray_uuid``: the same
``mqtt.md`` example, and Bambuddy clears an empty slot to those values,
``backend/app/services/bambu_mqtt.py:343-344`` at maziggy/bambuddy ``0d427510``), which
the cleaners turn into ``None``.

Keys are normalised as Bambuddy normalises them (``backend/app/utils/tag_normalization.py``
at ``0d427510``): hex digits only, upper case, a ``tag_uid`` cut to its last 16 digits
and a ``tray_uuid`` to its first 32. ``tray_uuid`` is the reliable key; ``tag_uid`` is a
fallback, since readers report a tag in different forms (Bambuddy fuzzy-matches it,
``spool_tag_matcher.py:438-497``; this never does, so a tag read in another form makes
no match rather than a wrong one).

A tray's ``state`` is a bitfield (ha-bambulab ``pybambu/const.py:321-328`` at
``0e027ff1``, ``AMSTrayStateFlags``: SPOOL 0x01, METADATA 0x02, MOTION 0x04, STEADY
0x08, RFID 0x10; ``utils.py`` ``ams_tray_spool_loaded`` reads it) that changes as the
AMS feeds and scans, so :func:`tray_fingerprint` leaves it out.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

LinkedBy = Literal["rfid", "manual"]
#: How the overlay found a tray's spool: its tag, a person's link, or the provider's own
#: assignment of an untagged tray.
FoundBy = Literal["rfid", "manual", "provider"]


def _hex(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch in "0123456789abcdefABCDEF").upper()


def _key(text: str) -> str | None:
    return None if not text or set(text) == {"0"} else text


def clean_tag_uid(value: str | None) -> str | None:
    """A ``tag_uid`` as stored: its last 16 hex digits, or ``None`` for none or zeros."""
    return _key(_hex(value)[-16:])


def clean_tray_uuid(value: str | None) -> str | None:
    """A ``tray_uuid`` as stored: its first 32 hex digits, or ``None`` for none or zeros."""
    return _key(_hex(value)[:32])


def tray_fingerprint(material: str | None, colour: str | None) -> str:
    """``material|RRGGBB``: what a person saw in the tray when they linked it. Never the
    tray's ``state``, which changes as the AMS feeds; whether the tray is filled at all is
    a check of its own."""
    return f"{(material or '').strip().upper()}|{_hex(colour)[:6]}"


class SpoolLocation(BaseModel):
    """Where a provider itself says a spool is loaded: Bambuddy's
    ``GET /inventory/assignments`` (`BambuddyClient.spool_assignments`), Spoolman's
    ``Spool.location``. ``printer`` is the provider's own key for the printer
    (``bambuddy:<id>``)."""

    printer: str
    ams_id: int
    tray_id: int
    #: `tray_fingerprint` of what the provider saw in the tray, when it says.
    fingerprint: str | None = None


class SpoolSnapshot(BaseModel):
    """What a provider says of one record; never a provider's raw payload."""

    record_id: str
    material: str | None = None
    #: ``#RRGGBB``.
    colour: str | None = None
    brand: str | None = None
    name: str | None = None
    remaining_g: float | None = None
    label_weight_g: int | None = None
    #: When the provider knows them.
    tray_uuid: str | None = None
    tag_uid: str | None = None
    #: The provider's filament preset id, when it has one.
    preset: str | None = None
    #: The tray the provider says the spool is in.
    location: SpoolLocation | None = None


class SeenTray(BaseModel):
    """One tray as a printer's report shows it."""

    ams_id: int
    tray_id: int
    tray_uuid: str | None = None
    tag_uid: str | None = None
    material: str | None = None
    colour: str | None = None
    tray_info_idx: str | None = None

    @property
    def fingerprint(self) -> str:
        return tray_fingerprint(self.material, self.colour)


class TraySpool(BaseModel):
    """One tray as the dialog's overlay sees it."""

    ams_id: int
    tray_id: int
    physical_spool_id: UUID | None = None
    linked_by: FoundBy | None = None
    #: Provider name -> its record of the spool in this tray.
    records: dict[str, SpoolSnapshot] = Field(default_factory=dict)


class ShelfSpool(BaseModel):
    """A spool no tray holds, as the fresh providers' records know it (#4504): what an
    untagged tray's question offers to link. ``physical_spool_id`` when a record is
    linked to one, its records in the providers' order."""

    physical_spool_id: UUID | None = None
    records: dict[str, SpoolSnapshot] = Field(default_factory=dict)


class Overlay(BaseModel):
    trays: list[TraySpool]
    #: The fresh records no tray holds: not of a tagged spool (a tag is read where the
    #: spool is), not placed in a tray by their provider, and not of a spool a person's
    #: link holds (one this printer's tray no longer matches holds nothing).
    shelf: list[ShelfSpool] = Field(default_factory=list)
    #: Each provider whose records are older than `SPOOL_OVERLAY_STALE`, with when they
    #: were last refreshed (``None``: never). Its records are left out of ``trays``.
    stale: dict[str, datetime | None] = Field(default_factory=dict)


class ProviderRefresh(BaseModel):
    """A provider's last refresh, and its last failure."""

    provider: str
    refreshed_at: datetime | None = None
    failed_at: datetime | None = None
    #: The failure's type only, never its message.
    failure: str | None = None
