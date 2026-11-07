"""Print-analyzer decisions (#284) as a component (`core/components.py`)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from scadbuddy.analyzers.decisions import DecisionStore, PostgresDecisionStore
from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.rows.component import ROWS

#: In Postgres only.
DECISIONS: Key[DecisionStore] = Key("decisions")


def _build(core: Core, components: Components) -> DecisionStore:
    # On the API's shared rows pool (#5226), which `ROWS` closes after this one's run.
    return PostgresDecisionStore(core.settings.database_url, pool=components.get(ROWS).pool)


@asynccontextmanager
async def _run(store: DecisionStore) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await asyncio.to_thread(store.close)


COMPONENT = Component(DECISIONS, build=_build, run=_run)

DecisionsDep = Annotated[DecisionStore, component_dep(DECISIONS)]
