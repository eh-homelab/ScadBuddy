"""What a worker registers, told to its workflows (#4139): an ``Operation`` defers a kind
whose activities its build lacks (`operation.defer_unregistered`) rather than schedule
one the build would fail as unregistered. Only temporalio is imported here, so the
sandboxed modules that build workers (`housekeeping.library_worker`) may import it."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from temporalio import activity
from temporalio.worker import Interceptor, WorkflowInboundInterceptor, WorkflowInterceptorClassInput

from scadbuddy.workflows.operation_models import REGISTERED_ACTIVITIES


class RegisteredActivities(Interceptor):
    """Gives each workflow on the worker the extern function `REGISTERED_ACTIVITIES`:
    the names of ``activities``, the worker's own. Per worker, not per process: a test
    runs an old and a new build side by side in one process."""

    def __init__(self, activities: Iterable[Callable[..., Any]]) -> None:
        names = {activity._Definition.must_from_callable(fn).name for fn in activities}
        self.names = frozenset(name for name in names if name is not None)

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        names = self.names
        input.unsafe_extern_functions[REGISTERED_ACTIVITIES] = lambda: names
        return None
