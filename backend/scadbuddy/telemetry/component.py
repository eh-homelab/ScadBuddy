"""The browser trace relay (spec 2026-10-01 §5.2) as a component (`core/components.py`).

Its forwarding task runs for as long as the app does (`Component.run`); on the way down
it stops accepting and drains, within the lifespan, inside the pod's grace period.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.settings import Settings
from scadbuddy.core.tracing import otlp_traces_target
from scadbuddy.telemetry.admission import RelayLimits
from scadbuddy.telemetry.forwarder import TraceForwarder


@dataclass(frozen=True)
class TraceRelay:
    forwarder: TraceForwarder
    limits: RelayLimits
    #: The settings in effect: ``public_url`` is live (#322), applied to the app's
    #: ``settings`` on every change, so it is read per request rather than held.
    settings: Callable[[], Settings]


TRACE_RELAY: Key[TraceRelay] = Key("trace_relay")


def _build(core: Core, components: Components) -> TraceRelay:
    return TraceRelay(
        forwarder=TraceForwarder(metrics=core.metrics, target=otlp_traces_target()),
        limits=RelayLimits(),
        settings=lambda: core.settings,
    )


def _run(relay: TraceRelay) -> AbstractAsyncContextManager[None]:
    return relay.forwarder.running()


COMPONENT = Component(TRACE_RELAY, build=_build, run=_run)

TraceRelayDep = Annotated[TraceRelay, component_dep(TRACE_RELAY)]
