"""3dfilamentprofiles.com (3DFP) search, and the create of a filament it lacks (#7025, plan
``docs/superpowers/plans/2026-11-22-3dfp-search-add.md``).

Both run on the profile-fetch worker, the one process that reaches the browser service with
the 3DFP login (#6625, #6780), as `TdfpSearch` and `TdfpCreate` on its queue. The API starts
the workflow and waits for its outcome (`wire.TdfpOutcome`); the worker calls no CRUD route
for them, and ScadBuddy stores nothing of a search. A create runs once per caller, request
key, type and ask (`create_workflow_id`, R-7025-12): a request sent again under its key
answers the first one's outcome, kept by Temporal under the closed workflow's id
(``ALLOW_DUPLICATE_FAILED_ONLY``, for the namespace's retention), and the activity is never
retried (R-7025-7). A create that failed before "Save Filament" fails its workflow
(`workflow.BEFORE_SAVE`), so the same key runs it again (R-7025-11). The route then sets the
new id as the type's ``tdfp_id``; the identifiers the create sends are stored nowhere
(R-7025-6).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Final, Protocol
from uuid import uuid4

from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.filament.profile_fetch import TemporalQueueProbe
from scadbuddy.profile_fetch.wire import (
    CREATE_WORKFLOW,
    SEARCH_WORKFLOW,
    CreateAsk,
    SearchAsk,
    TdfpOutcome,
)
from scadbuddy.profile_fetch.workflow import (
    CREATE_RETRY,
    CREATE_SCHEDULE_TO_START,
    CREATE_TIMEOUT,
    SEARCH_RETRY,
    SEARCH_SCHEDULE_TO_START,
    SEARCH_TIMEOUT,
    TDFP_EXECUTION_TIMEOUT,
    step_wait,
)

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

#: How long the route waits for a search's outcome: every attempt's wait in the queue and
#: run, and a margin (`workflow.step_wait`, R-7025-10). A search the route stops waiting
#: for ends by its own timeouts.
SEARCH_WAIT: Final = step_wait(SEARCH_SCHEDULE_TO_START, SEARCH_TIMEOUT, SEARCH_RETRY)
#: The same for a create. A create the route stops waiting for still runs: the same request
#: sent again under its key waits for it.
CREATE_WAIT: Final = step_wait(CREATE_SCHEDULE_TO_START, CREATE_TIMEOUT, CREATE_RETRY)
#: A create's workflow in these states ran nothing that made a filament: its key may run it
#: again.
_RETRYABLE: Final = frozenset(
    {
        WorkflowExecutionStatus.FAILED,
        WorkflowExecutionStatus.CANCELED,
        WorkflowExecutionStatus.TERMINATED,
        WorkflowExecutionStatus.TIMED_OUT,
    }
)


def create_workflow_id(key: str, ask: CreateAsk) -> str:
    """One create per request key and ask; ``key`` is the caller's, with the type the new id
    goes to (the route's): the same key with another ask, or for another type, is another
    create (R-7025-12)."""
    digest = hashlib.sha256(f"{key}\x1f{ask.model_dump_json()}".encode()).hexdigest()
    return f"tdfp-create:{digest[:40]}"


class Tdfp(Protocol):
    """What the routes run (`TemporalTdfp`; a test's fake)."""

    async def has_worker(self) -> bool: ...

    async def search(self, ask: SearchAsk, *, wait: float) -> TdfpOutcome: ...

    async def started(self, ask: CreateAsk, *, key: str) -> bool: ...

    async def create(self, ask: CreateAsk, *, key: str, wait: float) -> TdfpOutcome: ...


class TemporalTdfp:
    """`SEARCH_WORKFLOW` and `CREATE_WORKFLOW` on the profile-fetch worker's queue."""

    def __init__(self, client: Client, task_queue: str) -> None:
        self.client = client
        self.task_queue = task_queue

    async def has_worker(self) -> bool:
        """Whether a worker polls the queue (`profile_fetch.TemporalQueueProbe`)."""
        return await TemporalQueueProbe(self.client, self.task_queue).has_pollers()

    async def search(self, ask: SearchAsk, *, wait: float) -> TdfpOutcome:
        handle = await self.client.start_workflow(
            SEARCH_WORKFLOW,
            ask,
            id=f"tdfp-search:{uuid4().hex}",
            task_queue=self.task_queue,
            execution_timeout=TDFP_EXECUTION_TIMEOUT,
            result_type=TdfpOutcome,
        )
        return await _outcome(handle, wait)

    async def started(self, ask: CreateAsk, *, key: str) -> bool:
        """Whether a create of this key and ask runs, or ended in a way that may have made a
        filament; one that failed before its save does not count. Any other `RPCError`
        than NOT_FOUND is raised, and the route reads it as every route does (R-7025-15)."""
        try:
            described = await self.client.get_workflow_handle(
                create_workflow_id(key, ask)
            ).describe()
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                return False
            raise
        return described.status not in _RETRYABLE

    async def create(self, ask: CreateAsk, *, key: str, wait: float) -> TdfpOutcome:
        workflow_id = create_workflow_id(key, ask)
        try:
            handle = await self.client.start_workflow(
                CREATE_WORKFLOW,
                ask,
                id=workflow_id,
                task_queue=self.task_queue,
                execution_timeout=TDFP_EXECUTION_TIMEOUT,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                result_type=TdfpOutcome,
            )
        except WorkflowAlreadyStartedError:
            # It completed: its outcome, never a second create.
            handle = self.client.get_workflow_handle(workflow_id, result_type=TdfpOutcome)
        return await _outcome(handle, wait)


async def _outcome(handle: WorkflowHandle[object, TdfpOutcome], wait: float) -> TdfpOutcome:
    """The workflow's outcome; a create that failed before its save (its workflow failed
    with an `ApplicationError` of the failure's kind) as that kind and reason."""
    try:
        async with asyncio.timeout(wait):
            return await handle.result()
    except WorkflowFailureError as failure:
        cause = failure.cause
        if isinstance(cause, ApplicationError):
            return TdfpOutcome(kind=cause.type or "step_failed", reason=cause.message)
        return TdfpOutcome(kind="step_failed", reason="the 3DFP step failed")


def tdfp(state: AppState) -> Tdfp:
    """The app's runner: the operations' client, on the profile-fetch worker's queue."""
    from scadbuddy.operations.component import OPERATIONS

    ops = state.components.get(OPERATIONS)
    return TemporalTdfp(ops.client, state.settings.temporal_task_queue_profile_fetch)
