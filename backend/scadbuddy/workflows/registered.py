"""What a worker registers, told to its workflows (#4139): an ``Operation`` defers a kind
whose activities its build lacks (`operation.defer_unregistered`) rather than schedule
one the build would fail as unregistered, and a ``ProjectWorkflow`` a host function its
build lacks (#4146). Only temporalio is imported here, so the sandboxed modules that
build workers (`housekeeping.library_worker`) may import it."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from temporalio import activity
from temporalio.worker import Interceptor, WorkflowInboundInterceptor, WorkflowInterceptorClassInput

from scadbuddy.workflows.operation_models import REGISTERED_ACTIVITIES

#: The extern function the ``projects`` worker gives its workflows: the names of the host
#: functions a flow script may call on this build (#4146).
REGISTERED_HOST_FUNCTIONS = "scadbuddy_registered_host_functions"


class RegisteredActivities(Interceptor):
    """Gives each workflow on the worker the extern function `REGISTERED_ACTIVITIES`:
    the names of ``activities``, the worker's own; and, given ``host_functions`` (the
    ``projects`` worker), `REGISTERED_HOST_FUNCTIONS`. Per worker, not per process: a
    test runs an old and a new build side by side in one process. A dynamic activity
    takes every name, so a worker with one is told nothing and defers nothing (#4157).
    The names come from temporalio's private ``_Definition`` (1.x), which
    ``tests/test_operation_deferral.py`` pins."""

    def __init__(
        self,
        activities: Iterable[Callable[..., Any]],
        host_functions: Iterable[str] | None = None,
    ) -> None:
        names = [activity._Definition.must_from_callable(fn).name for fn in activities]
        named = frozenset(n for n in names if n is not None)
        #: None for a worker with a dynamic activity (a None name): it registers every kind.
        self.names = None if len(named) < len(set(names)) else named
        self.host_functions = None if host_functions is None else frozenset(host_functions)

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        names, host_functions = self.names, self.host_functions
        if names is not None:
            input.unsafe_extern_functions[REGISTERED_ACTIVITIES] = lambda: names
        if host_functions is not None:
            input.unsafe_extern_functions[REGISTERED_HOST_FUNCTIONS] = lambda: host_functions
        return None
