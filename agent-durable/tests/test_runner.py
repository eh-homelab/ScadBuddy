from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from temporalio.claude_agent_sdk._models import SegmentInput, SegmentOutput
from temporalio.claude_agent_sdk._runner import _check_extra_options
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy_durable.credentials import Credential, NoUsableCredential, credential_env
from scadbuddy_durable.runner import SessionRunner, extra_options, segments_cwd, session_of
from scadbuddy_durable.segments import SessionLimits

SECRET = "sk-ant-very-secret"
SID = str(uuid.uuid4())
SESSION = f"session-{SID}"
PLUGIN = "/srv/plugin"


class FakeCredentials:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def first_usable(self) -> Credential:
        if self.error:
            raise self.error
        return Credential("anthropic_api_key", SECRET)


class FakeSegments:
    def __init__(self, budget: float = 5.0, cost: float = 1.0) -> None:
        self.limits_row = SessionLimits(budget_usd=budget, cost_usd=cost, max_turns=10)
        self.recorded: list[tuple[Any, ...]] = []
        self.asked: list[str] = []

    async def limits(self, session_id: str) -> SessionLimits:
        self.asked.append(session_id)
        return self.limits_row

    async def record(self, *args: Any) -> None:
        self.recorded.append(args)


@dataclass
class Stub:
    out: SegmentOutput | Exception

    async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput:
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


class Factory:
    def __init__(self, out: SegmentOutput | Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.out = out or SegmentOutput(session_id="claude-1", cost_usd=0.25)

    def __call__(self, **kwargs: Any) -> Stub:
        self.calls.append(kwargs)
        return Stub(self.out)


def make(
    factory: Factory, segments: FakeSegments, credentials: FakeCredentials | None = None
) -> tuple[SessionRunner, object]:
    store = object()
    runner = SessionRunner(
        credentials,  # type: ignore[arg-type]
        segments,  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        plugin_dir=PLUGIN,
        prompt_append="APPEND",
        runner_factory=factory,
    )
    return runner, store


def inp() -> SegmentInput:
    return SegmentInput(session_id="claude-0", prompt="hi", tools=[], segment_index=3)


async def run_in_activity(runner: SessionRunner, workflow_id: str = SESSION) -> SegmentOutput:
    env = ActivityEnvironment()
    env.info = _info(env, workflow_id)
    return await env.run(runner.run, inp(), 2)


def _info(env: ActivityEnvironment, workflow_id: str) -> Any:
    import dataclasses

    return dataclasses.replace(env.info, workflow_id=workflow_id)


async def test_builds_runner_with_credential_and_remaining_budget() -> None:
    factory, segments = Factory(), FakeSegments(budget=5.0, cost=1.5)
    runner, store = make(factory, segments, FakeCredentials())
    out = await run_in_activity(runner)
    assert out.session_id == "claude-1"
    [kwargs] = factory.calls
    assert kwargs["env"] == credential_env(Credential("anthropic_api_key", SECRET))
    assert kwargs["max_budget_usd"] == 3.5
    assert kwargs["session_store"] is store
    assert kwargs["cwd"] == segments_cwd()  # /srv/agent in the image
    assert kwargs["extra_options"] == {
        "plugins": [{"type": "local", "path": PLUGIN}],
        "system_prompt": {"type": "preset", "preset": "claude_code", "append": "APPEND"},
    }
    assert segments.asked == [SID]
    assert segments.recorded == [(SID, 3, 2, "claude-1", 0.25)]


async def test_spent_budget_builds_no_runner() -> None:
    factory = Factory()
    runner, _ = make(factory, FakeSegments(budget=1.0, cost=1.0), FakeCredentials())
    out = await run_in_activity(runner)
    assert out.is_error and out.error == "the session's budget is spent"
    assert factory.calls == []


async def test_no_usable_credential_is_non_retryable() -> None:
    reason = "no usable Claude credential: 1 disabled, 0 cooling down, 0 sealed with another key"
    runner, _ = make(Factory(), FakeSegments(), FakeCredentials(NoUsableCredential(reason)))
    with pytest.raises(ApplicationError) as err:
        await run_in_activity(runner)
    assert err.value.non_retryable
    assert err.value.message == reason
    assert SECRET not in str(err.value)


@pytest.mark.parametrize("workflow_id", ["flow-" + str(uuid.uuid4()), "x", "session-nope"])
async def test_foreign_workflow_id_is_refused(workflow_id: str) -> None:
    factory = Factory()
    runner, _ = make(factory, FakeSegments(), FakeCredentials())
    with pytest.raises(ApplicationError) as err:
        await run_in_activity(runner, workflow_id)
    assert err.value.non_retryable
    assert factory.calls == []


def test_session_of() -> None:
    assert session_of(SESSION) == SID
    with pytest.raises(ApplicationError):
        session_of("x")


async def test_secret_never_logged_on_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    runner, _ = make(Factory(RuntimeError("boom")), FakeSegments(), FakeCredentials())
    with pytest.raises(RuntimeError):
        await run_in_activity(runner)
    assert SECRET not in caplog.text


def test_extra_options_pass_the_plugins_check() -> None:
    _check_extra_options(extra_options(PLUGIN, "APPEND"))
