"""Workflow logic flag 2 on for every workflow this backend starts (#8313).

temporalio 1.36.0 applied every job of a workflow activation before any workflow task
ran (workflow logic flag 2, ``PROCESS_WORKFLOW_ACTIVATION_JOBS_AS_SINGLE_BATCH``). The
#1832 release race and ``handler_wait`` (`handlers.py`) rely on that order. 1.37.0 turns
the flag off by default for workflows whose history has not recorded it ("to preserve
legacy signal, condition-wait, and cancellation behavior for unflagged workflows",
https://github.com/temporalio/sdk-python/pull/2121); histories that recorded it keep it.
Every workflow this backend started on 1.36 recorded it, so with the flag on a new run
behaves as those do. The SDK has no public option for it: ``Worker``'s
``_set_default_workflow_logic_flag`` is the one its own tests use (1.37.0
``tests/worker/test_workflow.py``). `tests/test_handler_wait.py` and
`tests/test_submit.py` fail if a later release changes or removes it.
"""

from __future__ import annotations

import temporalio.worker._workflow as _sdk_workflow
from temporalio.worker import Worker
from temporalio.worker._workflow_instance import _WorkflowLogicFlag  # type: ignore[attr-defined]

SINGLE_BATCH = _WorkflowLogicFlag.PROCESS_WORKFLOW_ACTIVATION_JOBS_AS_SINGLE_BATCH


def single_batch(worker: Worker) -> Worker:
    """``worker`` (one that runs workflows, before it starts) with flag 2 on."""
    worker._set_default_workflow_logic_flag(SINGLE_BATCH, enabled=True)
    return worker


def single_batch_by_default() -> None:
    """Flag 2 on for every workflow ``Worker`` built after this call. For the tests,
    whose own workers then run workflows as `single_batch` workers do."""
    current = _sdk_workflow._DEFAULT_ENABLED_WORKFLOW_LOGIC_FLAGS  # type: ignore[attr-defined]
    _sdk_workflow._DEFAULT_ENABLED_WORKFLOW_LOGIC_FLAGS = frozenset({*current, SINGLE_BATCH})  # type: ignore[attr-defined]
