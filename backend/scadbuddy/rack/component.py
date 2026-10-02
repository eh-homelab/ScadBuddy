"""Rack hotend usage (#836) as a component (`core/components.py`)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.rack.usage import RackUsageStore

RACK_USAGE: Key[RackUsageStore] = Key("rack_usage")


def _build(core: Core, components: Components) -> RackUsageStore:
    return RackUsageStore(core.settings.database_url)


@asynccontextmanager
async def _run(store: RackUsageStore) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await asyncio.to_thread(store.close)


COMPONENT = Component(RACK_USAGE, build=_build, run=_run)

RackUsageDep = Annotated[RackUsageStore, component_dep(RACK_USAGE)]
