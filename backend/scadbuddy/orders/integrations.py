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

from typing import Protocol

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


class NotLinked:
    """The seam without ``bambuddy_url``: nothing to mirror, nothing to import."""

    @property
    def linked(self) -> bool:
        return False

    async def spool_groups(self) -> list[SpoolGroup]:
        raise NotLinkedError


NOT_LINKED = NotLinked()


def choose(bambuddy_url: str | None, settings: SettingsStore) -> OrderIntegrations:
    """The seam for a running ``bambuddy_url``: Bambuddy's when it is set, else
    :data:`NOT_LINKED`."""
    if not bambuddy_url:
        return NOT_LINKED
    from scadbuddy.orders.bambuddy import BambuddyIntegrations

    return BambuddyIntegrations(settings)
