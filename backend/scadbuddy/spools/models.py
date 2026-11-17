"""A printer's trays as its report shows them, and the cleaners of their RFID keys (plan
2026-10-18 D1; what is left of the spool overlay, which #6262 Task 12 removed). The SB
inventory reads them (`filament/inventory.py`, `filament/spools.py`).

A spool is told by the AMS tray's ``tray_uuid`` and ``tag_uid``, the RFID fields of each
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

from pydantic import BaseModel


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
    """``material|RRGGBB``: what the tray shows, so the native dialog remembers a "no" to
    its question only until it changes (`bambuddy/trays.py` `tray_questions`). Never the
    tray's ``state``, which changes as the AMS feeds; whether the tray is filled at all is
    a check of its own. The SB inventory's own, alpha kept, is `filament/inventory.py`
    `tray_fingerprint`."""
    return f"{(material or '').strip().upper()}|{_hex(colour)[:6]}"


class SeenTray(BaseModel):
    """One tray as a printer's report shows it."""

    ams_id: int
    tray_id: int
    tray_uuid: str | None = None
    tag_uid: str | None = None
    material: str | None = None
    colour: str | None = None
    tray_info_idx: str | None = None
    #: The tray's ``tray_color`` as ``#RRGGBBAA``, alpha kept (#6262, spec §5.1), for
    #: the SB tray fingerprint (`filament/inventory.py`); ``colour`` stays ``#RRGGBB``
    #: for the slicer and the colour match.
    rgba: str | None = None

    @property
    def fingerprint(self) -> str:
        return tray_fingerprint(self.material, self.colour)
