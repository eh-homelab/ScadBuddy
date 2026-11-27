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


@pytest.fixture
def prompts_before_8062(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new run's `prompt` checks as a call from before #8062 replays: without
    `PROMPT_WIDGETS_PATCH`. As `waits_before_8062` in `tests/conftest.py`."""
    from temporalio import workflow

    from scadbuddy.workflows.flow_tools import PROMPT_WIDGETS_PATCH

    patched = workflow.patched

    def before(patch_id: str) -> bool:
        return not patch_id.startswith(f"{PROMPT_WIDGETS_PATCH}:") and patched(patch_id)

    monkeypatch.setattr(workflow, "patched", before)


@temporal
@postgres
@pytest.mark.usefixtures("prompts_before_8062")
async def test_a_prompt_from_before_8062_refuses_a_widget_schema(flows: Flows) -> None:
    run_id = await flows.start(
        script(
            "try:",
            f"    await prompt('q', {SCAN!r})",
            "except ValueError as e:",
            "    return str(e)",
        )
    )
    done = await flows.finished(run_id)
    assert done.status == "succeeded" and "'x-sb-widget' is not supported" in (done.result or "")
    assert await run_tasks(flows, run_id) == []
    assert "flow_task_open" not in await activities(flows, run_id)


@temporal
@postgres
@pytest.mark.usefixtures("prompts_before_8062")
async def test_a_prompt_from_before_8062_does_not_check_its_blocks(flows: Flows) -> None:
    """The workflow takes the block as before #8062 and opens the task. The server keeps
    the new rule (it is not replayed), so the open is refused there."""
    plain = {"type": "object", "properties": {"x": {"type": "string"}}}
    run_id = await flows.start(
        script(
            "try:",
            f"    await prompt('q', {plain!r}, context=[{{'entity': 'SB-1042'}}])",
            "except ValueError as e:",
            "    return str(e)",
        )
    )
    done = await flows.finished(run_id)
    assert done.status == "succeeded" and "entity" in (done.result or ""), done.result
    assert "flow_task_open" in await activities(flows, run_id)
