"""`prompt()` with an assignee, a checked context and a widget schema (#8062), on a real
Temporal and Postgres. The parked-run helpers are `tests/flows/test_prompt.py`'s."""

from typing import Any

import pytest

from tests.flows.flows_support import Flows, script
from tests.flows.test_prompt import activities, parked, run_tasks, task_of

temporal = pytest.mark.requires_temporal
postgres = pytest.mark.requires_postgres

SCAN: dict[str, Any] = {
    "type": "object",
    "required": ["code"],
    "properties": {
        "code": {"type": "string", "x-sb-widget": "scan"},
    },
}


@temporal
@postgres
async def test_a_prompt_keeps_its_assignee(flows: Flows) -> None:
    run_id = await flows.start(script(f"await prompt('q', {SCAN!r}, assignee='browser:jo')"))
    run = await parked(flows, run_id)
    task = await task_of(flows, run.waiting_on[0].call_id)
    assert task.assignee == "browser:jo"


@temporal
@postgres
async def test_a_prompt_without_an_assignee_has_none(flows: Flows) -> None:
    run_id = await flows.start(script(f"await prompt('q', {SCAN!r})"))
    run = await parked(flows, run_id)
    assert (await task_of(flows, run.waiting_on[0].call_id)).assignee is None


@temporal
@postgres
async def test_a_bad_context_block_raises_before_any_activity(flows: Flows) -> None:
    bad = "[{'checklist': 'not a list'}]"
    run_id = await flows.start(
        script(
            "try:",
            f"    await prompt('q', {SCAN!r}, context={bad})",
            "except ValueError as e:",
            "    return str(e)",
        )
    )
    done = await flows.finished(run_id)
    assert done.status == "succeeded" and "context[0] checklist" in (done.result or ""), done.result
    assert await run_tasks(flows, run_id) == []
    assert "flow_task_open" not in await activities(flows, run_id)


@temporal
@postgres
async def test_a_widget_schema_reaches_the_task_unchanged(flows: Flows) -> None:
    run_id = await flows.start(script(f"await prompt('q', {SCAN!r})"))
    run = await parked(flows, run_id)
    assert (await task_of(flows, run.waiting_on[0].call_id)).schema_ == SCAN
