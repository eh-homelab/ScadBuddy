"""Flow triggers (#2484) as a component (`core/components.py`): their record, the flows
they start, and the client their Schedules are kept on (the API's own, lazy, so the API
boots while Temporal is down). The fires run on the `library` worker (`main.py`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from temporalio.client import Client

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.flow_triggers.store import TriggerStore
from scadbuddy.flows.component import FLOWS, Flows


@dataclass
class Triggers:
    store: TriggerStore
    flows: Flows
    client: Client
    #: The `library` queue: the Schedules' fires run there.
    task_queue: str


FLOW_TRIGGERS: Key[Triggers] = Key("flow_triggers")


def _build(core: Core, components: Components) -> Triggers:
    return Triggers(
        store=TriggerStore(core.projection.pool),
        flows=components.get(FLOWS),
        client=core.print_runs.client,
        task_queue=core.settings.temporal_task_queue_library,
    )


COMPONENT = Component(FLOW_TRIGGERS, build=_build)

TriggersDep = Annotated[Triggers, component_dep(FLOW_TRIGGERS)]
