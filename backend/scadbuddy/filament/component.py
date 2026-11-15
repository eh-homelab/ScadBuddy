"""Filament as a component (`core/components.py`): ScadBuddy's own filament inventory
(#6261, epic #6260). Every store is built over the settings store's pool, as the native
spool provider's is."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore


@dataclass
class Filament:
    catalog: CatalogStore
    types: SpoolTypeStore
    spools: FilamentSpoolStore
    locations: LocationStore


FILAMENT: Key[Filament] = Key("filament")


def _build(core: Core, components: Components) -> Filament:
    pool = core.settings_store.pool
    return Filament(
        catalog=CatalogStore(pool),
        types=SpoolTypeStore(pool),
        spools=FilamentSpoolStore(pool),
        locations=LocationStore(pool),
    )


COMPONENT = Component(FILAMENT, build=_build)

FilamentDep = Annotated[Filament, component_dep(FILAMENT)]
