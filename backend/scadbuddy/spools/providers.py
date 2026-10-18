"""The spool provider interface (plan 2026-10-18 D1).

A feature that provides spools exports ``SPOOL_PROVIDERS``, a list of
:data:`SpoolProviderBuild`, from its ``scadbuddy/<feature>/spool_providers.py``;
:func:`discover_providers` finds them as `core.components.feature_exports` finds every
feature export, so no list is written by hand. Bambuddy's inventory is the first
(`bambuddy/spool_providers.py`). A provider is read only by the refresh
(`workflows/spool_refresh.py`), never by the print dialog.
"""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import Protocol

import scadbuddy
from scadbuddy.core.components import Components, Core, feature_exports
from scadbuddy.spools.models import SeenTray, SpoolSnapshot

PROVIDER_MODULE = "spool_providers"
PROVIDER_ATTR = "SPOOL_PROVIDERS"


class SpoolProvider(Protocol):
    #: The stable key, stored as ``spool_provider_records.provider``.
    name: str
    #: What a person reads in the overlay's note: "Bambuddy".
    label: str
    #: Lower first: the order the overlay prefers providers' records in (a tray's
    #: remaining weight is the first fresh record's), and the order they are refreshed.
    priority: int

    async def configured(self) -> bool:
        """False: the refresh skips it and leaves its rows as they are."""
        ...

    async def records(self) -> list[SpoolSnapshot]:
        """The whole inventory; read only by the refresh."""
        ...


class TraySource(Protocol):
    """Every printer's trays as its latest report shows them: the refresh's ``seen``
    half. The printer registry's report store provides it (plan 2026-10-16 Task 6)."""

    async def latest(self) -> list[SeenTray]: ...


#: Builds a feature's provider over the core services; ``None`` when it has none.
SpoolProviderBuild = Callable[[Core, Components], SpoolProvider | None]


class DuplicateProviderError(ValueError):
    pass


def discover_providers(
    core: Core, components: Components, package: ModuleType = scadbuddy
) -> list[SpoolProvider]:
    """Every feature's providers by `SpoolProvider.priority`, then name; two named alike
    are refused."""
    found: list[SpoolProvider] = []
    names: set[str] = set()
    for builds in feature_exports(PROVIDER_MODULE, PROVIDER_ATTR, package):
        for build in builds:
            provider = build(core, components)
            if provider is None:
                continue
            if provider.name in names:
                raise DuplicateProviderError(
                    f"two spool providers are registered as {provider.name!r}"
                )
            names.add(provider.name)
            found.append(provider)
    return sorted(found, key=lambda provider: (provider.priority, provider.name))
