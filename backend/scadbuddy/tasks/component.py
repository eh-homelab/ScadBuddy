"""Tasks (#7364) as a component (`core/components.py`): the typed questions flows ask."""

from __future__ import annotations

from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.api.deps import transactional_events
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.rows.component import ROWS
from scadbuddy.tasks.store import TaskStore

TASKS: Key[TaskStore] = Key("tasks")


def _build(core: Core, components: Components) -> TaskStore:
    # On the API's shared rows pool (#5226), which `ROWS` opens on first use and closes.
    pool = components.get(ROWS).pool
    pool.open(wait=False)
    return TaskStore(pool, events=transactional_events(core.events))


COMPONENT = Component(TASKS, build=_build)

TasksDep = Annotated[TaskStore, component_dep(TASKS)]
