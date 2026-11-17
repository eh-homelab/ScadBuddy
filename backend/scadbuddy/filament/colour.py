"""Filament colours: ``#RRGGBBAA``, upper case, as the ``filament_*`` tables store them
(their ``rgba ~ '^#[0-9A-F]{8}$'`` checks)."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator

_HEX = re.compile(r"#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?")


def normalise_rgba(value: str) -> str:
    """``RRGGBB`` or ``RRGGBBAA``, with or without ``#``, any case, as ``#RRGGBBAA``.
    Six digits are opaque (``FF``)."""
    match = _HEX.fullmatch(value)
    if match is None:
        raise ValueError("a colour is RRGGBB or RRGGBBAA hex digits, with or without #")
    return f"#{match.group(1)}{match.group(2) or 'FF'}".upper()


def is_translucent(rgba: str) -> bool:
    """True when the alpha byte is below ``FF``."""
    return int(normalise_rgba(rgba)[7:9], 16) < 0xFF


def same_opacity(left: str | None, right: str | None) -> bool:
    """Whether two colours are both opaque or both translucent (spec §5.1, plan #6262
    Decision 12): a translucent spool never matches an opaque one by colour alone.
    ``#RRGGBB`` and ``None`` read as opaque. Mirrored by ``sameOpacity`` in
    ``frontend/src/lib/rgba.ts``."""
    return _translucent(left) == _translucent(right)


def _translucent(colour: str | None) -> bool:
    return colour is not None and is_translucent(colour)


Rgba = Annotated[str, AfterValidator(normalise_rgba)]
