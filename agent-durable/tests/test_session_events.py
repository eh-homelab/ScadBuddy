"""The live output's translation, its once-only append, and a turn's end (Rulings 6, 7)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import psycopg
import pytest
from session_support import Conn, events, insert_session
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy_durable.session import events as events_module
from scadbuddy_durable.session.activities import SessionActivities
from scadbuddy_durable.session.events import (
    INPUT_MAX,
    REDACTED,
    SessionEvents,
    append_from,
    logged_input,
    translate,
)
from scadbuddy_durable.session.models import FinishTurn, FollowArgs
from scadbuddy_durable.worker import config_from_env

SID = "0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60"


def test_translates_the_plugins_events_into_the_panels() -> None:
    tiers = {"print_output": "outward", "render_preview": "read"}
    text = translate(SID, {"type": "text", "text": "Hi", "offset": 4}, tiers)
    assert [e["type"] for e in text] == ["assistant.text.delta", "assistant.text.done"]
    assert text[0]["messageId"] == text[1]["messageId"] == "durable-4"
    [call] = translate(
        SID,
        {"type": "tool_call", "id": "t1", "name": "render_preview", "input": {"a": 1}, "offset": 5},
        tiers,
    )
    assert call == {
        "v": 1,
        "sessionId": SID,
        "type": "tool.call",
        "id": "t1",
        "name": "render_preview",
        "input": {"a": 1},
        "risk": "read",
    }
    [unknown] = translate(SID, {"type": "tool_call", "id": "t2", "name": "x", "offset": 6}, tiers)
    assert unknown["risk"] == "outward"
    [result] = translate(SID, {"type": "tool_result", "id": "t1", "status": "rejected"}, tiers)
    assert (result["ok"], result["summary"]) == (False, "rejected")
    for dropped in ("prompt", "approval_needed", "retry", "continued_as_new", "done", "error"):
        assert translate(SID, {"type": dropped, "offset": 1}, tiers) == []


def test_a_logged_input_blanks_secrets_and_is_capped() -> None:
    assert logged_input({"api_key": "k", "nested": [{"password": "p", "ok": 1}]}) == {
        "api_key": REDACTED,
        "nested": [{"password": REDACTED, "ok": 1}],
    }
    long = logged_input({"text": "x" * (INPUT_MAX * 2)})
    assert long["truncated"] is True
    assert len(long["preview"]) == INPUT_MAX


@pytest.mark.requires_postgres
async def test_an_offset_is_written_once(agent_db: Conn) -> None:
    sid = await insert_session(agent_db)
    one = [{"v": 1, "type": "assistant.text.done", "sessionId": sid, "messageId": "m"}]
    assert await append_from(agent_db, sid, 0, one)
    assert not await append_from(agent_db, sid, 0, one)  # a second subscriber
    assert await append_from(agent_db, sid, 1, [])  # nothing to write still moves on
    assert await append_from(agent_db, sid, 2, one)
    assert len(await events(agent_db, sid)) == 2


@pytest.mark.requires_postgres
async def test_finish_turn_writes_once(agent_db: Conn, connect: object) -> None:
    sid = await insert_session(agent_db, budget=2.0)
    await agent_db.execute("UPDATE ai_sessions SET cost_usd = 0.5, turns = 3 WHERE id = %s", (sid,))
    acts = SessionActivities(connect)  # type: ignore[arg-type]
    args = FinishTurn(session_id=sid, turn_id="t", outcome="failed", message="it broke")
    assert await acts.finish_turn(args)
    assert not await acts.finish_turn(args)  # a retry writes nothing twice
    log = await events(agent_db, sid)
    assert [e["type"] for e in log] == ["session.result", "error", "session.status"]
    assert log[0] == {**log[0], "costUsd": 0.5, "turns": 3, "budgetUsd": 2.0}
    assert log[1]["code"] == "turn_failed"
    assert log[2]["status"] == "failed"


@pytest.mark.requires_postgres
async def test_gate_settings_are_read_and_clamped(agent_db: Conn, connect: object) -> None:
    acts = SessionActivities(connect)  # type: ignore[arg-type]
    defaults = await acts.gate_settings()
    assert (defaults.approval_expiry_s, defaults.question_expiry_s) == (600, 3600)
    await agent_db.execute(
        "INSERT INTO ai_settings (key, value) VALUES ('approval_expiry_seconds', '5'),"
        " ('question_expiry_seconds', '999999')"
    )
    clamped = await acts.gate_settings()
    assert (clamped.approval_expiry_s, clamped.question_expiry_s) == (10, 86_400)


def test_the_worker_names_what_it_misses_and_never_shows_the_database_url() -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_DURABLE_TOOLS_JSON"):
        config_from_env({"SCADBUDDY_DATABASE_URL": "postgresql://u:pw@h/db"})
    cfg = config_from_env(
        {
            "SCADBUDDY_DATABASE_URL": "postgresql://u:pw@h/db",
            "SCADBUDDY_TEMPORAL_ADDRESS": "t:7233",
            "SCADBUDDY_SECRET_KEY_FILE": "/k",
            "SCADBUDDY_DURABLE_TOOLS_JSON": "/tools.json",
        }
    )
    assert cfg.cwd == "/srv/agent"
    assert cfg.namespace == "default"
    assert "pw" not in repr(cfg)


SECRET = "the user's private words, a token sk-ant-xyz"


@pytest.mark.requires_postgres
async def test_a_failure_carries_no_event_content(
    agent_db: Conn,
    connect: object,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Security review of 5c (sensitive-to-observability): an error while writing an
    event (a driver error can quote the row it refused) leaves the activity with its
    type and the offset only, so no worker log, failure message or span carries content."""
    sid = await insert_session(agent_db)

    async def follow(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        yield {"type": "text", "text": SECRET, "offset": 7}

    async def refuse(*_: object) -> bool:
        raise psycopg.DataError(f'invalid input: "{SECRET}"')

    monkeypatch.setattr(events_module, "follow_agent", follow)
    monkeypatch.setattr(events_module, "append_from", refuse)
    env = ActivityEnvironment(client=cast(Client, object()))
    acts = SessionEvents(connect)  # type: ignore[arg-type]
    with pytest.raises(ApplicationError) as failed:
        await env.run(acts.follow_session, FollowArgs(session_id=sid, workflow_id=f"session-{sid}"))
    err = failed.value
    shown = f"{err} {err.details} {err.__cause__} {err.__context__}"
    assert SECRET not in shown
    assert err.__cause__ is None
    assert err.__context__ is None
    assert "offset 7" in str(err)
    assert "DataError" in str(err)
    assert SECRET not in caplog.text
