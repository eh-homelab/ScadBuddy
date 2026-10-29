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
from dataclasses import dataclass
from types import ModuleType
from typing import Protocol

import scadbuddy
from scadbuddy.core.components import Components, feature_exports
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.printers.models import PrinterRow
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

    def printer_key(self, row: PrinterRow) -> str | None:
        """The registry printer ``row`` as the provider names it in a record's
        `SpoolLocation.printer` (``bambuddy:<id>``), or ``None`` when the provider does
        not know it. No I/O: the print dialog calls it."""
        ...


class TraySource(Protocol):
    """Every printer's trays as its latest report shows them: the refresh's ``seen``
    half. The printer registry's report store provides it (plan 2026-10-16 Task 6)."""

    async def latest(self) -> list[SeenTray]: ...


class ProviderCore(Protocol):
    """What a provider's build may read of the core services: `Core` is one, and so is
    :class:`SettingsOnly`, so the print worker, which has no `Core`, finds the same
    providers (#4551: a run's plan reads the overlay there)."""

    @property
    def settings_store(self) -> SettingsStore: ...


@dataclass(frozen=True)
class SettingsOnly:
    """The :class:`ProviderCore` of a process with no `Core` (the print worker)."""

    settings_store: SettingsStore


#: Builds a feature's provider over the core services and the components, which are
#: ``None`` in a process with no registry (the print worker): a provider that needs a
#: component returns ``None`` there. ``None`` when the feature has none.
SpoolProviderBuild = Callable[[ProviderCore, Components | None], SpoolProvider | None]


class DuplicateProviderError(ValueError):
    pass


def discover_providers(
    core: ProviderCore, components: Components | None, package: ModuleType = scadbuddy
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
