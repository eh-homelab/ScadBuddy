"""Time outside dry storage (#6268, plan Decision 8): the hours since a spool was last
dried (else since it was made) that its place was not a dry-storage one. A spool with no
place counts as outside. The places come from the spool's ``moved`` events, so a move
between two dry boxes adds nothing. A place counts as dry by its CURRENT kind for the
whole window: no kind history is kept, so re-kinding a shelf to a dry box turns the time
already spent there into dry time. The functions take every time as an argument."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import NamedTuple
from uuid import UUID


class Move(NamedTuple):
    """One ``moved`` event: when, and the places it left and entered (``None``: none)."""

    at: datetime
    origin: UUID | None
    target: UUID | None


def outside_dry(
    since: datetime,
    moves: Sequence[Move],
    dry: Collection[UUID],
    current: UUID | None,
    now: datetime,
) -> float:
    """The hours from ``since`` to ``now`` spent outside the ``dry`` places. ``moves`` are
    the spool's moves after ``since``, oldest first; ``current`` is where it is now. Its
    place at ``since`` is the first move's origin, else ``current``."""
    place = moves[0].origin if moves else current
    start = since
    seconds = 0.0
    for move in moves:
        if place is None or place not in dry:
            seconds += max((move.at - start).total_seconds(), 0.0)
        place, start = move.target, max(move.at, start)
    if place is None or place not in dry:
        seconds += max((now - start).total_seconds(), 0.0)
    return seconds / 3600


def needs_drying(hours: float, *, hygroscopic: bool, max_hours_outside_dry: float | None) -> bool:
    return hygroscopic and max_hours_outside_dry is not None and hours > max_hours_outside_dry
