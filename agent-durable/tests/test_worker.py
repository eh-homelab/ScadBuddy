"""The worker's segment runner: the real one, or the scripted model the agent service's
end-to-end test runs against (SCADBUDDY_DURABLE_SCRIPTED, tests only)."""

from __future__ import annotations

from typing import Any

from temporalio.claude_agent_sdk.testing import Final, ScriptedClaude

from scadbuddy_durable.secrets import kek_from_base64
from scadbuddy_durable.session.runner import ScadBuddyRunner
from scadbuddy_durable.session.scripted import echo
from scadbuddy_durable.worker import config_from_env, segment_runner

KEK = kek_from_base64("WlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlo=")
BASE = {
    "SCADBUDDY_DATABASE_URL": "postgresql://u:pw@h/db",
    "SCADBUDDY_TEMPORAL_ADDRESS": "t:7233",
    "SCADBUDDY_SECRET_KEY_FILE": "/k",
    "SCADBUDDY_DURABLE_TOOLS_JSON": "/tools.json",
}


def _connect() -> Any:
    raise AssertionError("not connected in this test")


def test_the_real_runner_unless_the_scripted_one_is_asked_for() -> None:
    cfg = config_from_env(BASE)
    assert cfg.scripted is False
    assert isinstance(segment_runner(cfg, _connect, KEK), ScadBuddyRunner)
    for off in ("", "0"):
        assert config_from_env({**BASE, "SCADBUDDY_DURABLE_SCRIPTED": off}).scripted is False
    scripted = config_from_env({**BASE, "SCADBUDDY_DURABLE_SCRIPTED": "1"})
    assert scripted.scripted is True
    assert isinstance(segment_runner(scripted, _connect, KEK), ScriptedClaude)


def test_the_scripted_model_answers_with_what_it_was_told() -> None:
    assert echo("hello there", []) == Final("you said: hello there")
