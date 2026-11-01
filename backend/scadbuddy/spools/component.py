"""Spools as a component (`core/components.py`): the store, and every discovered spool
provider (`spools/providers.py`). The providers are read only by the refresh on the
``library`` worker (`workflows/spool_refresh.py`), never by a route."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.printers.component import PRINTERS
from scadbuddy.spools.providers import SpoolProvider, TraySource, discover_providers
from scadbuddy.spools.seen import ReportTrays
from scadbuddy.spools.spool_providers import native_use_hook
from scadbuddy.spools.store import SpoolStore


@dataclass
class Spools:
    store: SpoolStore
    providers: list[SpoolProvider]
    #: Every printer's trays from its latest report, for the refresh's ``seen`` half
    #: (`spools/seen.py`, over the printer registry's report store); ``None`` in a
    #: process that runs no refresh (the print worker), which reads providers only.
    trays: TraySource | None = None


SPOOLS: Key[Spools] = Key("spools")


def _build(core: Core, components: Components) -> Spools:
    spools = Spools(
        store=SpoolStore(core.projection.pool),
        providers=discover_providers(core, components),
        trays=ReportTrays(components.get(PRINTERS)),
    )
    # What a native print used comes off ScadBuddy's own spools as the follow reads it
    # settled (#4643); the follow is a core service and imports no feature.
    core.print_follower.on_read.append(native_use_hook(spools))
    return spools


COMPONENT = Component(SPOOLS, build=_build)

SpoolsDep = Annotated[Spools, component_dep(SPOOLS)]
