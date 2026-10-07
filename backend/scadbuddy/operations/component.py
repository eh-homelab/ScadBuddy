"""Operations (#1053) as a component (`core/components.py`): the record, the client and
the queues ``Operation`` starts on, and every feature's kinds.

A feature registers its kinds by exporting ``OPERATION_KINDS``, a ``KindsBuild``, from
its ``scadbuddy/<feature>/operations.py`` (as ``bambuddy/operations.py`` does). They are
found the way components are, built over the core and the components when this one is
built, and a name two features both claim stops the start. Nothing adds a kind later.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated

from fastapi import Depends, status
from temporalio.client import Client

from scadbuddy.api.components import component_dep
from scadbuddy.api.deps import DATABASE_REQUIRED_PROBLEM, transactional_events
from scadbuddy.core.components import Component, Components, Core, Key, feature_exports
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.kinds import (
    KINDS_ATTR,
    KINDS_MODULE,
    OperationKind,
    Queue,
    build_kinds,
)
from scadbuddy.operations.store import OperationStore


@dataclass(frozen=True)
class OperationCommands:
    """What a route that starts an ``Operation`` needs, and what the workers serve: the
    record, the client, each kind's task queue, and the kinds by name."""

    store: OperationStore
    client: Client
    queues: Mapping[Queue, str]
    kinds: Mapping[str, OperationKind]
    search_attributes: bool = False


OPERATIONS: Key[OperationCommands] = Key("operations")


def _build(core: Core, components: Components) -> OperationCommands:
    # Print runs and operations share the `bambuddy` queue and the API's lazy client;
    # the library kinds run on the `library` worker (#1054).
    runs = core.print_runs
    return OperationCommands(
        store=OperationStore(core.projection.pool, events=transactional_events(core.events)),
        client=runs.client,
        queues=MappingProxyType(
            {"bambuddy": runs.task_queue, "library": core.settings.temporal_task_queue_library}
        ),
        kinds=MappingProxyType(
            build_kinds(core, components, feature_exports(KINDS_MODULE, KINDS_ATTR))
        ),
        search_attributes=runs.search_attributes,
    )


COMPONENT = Component(OPERATIONS, build=_build)


def require_operations(
    ops: Annotated[OperationCommands, component_dep(OPERATIONS)],
) -> OperationCommands:
    """The operations, or a 503 naming what is missing: they live only in Postgres."""
    if not ops.store.available:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "operations are stored in Postgres, and SCADBUDDY_DATABASE_URL is not set",
            type_=DATABASE_REQUIRED_PROBLEM,
        )
    return ops


OperationsDep = Annotated[OperationCommands, Depends(require_operations)]
