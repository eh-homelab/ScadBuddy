"""`warm_sandbox` (#6346): the projects worker runs one script in each of the harness's
Monty pools before it polls, so a run's first step does not pay for the pool's first
process inside the live pool's 1 s watchdog."""

from __future__ import annotations

from typing import Any

import pytest
from temporal_agent_harness.harness.code_mode import monty_stepper

from scadbuddy.workflows import projects_worker
from scadbuddy.workflows.flow_tools import run_flow_tool
from scadbuddy.workflows.projects_worker import WARM_SCRIPT, warm_sandbox


def test_the_warm_up_script_passes_the_flows_type_check() -> None:
    """A script the type check refused would warm nothing past the checker."""
    stubs = run_flow_tool().__code_mode_stubs__
    assert monty_stepper.type_check(WARM_SCRIPT, stubs) is None
    step = monty_stepper.ScriptRun(WARM_SCRIPT, stubs, answer_os=projects_worker._no_os).start()
    assert (step.done, step.error, step.awaiting) == (True, None, [])


def test_warm_sandbox_runs_in_both_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    """One step on the live pool (`ScriptRun.start`) and one check on the replay pool
    (`type_check`), each against the flows' own stubs."""
    seen: list[str] = []
    real_start = monty_stepper.ScriptRun.start
    real_check = monty_stepper.type_check

    def start(self: monty_stepper.ScriptRun, **kwargs: Any) -> monty_stepper.Step:
        seen.append("live")
        return real_start(self, **kwargs)

    def check(script: str, stubs: Any) -> Any:
        seen.append("replay")
        return real_check(script, stubs)

    monkeypatch.setattr(monty_stepper.ScriptRun, "start", start)
    monkeypatch.setattr(monty_stepper, "type_check", check)
    warm_sandbox()
    assert seen == ["live", "replay"]


def test_a_sandbox_that_will_not_warm_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The worker still starts: a step it could not warm is the run's to fail, as before.
    A step the watchdog stopped is tried once more."""
    starts: list[int] = []

    def timed_out(self: monty_stepper.ScriptRun, **_: Any) -> monty_stepper.Step:
        starts.append(1)
        return monty_stepper.Step(done=True, aborted=True, timed_out=True, error="slow")

    monkeypatch.setattr(monty_stepper.ScriptRun, "start", timed_out)

    def crashed(script: str, stubs: Any) -> str | None:
        # MontyCrashedError cannot be made outside Monty; any error takes the same path.
        raise RuntimeError("worker died")

    monkeypatch.setattr(monty_stepper, "type_check", crashed)
    warm_sandbox()
    assert len(starts) == 2
    assert "live pool did not warm (timed out: True): slow" in caplog.text
    assert "the flow sandbox did not warm" in caplog.text


def test_a_pool_that_cannot_start_does_not_stop_the_worker(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Review of #6351: the live pool is built outside `ScriptRun`'s own handling, so a
    `monty` process that cannot be spawned raises from `start`."""

    def cannot_spawn(self: monty_stepper.ScriptRun, **_: Any) -> monty_stepper.Step:
        raise OSError("Resource temporarily unavailable")

    monkeypatch.setattr(monty_stepper.ScriptRun, "start", cannot_spawn)
    warm_sandbox()
    assert "the flow sandbox did not warm" in caplog.text
