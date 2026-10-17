"""Batch print state (#2672) as a component (`core/components.py`)."""

from __future__ import annotations

from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.batches.store import PrintBatchStore
from scadbuddy.core.components import Component, Components, Core, Key

PRINT_BATCHES: Key[PrintBatchStore] = Key("print_batches")


def _build(core: Core, components: Components) -> PrintBatchStore:
    return PrintBatchStore(core.projection.pool)


COMPONENT = Component(PRINT_BATCHES, build=_build)

PrintBatchesDep = Annotated[PrintBatchStore, component_dep(PRINT_BATCHES)]
