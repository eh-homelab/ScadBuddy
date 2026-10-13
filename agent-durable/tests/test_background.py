"""bg_monitor_command (scadbuddy_durable/background.py): a durable session's Monitor
command, run where its Bash runs, with its events written for the agent's workflow."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import pytest
from temporalio import activity

from scadbuddy_durable.background import (
    EVENT_MAX_CHARS,
    LineSplitter,
    MonitorActivities,
    event_notice,
    monitor_env,
)

SESSION = "0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60"


def test_the_command_gets_a_shells_environment_only() -> None:
    env = {
        "PATH": "/bin",
        "HOME": "/h",
        "SCADBUDDY_DATABASE_URL": "postgres://secret",
        "ANTHROPIC_API_KEY": "k",
    }
    assert monitor_env(env) == {"PATH": "/bin", "HOME": "/h"}


def test_an_event_reads_as_the_agents() -> None:
    # agent/src/background/monitor.ts eventNotice
    assert event_notice("ab12cd34", "ticks", "tick 1\ntick 2") == (
        "<task-notification>\n<task-id>ab12cd34</task-id>\n"
        '<summary>Monitor event: "ticks"</summary>\n'
        "<event>tick 1\ntick 2</event>\n</task-notification>"
    )


class _Handle:
    def __init__(self, sent: list[str]) -> None:
        self._sent = sent

    async def signal(self, name: str) -> None:
        self._sent.append(name)


class _Client:
    def __init__(self, sent: list[str]) -> None:
        self._sent = sent

    def get_workflow_handle(self, wid: str) -> _Handle:
        assert wid == f"bgtask-{SESSION}-ab12cd34"
        return _Handle(self._sent)


def _activities(
    tmp_path: Any, spec: dict[str, Any], events: list[str], cap: int = 200
) -> MonitorActivities:
    acts = MonitorActivities(lambda: None, str(tmp_path))  # type: ignore[arg-type,return-value]

    async def task(task_id: str) -> dict[str, Any] | None:
        return {"description": "ticks", "spec": spec, "session_id": SESSION}

    async def append(task_id: str, text: str) -> bool:
        if len(events) >= cap:
            return False
        events.append(text)
        return True

    acts._task = task  # type: ignore[method-assign]
    acts._append = append  # type: ignore[method-assign]
    return acts


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(activity, "client", lambda: _Client(sent))
    monkeypatch.setattr(activity, "heartbeat", lambda *a: None)
    return sent


async def test_batches_lines_reports_the_exit_and_signals_each_event(
    tmp_path: Any, signals: list[str]
) -> None:
    events: list[str] = []
    cmd = "pwd; echo a; echo b; sleep 0.5; echo c; exit 3"
    acts = _activities(tmp_path, {"command": cmd, "timeoutMs": 10_000}, events)
    result = await acts.bg_monitor_command({"taskId": "ab12cd34"})
    assert result == {"end": {"outcome": "exited", "code": 3}, "events": 2}
    assert events == [
        event_notice("ab12cd34", "ticks", f"{os.path.realpath(tmp_path)}\na\nb"),
        event_notice("ab12cd34", "ticks", "c"),
    ]
    assert signals == ["bg_events", "bg_events"]


async def test_expires_and_kills_the_command(tmp_path: Any, signals: list[str]) -> None:
    events: list[str] = []
    acts = _activities(tmp_path, {"command": "echo up; sleep 30", "timeoutMs": 1000}, events)
    started = time.monotonic()
    result = await acts.bg_monitor_command({"taskId": "ab12cd34"})
    assert result == {"end": {"outcome": "expired"}, "events": 1}
    assert time.monotonic() - started < 5


async def test_stops_a_monitor_that_makes_too_many_events(
    tmp_path: Any, signals: list[str]
) -> None:
    events: list[str] = []
    cmd = "while true; do echo x; sleep 0.3; done"
    acts = _activities(tmp_path, {"command": cmd, "timeoutMs": 20_000}, events, cap=2)
    result = await asyncio.wait_for(acts.bg_monitor_command({"taskId": "ab12cd34"}), 10)
    assert result == {"end": {"outcome": "too_many"}, "events": 2}


async def test_an_event_that_cannot_be_written_kills_the_command(
    tmp_path: Any, signals: list[str]
) -> None:
    cmd = "echo $$ > pid; while true; do echo x; sleep 0.2; done"
    acts = _activities(tmp_path, {"command": cmd, "timeoutMs": 20_000}, [])

    async def append(task_id: str, text: str) -> bool:
        raise ConnectionError("the database went away")

    acts._append = append  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(acts.bg_monitor_command({"taskId": "ab12cd34"}), 10)
    pid = int((tmp_path / "pid").read_text())
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_line_without_a_newline_is_cut_not_held() -> None:
    lines = LineSplitter()
    for _ in range(100):
        lines.push("x" * 1000)
    lines.push("\nnext\n")
    out = lines.take()
    assert out == ["x" * EVENT_MAX_CHARS + " […cut]", "next"]
