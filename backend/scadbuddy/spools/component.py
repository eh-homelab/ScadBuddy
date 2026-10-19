"""Spools as a component (`core/components.py`): the store, and every discovered spool
provider (`spools/providers.py`). The providers are read only by the refresh on the
``library`` worker (`workflows/spool_refresh.py`), never by a route."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.spools.providers import SpoolProvider, TraySource, discover_providers
from scadbuddy.spools.store import SpoolStore


@dataclass
class Spools:
    store: SpoolStore
    providers: list[SpoolProvider]
    #: Every printer's trays from its latest report, for the refresh's ``seen`` half;
    #: ``None`` until the printer registry's report store is on main (plan 2026-10-16
    #: Task 6), and the refresh then reads providers only.
    trays: TraySource | None = None


SPOOLS: Key[Spools] = Key("spools")


def _build(core: Core, components: Components) -> Spools:
    return Spools(
        store=SpoolStore(core.projection.pool),
        providers=discover_providers(core, components),
    )


COMPONENT = Component(SPOOLS, build=_build)

SpoolsDep = Annotated[Spools, component_dep(SPOOLS)]
