"""Tasks (#7364) as a component (`core/components.py`): the typed questions flows ask.

Building it does no I/O: the shared rows pool (`rows/component.py`, #5226) opens on first
use, as it does for orders, printers and entities. `ROWS` closes it."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.api.components import component_dep
from scadbuddy.api.deps import transactional_events
from scadbuddy.bambuddy.runs import TransactionalEvents
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.rows.component import ROWS
from scadbuddy.tasks.models import Task
from scadbuddy.tasks.store import TaskStore


class TasksComponent:
    def __init__(
        self, pool: ConnectionPool[Connection[DictRow]], events: TransactionalEvents
    ) -> None:
        self._pool = pool
        self._store = TaskStore(pool, events=events)

    @property
    def store(self) -> TaskStore:
        """The store, its pool opened on first use (an open pool ignores it)."""
        self._pool.open(wait=False)
        return self._store

    async def mark_answered(
        self, task_id: str, answer: dict[str, Any], by: str | None, workflow_run_id: str
    ) -> Task:
        """`TaskStore.mark_answered`, as `RemoteTasks` has it, for the in-process worker."""
        return await asyncio.to_thread(
            self.store.mark_answered, task_id, answer, by, workflow_run_id
        )


TASKS: Key[TasksComponent] = Key("tasks")


def _build(core: Core, components: Components) -> TasksComponent:
    return TasksComponent(components.get(ROWS).pool, transactional_events(core.events))


COMPONENT = Component(TASKS, build=_build)

TasksDep = Annotated[TasksComponent, component_dep(TASKS)]
