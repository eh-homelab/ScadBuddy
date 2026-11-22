"""A run that never gets its `execute` Update fails and closes (#7032).

Measured in prod on 2026-11-22: two runs started on 2026-11-15 lost the start's `execute`
Update when their first workflow task timed out on the projects worker; the retried task
completed with no commands, and each run waited for a script for ever, with no row. A start
with no Update is that run: the workflow is started, and no message ever reaches it.
"""

import uuid

import pytest
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporalio.api.enums.v1 import EventType

from scadbuddy.workflows.project import NO_SCRIPT_REASON, PROJECT_WORKFLOW, FlowStart
from tests.flows.flows_support import Flows, define, script
from tests.flows.test_project_replay import _record

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]


async def _start_without_script(flows: Flows, wait_s: int) -> str:
    """The run as Temporal holds it once its `execute` Update was lost."""
    definition = await define(flows.store, "t")
    run_id = str(uuid.uuid4())
    await flows.client.start_workflow(
        PROJECT_WORKFLOW,
        args=[
            AgentConfig(),
            FlowStart(
                run_id=run_id,
                definition_id=definition.id,
                version=definition.version,
                name="t",
                started_by={"kind": "browser"},
                script_wait_s=wait_s,
            ),
        ],
        id=f"flow-{run_id}",
        task_queue=flows.queue,
    )
    return run_id


async def test_a_run_whose_script_never_arrives_fails_and_closes(flows: Flows) -> None:
    run_id = await _start_without_script(flows, 2)
    run = await flows.finished(run_id)
    assert run.status == "failed"
    assert run.result == NO_SCRIPT_REASON
    assert run.steps == []
    await _record(flows, "unscripted", run_id)


async def test_a_run_whose_script_arrives_is_not_failed_by_the_wait(flows: Flows) -> None:
    """The wait ends once `execute` starts: a script that runs past it still succeeds,
    and the history keeps no timer of the wait's that fired."""
    definition = await define(flows.store, "t")
    run_id = str(uuid.uuid4())
    await flows.send(run_id, definition.id, script("await sleep(3)", "return 1"), script_wait_s=1)
    run = await flows.finished(run_id)
    assert run.status == "succeeded"
    assert run.result == "result: 1"
    history = await flows.client.get_workflow_handle(f"flow-{run_id}").fetch_history()
    fired = [e for e in history.events if e.event_type == EventType.EVENT_TYPE_TIMER_FIRED]
    # The script's own `sleep(3)` is the only timer that fires.
    assert len(fired) == 1
    await _record(flows, "scripted", run_id)
