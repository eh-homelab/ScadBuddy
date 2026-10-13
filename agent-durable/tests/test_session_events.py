"""The live output's translation, its once-only append, and a turn's end (Rulings 6, 7)."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator
from typing import cast

import psycopg
import pytest
from session_support import MANIFEST, Conn, events, insert_session
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy_durable.session import events as events_module
from scadbuddy_durable.session import tools
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
    await agent_db.execute(
        "UPDATE ai_sessions SET cost_usd = 0.5, turns = 3, durable_turn_id = gen_random_uuid()"
        " WHERE id = %s",
        (sid,),
    )
    acts = SessionActivities(connect)  # type: ignore[arg-type]
    args = FinishTurn(session_id=sid, turn_id="t", outcome="failed", message="it broke")
    assert await acts.finish_turn(args)
    # The turn is over: it is no longer the session's running turn (#2320).
    cur = await agent_db.execute("SELECT durable_turn_id FROM ai_sessions WHERE id = %s", (sid,))
    assert await cur.fetchone() == (None,)
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


@pytest.mark.requires_postgres
async def test_gate_settings_read_the_tools_overrides_sanitised(
    agent_db: Conn, connect: object
) -> None:
    tools.use_manifest(MANIFEST)
    acts = SessionActivities(connect)  # type: ignore[arg-type]
    none = await acts.gate_settings()
    assert (none.tool_tiers, none.disabled_tools) == ({}, [])
    await agent_db.execute(
        "INSERT INTO ai_settings (key, value) VALUES (%s, %s)",
        (
            tools.OVERRIDES_SETTING,
            json.dumps(
                {
                    "tool_tiers": {"render_preview": "outward", "print_output": "read"},
                    "disabled_tools": ["print_output", "gone"],
                }
            ),
        ),
    )
    read = await acts.gate_settings()
    assert read.tool_tiers == {"render_preview": "outward"}
    assert read.disabled_tools == ["print_output"]


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


@pytest.mark.requires_postgres
async def test_a_session_that_is_gone_is_not_retried(connect: object) -> None:
    env = ActivityEnvironment(client=cast(Client, object()))
    acts = SessionEvents(connect)  # type: ignore[arg-type]
    gone = "9b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f61"
    with pytest.raises(ApplicationError) as failed:
        await env.run(
            acts.follow_session, FollowArgs(session_id=gone, workflow_id=f"session-{gone}")
        )
    assert failed.value.type == "LookupError"
    assert failed.value.non_retryable


@pytest.mark.requires_postgres
async def test_a_turn_ends_on_its_own_end_not_the_last_turns(
    agent_db: Conn, connect: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI on #1958: a previous turn's subscriber was cancelled before it wrote that turn's
    end, so the next one read it first and stopped before its own text."""
    sid = await insert_session(agent_db)
    stream = [
        {"type": "cancelled", "offset": 0},
        {"type": "prompt", "text": "again", "offset": 1},
        {"type": "text", "text": "hello", "offset": 2},
        {"type": "done", "result": "hello", "offset": 3},
    ]

    async def follow(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        for e in stream:
            yield dict(e)

    monkeypatch.setattr(events_module, "follow_agent", follow)
    env = ActivityEnvironment(client=cast(Client, object()))
    acts = SessionEvents(connect)  # type: ignore[arg-type]
    result = await env.run(
        acts.follow_session,
        FollowArgs(session_id=sid, workflow_id=f"session-{sid}", prompted=True),
    )
    assert result.ended == "done"
    assert [e["type"] for e in await events(agent_db, sid)] == [
        "assistant.text.delta",
        "assistant.text.done",
    ]


@pytest.mark.requires_postgres
async def test_a_carried_over_turn_ends_on_its_own_end_not_an_earlier_turns(
    agent_db: Conn, connect: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2123: a turn handed over by continue-as-new has no prompt in this run, so an
    earlier turn's unread end is told apart by its time."""
    sid = await insert_session(agent_db)
    since = "2026-10-11T12:00:00+00:00"
    stream = [
        {"type": "cancelled", "at": "2026-10-11T11:59:58+00:00", "offset": 0},
        {"type": "text", "text": "hello", "at": "2026-10-11T12:00:03+00:00", "offset": 1},
        {"type": "done", "result": "hello", "at": "2026-10-11T12:00:04+00:00", "offset": 2},
    ]

    async def follow(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        for e in stream:
            yield dict(e)

    monkeypatch.setattr(events_module, "follow_agent", follow)
    env = ActivityEnvironment(client=cast(Client, object()))
    acts = SessionEvents(connect)  # type: ignore[arg-type]
    result = await env.run(
        acts.follow_session,
        FollowArgs(session_id=sid, workflow_id=f"session-{sid}", since=since),
    )
    assert result.ended == "done"
    assert [e["type"] for e in await events(agent_db, sid)] == [
        "assistant.text.delta",
        "assistant.text.done",
    ]


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@pytest.mark.requires_postgres
async def test_a_tool_result_names_the_images_its_call_produced(
    agent_db: Conn, connect: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2478: the agent-tools activity kept the call's images (ai_session_blobs) and
    recorded them under its tool_use_id; the tool.result names them, never holds them."""
    sid = await insert_session(agent_db)
    name = f"{hashlib.sha256(PNG).hexdigest()}.png"
    await agent_db.execute(
        "INSERT INTO ai_session_blobs (session_id, name, media_type, data)"
        " VALUES (%s, %s, 'image/png', %s)",
        (sid, name, PNG),
    )
    await agent_db.execute(
        "INSERT INTO ai_session_tool_images (session_id, tool_use_id, ord, name)"
        " VALUES (%s, 'toolu_img', 0, %s)",
        (sid, name),
    )
    stream = [
        {"type": "prompt", "text": "show me", "offset": 0},
        {"type": "tool_call", "id": "toolu_img", "name": "render_preview", "offset": 1},
        {"type": "tool_result", "id": "toolu_img", "status": "done", "offset": 2},
        # A call that produced none, as every event before #2478 was.
        {"type": "tool_call", "id": "toolu_txt", "name": "render_preview", "offset": 3},
        {"type": "tool_result", "id": "toolu_txt", "status": "done", "offset": 4},
        {"type": "done", "result": "ok", "offset": 5},
    ]

    async def follow(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        for e in stream:
            yield dict(e)

    monkeypatch.setattr(events_module, "follow_agent", follow)
    env = ActivityEnvironment(client=cast(Client, object()))
    acts = SessionEvents(connect)  # type: ignore[arg-type]
    await env.run(
        acts.follow_session,
        FollowArgs(session_id=sid, workflow_id=f"session-{sid}", prompted=True),
    )
    results = [e for e in await events(agent_db, sid) if e["type"] == "tool.result"]
    assert results[0]["images"] == [{"name": name, "mediaType": "image/png"}]
    assert "images" not in results[1]
    cur = await agent_db.execute(
        "SELECT event FROM ai_session_events WHERE session_id = %s", (sid,)
    )
    logged = "".join(str(r[0]) for r in await cur.fetchall())
    assert base64.b64encode(PNG).decode()[:24] not in logged
    assert PNG.hex()[:24] not in logged


@pytest.mark.requires_postgres
async def test_a_turn_tools_call_is_labelled_with_the_tier_the_listing_gave(
    agent_db: Conn, connect: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin's tool is not in the manifest, so its calls read "outward" in the
    transcript whatever Settings tiered it; the turn's listing carries its tier."""
    sid = await insert_session(agent_db)
    stream = [
        {"type": "prompt", "text": "look", "offset": 0},
        {"type": "tool_call", "id": "a", "name": "playwright__browser_snapshot", "offset": 1},
        {"type": "tool_call", "id": "b", "name": "playwright__browser_navigate", "offset": 2},
        {"type": "tool_call", "id": "c", "name": "other__unlisted", "offset": 3},
        {"type": "done", "result": "", "offset": 4},
    ]

    async def follow(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        for e in stream:
            yield dict(e)

    monkeypatch.setattr(events_module, "follow_agent", follow)
    env = ActivityEnvironment(client=cast(Client, object()))
    acts = SessionEvents(connect)  # type: ignore[arg-type]
    await env.run(
        acts.follow_session,
        FollowArgs(
            session_id=sid,
            workflow_id=f"session-{sid}",
            prompted=True,
            tiers={
                "playwright__browser_snapshot": "read",
                "playwright__browser_navigate": "write",
            },
        ),
    )
    risks = {e["name"]: e["risk"] for e in await events(agent_db, sid) if e["type"] == "tool.call"}
    assert risks == {
        "playwright__browser_snapshot": "read",
        "playwright__browser_navigate": "write",
        "other__unlisted": "outward",
    }
