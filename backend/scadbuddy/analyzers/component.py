"""Print-analyzer decisions (#284) as a component (`core/components.py`)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from scadbuddy.analyzers.decisions import DecisionStore, PostgresDecisionStore
from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key

#: In Postgres only. ``None`` without a database (until #401 makes one required): the
#: routes that persist answer 503.
DECISIONS: Key[DecisionStore | None] = Key("decisions")


def _build(core: Core, components: Components) -> DecisionStore | None:
    url = core.settings.database_url
    return PostgresDecisionStore(url) if url else None


@asynccontextmanager
async def _run(store: DecisionStore | None) -> AsyncIterator[None]:
    try:
        yield
    finally:
        if store is not None:
            await asyncio.to_thread(store.close)


COMPONENT = Component(DECISIONS, build=_build, run=_run)

OptionalDecisionsDep = Annotated[DecisionStore | None, component_dep(DECISIONS)]
#: The decision store, or a 503 naming what is missing. There is no file fallback.
DecisionsDep = Annotated[
    DecisionStore,
    component_dep(
        DECISIONS,
        required=(
            "analyzer decisions are stored in Postgres, and SCADBUDDY_DATABASE_URL is not set"
        ),
    ),
]
