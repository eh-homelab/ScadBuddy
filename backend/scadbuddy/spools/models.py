"""What the spool overlay is made of (plan 2026-10-18 D1).

A tray holds one physical spool, and each spool provider keeps its own record of it. A
spool is told by the AMS tray's ``tray_uuid`` and ``tag_uid``, the RFID fields of each
``ams.ams[].tray[]`` entry and of ``vt_tray`` (Doridian/OpenBambuAPI ``mqtt.md`` at
``cc383a2c``, the ``pushing.pushall`` example; ha-bambulab ``pybambu/models.py``
``AMSTray.tag_uid``/``tray_uuid`` at ``0e027ff1``; both read 2026-10-18). An untagged
spool reports all zeros (16 for ``tag_uid``, 32 for ``tray_uuid``: the same
``mqtt.md`` example, and Bambuddy clears an empty slot to those values,
``backend/app/services/bambu_mqtt.py:343-344`` at maziggy/bambuddy ``0d427510``), which
:func:`clean_key` turns into ``None``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

LinkedBy = Literal["rfid", "manual"]


def clean_key(value: str | None) -> str | None:
    """An RFID key as stored: upper-case hex, or ``None`` for blank or all zeros."""
    text = (value or "").strip().upper()
    if not text or set(text) == {"0"}:
        return None
    return text


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


class SeenTray(BaseModel):
    """One tray as a printer's report shows it."""

    ams_id: int
    tray_id: int
    tray_uuid: str | None = None
    tag_uid: str | None = None
    material: str | None = None
    colour: str | None = None
    tray_info_idx: str | None = None
    #: ``material|colour|state`` (`bambuddy/trays.py` `unknown_trays`).
    fingerprint: str


class TraySpool(BaseModel):
    """One tray as the dialog's overlay sees it."""

    ams_id: int
    tray_id: int
    physical_spool_id: UUID | None = None
    linked_by: LinkedBy | None = None
    #: Provider name -> its record of the spool in this tray.
    records: dict[str, SpoolSnapshot] = Field(default_factory=dict)


class Overlay(BaseModel):
    trays: list[TraySpool]
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
