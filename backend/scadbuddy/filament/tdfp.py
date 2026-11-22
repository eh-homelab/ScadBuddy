"""3dfilamentprofiles.com (3DFP) search, and the create of a filament it lacks (#7025, plan
``docs/superpowers/plans/2026-11-22-3dfp-search-add.md``).

Both run on the profile-fetch worker, the one process that reaches the browser service with
the 3DFP login (#6625, #6780), as `TdfpSearch` and `TdfpCreate` on its queue. The API starts
the workflow and waits for its outcome (`wire.TdfpOutcome`); the worker calls no CRUD route
for them, and ScadBuddy stores nothing of a search. A create runs once per caller, request
key and ask (`create_workflow_id`): a request sent again under its key answers the first
one's outcome (Temporal keeps a closed workflow's id, ``REJECT_DUPLICATE``, for the
namespace's retention), and the activity is never retried (R-7025-7). The route then sets
the new id as the type's ``tdfp_id`` (`set_type_tdfp_id`); the identifiers the create sends
are stored nowhere (R-7025-6).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Final, Protocol
from uuid import uuid4

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
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
    CREATE_SCHEDULE_TO_START,
    CREATE_TIMEOUT,
    SEARCH_SCHEDULE_TO_START,
    SEARCH_TIMEOUT,
    TDFP_EXECUTION_TIMEOUT,
)

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

#: How long the route waits for a search's outcome: its wait in the queue and its step,
#: plus a margin. A search the route stops waiting for ends by its own timeouts.
SEARCH_WAIT: Final = (SEARCH_SCHEDULE_TO_START + SEARCH_TIMEOUT).total_seconds() + 30
#: The same for a create. A create the route stops waiting for still runs: the same request
#: sent again under its key waits for it.
CREATE_WAIT: Final = (CREATE_SCHEDULE_TO_START + CREATE_TIMEOUT).total_seconds() + 30


def create_workflow_id(key: str, ask: CreateAsk) -> str:
    """One create per caller's request key and ask: the same key with another ask is
    another create."""
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
        """Whether a create of this key and ask was started before (it may have ended)."""
        try:
            await self.client.get_workflow_handle(create_workflow_id(key, ask)).describe()
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                return False
            raise
        return True

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
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                result_type=TdfpOutcome,
            )
        except WorkflowAlreadyStartedError:
            # It ran and closed: its outcome, never a second create.
            handle = self.client.get_workflow_handle(workflow_id, result_type=TdfpOutcome)
        return await _outcome(handle, wait)


async def _outcome(handle: WorkflowHandle[object, TdfpOutcome], wait: float) -> TdfpOutcome:
    async with asyncio.timeout(wait):
        return await handle.result()


def tdfp(state: AppState) -> Tdfp:
    """The app's runner: the operations' client, on the profile-fetch worker's queue."""
    from scadbuddy.operations.component import OPERATIONS

    ops = state.components.get(OPERATIONS)
    return TemporalTdfp(ops.client, state.settings.temporal_task_queue_profile_fetch)
