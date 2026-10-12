"""A flow step's writes when its workflow is torn down (#1057): a call's coroutine
closed after an eviction, maybe while another workflow's event loop is current, writes
nothing, so it never issues a command in that other workflow."""

from datetime import UTC, datetime
from typing import Any

import pytest
from temporalio import workflow

from scadbuddy.workflows import flow_steps
from scadbuddy.workflows.flow_models import ProjectionWrite


class _Owner:
    run_id = "r1"

    def next_seq(self) -> int:
        return 1


class _Info:
    run_id = "wr1"

    def get_current_history_length(self) -> int:
        return 11


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[ProjectionWrite]:
    written: list[ProjectionWrite] = []

    async def project(write: ProjectionWrite) -> None:
        written.append(write)

    monkeypatch.setattr(flow_steps, "project", project)
    monkeypatch.setattr(workflow, "instance", lambda: _Owner())
    monkeypatch.setattr(workflow, "info", lambda: _Info())
    monkeypatch.setattr(workflow, "now", lambda: datetime(2026, 10, 11, tzinfo=UTC))
    return written


async def test_a_closed_call_writes_no_end(writes: list[ProjectionWrite]) -> None:
    step = flow_steps.step("sleep", "c1", outward=False)
    await step.__aenter__()
    # Not suppressed: the close goes on.
    assert not await step.__aexit__(GeneratorExit, GeneratorExit(), None)
    assert [w.step.status for w in writes if w.step is not None] == ["running"]


@pytest.mark.parametrize(("raised", "status"), [(None, "succeeded"), (KeyError, "failed")])
async def test_an_ended_call_writes_its_end(
    writes: list[ProjectionWrite], raised: Any, status: str
) -> None:
    step = flow_steps.step("sleep", "c1", outward=False)
    await step.__aenter__()
    if raised is None:
        await step.__aexit__(None, None, None)
    else:
        assert not await step.__aexit__(raised, raised(), None)
    assert [w.step.status for w in writes if w.step is not None] == ["running", status]
