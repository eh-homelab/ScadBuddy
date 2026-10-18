"""The live output's translation, its once-only append, and a turn's end (Rulings 6, 7)."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from session_support import MANIFEST, Conn, events, insert_session, until
from temporalio import workflow

# The plugin's stream topic, which follow_agent reads: the package exports no name for it.
from temporalio.claude_agent_sdk._events import TOPIC
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.workflow import ActivityCancellationType

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
from scadbuddy_durable.session.models import FinishTurn, FollowArgs, FollowResult
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
    # Whole and in part: a truncated copy of the bytes is still a leak. The first 24
    # characters are the header every PNG shares, so a slice from the middle and the
    # end are checked too (#3214 review).
    for encoded in (base64.b64encode(PNG).decode(), PNG.hex()):
        middle = len(encoded) // 2
        assert encoded not in logged
        for window in (encoded[:24], encoded[middle - 12 : middle + 12], encoded[-24:]):
            assert window not in logged


@pytest.mark.requires_postgres
async def test_a_turn_tools_call_is_labelled_with_the_tier_the_listing_gave(
    agent_db: Conn,
    connect: object,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
                # Not a tier (#2839): labelled outward, never passed through.
                "other__unlisted": "<b>admin</b>",
            },
        ),
    )
    risks = {e["name"]: e["risk"] for e in await events(agent_db, sid) if e["type"] == "tool.call"}
    assert risks == {
        "playwright__browser_snapshot": "read",
        "playwright__browser_navigate": "write",
        "other__unlisted": "outward",
    }
    # Said, naming the tool and never the agent's value (#3144). The record itself, so
    # a warning that never reached caplog cannot pass (#3245).
    [warned] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "other__unlisted" in warned.getMessage()
    assert "admin" not in warned.getMessage()


@pytest.mark.requires_postgres
async def test_a_manifest_tier_that_is_not_a_tier_is_labelled_outward(
    agent_db: Conn,
    connect: object,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#3245: the manifest's own tiers were used as they were, so one that is not a tier
    reached the transcript's label."""
    # Through parse_manifest, as a worker reads it, so this covers parse plus label (#3916).
    [odd] = tools.parse_manifest(
        [
            {
                "name": "odd_tool",
                "description": "odd",
                "input_schema": {"type": "object"},
                "tier": "<b>admin</b>",
            }
        ]
    )
    monkeypatch.setattr(tools, "_MANIFEST", (*MANIFEST, odd))
    caplog.clear()  # parse_manifest's own warning: the turn must add none of its own
    sid = await insert_session(agent_db)
    stream = [
        {"type": "prompt", "text": "go", "offset": 0},
        {"type": "tool_call", "id": "a", "name": "odd_tool", "offset": 1},
        {"type": "tool_call", "id": "b", "name": MANIFEST[0].name, "offset": 2},
        {"type": "done", "result": "", "offset": 3},
    ]

    async def follow(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        for e in stream:
            yield dict(e)

    monkeypatch.setattr(events_module, "follow_agent", follow)
    env = ActivityEnvironment(client=cast(Client, object()))
    acts = SessionEvents(connect)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING):
        await env.run(
            acts.follow_session,
            FollowArgs(session_id=sid, workflow_id=f"session-{sid}", prompted=True),
        )
    risks = {e["name"]: e["risk"] for e in await events(agent_db, sid) if e["type"] == "tool.call"}
    assert risks == {"odd_tool": "outward", MANIFEST[0].name: MANIFEST[0].tier}
    # Said once, by parse_manifest, not again on every turn (#3581).
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_a_manifest_tier_that_is_not_a_tier_is_said_once_and_kept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#3581: the manifest is the worker's for its life, so its bad tier is said at parse
    time, naming the tool and never the value, and the worker still starts. The tier is
    kept as given (#3882): follow_session labels it outward, and Settings can still raise
    the tool (test_tool_overrides)."""
    entry = {"name": "odd_tool", "description": "odd", "input_schema": {"type": "object"}}
    with caplog.at_level(logging.WARNING):
        [odd, fine] = tools.parse_manifest(
            [{**entry, "tier": "<b>admin</b>"}, {**entry, "name": "fine", "tier": "write"}]
        )
    assert (odd.tier, fine.tier) == ("<b>admin</b>", "write")
    [warned] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "odd_tool" in warned.getMessage()
    assert "admin" not in warned.getMessage()


@pytest.mark.requires_postgres
async def test_a_listed_tier_that_is_not_a_tier_overrides_the_manifests_with_outward(
    agent_db: Conn, connect: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3581: the listing wins over the manifest even when what it lists is not a tier,
    and outward is the label then, not the manifest's."""
    read = next(e for e in MANIFEST if e.tier == "read")
    sid = await insert_session(agent_db)
    stream = [
        {"type": "prompt", "text": "go", "offset": 0},
        {"type": "tool_call", "id": "a", "name": read.name, "offset": 1},
        {"type": "done", "result": "", "offset": 2},
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
            session_id=sid, workflow_id=f"session-{sid}", prompted=True, tiers={read.name: "bogus"}
        ),
    )
    risks = {e["name"]: e["risk"] for e in await events(agent_db, sid) if e["type"] == "tool.call"}
    assert risks == {read.name: "outward"}


def test_the_streams_topic_is_the_plugins() -> None:
    """The private TOPIC the stream tests publish on: a pin bump that changes it fails here."""
    assert TOPIC == "claude"


def test_risk_tiers_are_the_agents() -> None:
    """#3144: `RISK_TIERS` copies the agent's `RiskTier`; both are pinned to one file."""
    fixture = (
        Path(__file__).resolve().parents[2] / "agent" / "test" / "fixtures" / "risk-tiers.json"
    )
    assert frozenset(json.loads(fixture.read_text())) == events_module.RISK_TIERS


@workflow.defn(sandboxed=False)
class Streamed:
    """A session's stream alone: what a test publishes, until it is told to close."""

    @workflow.init
    def __init__(self) -> None:
        self._topic = WorkflowStream().topic(TOPIC)
        self._closed = False

    @workflow.signal
    def emit(self, event: dict[str, Any]) -> None:
        self._topic.publish(event)

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.query
    def polled(self) -> bool:
        """A subscriber's poll is waiting here: its client is in the poll update's
        start or result, where a cancel becomes WorkflowUpdateRPCTimeoutOrCancelledError."""
        return not workflow.all_handlers_finished()

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn(sandboxed=False)
class Follows:
    """Starts follow_session as DurableSession does, on a queue a test can stop."""

    @workflow.run
    async def run(self, args: FollowArgs, queue: str) -> str:
        result: FollowResult = await workflow.execute_activity(
            "follow_session",
            args,
            result_type=FollowResult,
            task_queue=queue,
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=100), maximum_interval=timedelta(seconds=1)
            ),
        )
        return result.ended


@workflow.defn(sandboxed=False)
class CancelsFollow:
    """Cancels its follower when told, as DurableSession does past FOLLOW_DRAIN."""

    def __init__(self) -> None:
        self._stop = False

    @workflow.signal
    def stop(self) -> None:
        self._stop = True

    @workflow.run
    async def run(self, args: FollowArgs, queue: str) -> str:
        follow = workflow.start_activity(
            "follow_session",
            args,
            task_queue=queue,
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=30),
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        await workflow.wait_condition(lambda: self._stop)
        follow.cancel()
        try:
            await follow
        except ActivityError as err:
            return type(err.cause).__name__
        return "completed"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@pytest.mark.requires_postgres
@pytest.mark.requires_temporal
async def test_a_stream_dropped_by_a_worker_shutdown_is_followed_again(
    agent_db: Conn, connect: object, temporal_env: WorkflowEnvironment
) -> None:
    """#4019: a rollout shut down the follower's worker mid-turn. Its subscription
    ended as if the workflow had closed, the activity completed, and nothing wrote the
    rest of the turn. It must fail instead, so a retry on another worker resumes from
    the offset: every event written once and in order, and the turn's end still ends it."""
    client = temporal_env.client
    sid = await insert_session(agent_db)
    streamed, queue, follower = f"session-{sid}", f"wf-{sid}", f"follow-{sid}"
    acts = SessionEvents(connect)  # type: ignore[arg-type]

    async def written() -> list[str]:
        return [e["delta"] for e in await events(agent_db, sid) if "delta" in e]

    async def wrote(*want: str) -> bool:
        return await written() == list(want)

    async with Worker(client, task_queue=queue, workflows=[Streamed, Follows]):
        stream = await client.start_workflow(Streamed.run, id=streamed, task_queue=queue)
        args = FollowArgs(session_id=sid, workflow_id=streamed, prompted=True, since=_now())
        follows = await client.start_workflow(
            Follows.run, args=[args, follower], id=f"follows-{sid}", task_queue=queue
        )
        first = Worker(client, task_queue=follower, activities=[acts.follow_session])
        running = asyncio.create_task(first.run())
        await stream.signal(Streamed.emit, {"type": "prompt", "text": "hi", "at": _now()})
        await stream.signal(Streamed.emit, {"type": "text", "text": "one", "at": _now()})
        await until(lambda: wrote("one"))
        # The subscriber waiting in its next poll, as an idle turn's is (a tool call
        # running): the shutdown's cancel ends the stream there, not in its cooldown.
        await until(lambda: stream.query(Streamed.polled))
        await first.shutdown()
        await running
        await stream.signal(Streamed.emit, {"type": "text", "text": "two", "at": _now()})
        async with Worker(client, task_queue=follower, activities=[acts.follow_session]):
            await stream.signal(Streamed.emit, {"type": "text", "text": "three", "at": _now()})
            await stream.signal(Streamed.emit, {"type": "done", "result": "x", "at": _now()})
            assert await asyncio.wait_for(follows.result(), 60) == "done"
        await stream.signal(Streamed.close)
        await stream.result()
    assert await written() == ["one", "two", "three"]
    # The first attempt failed as a stream that ended early, not as core's WorkerShutdown
    # (a cancel outside the poll), and the second ended the turn.
    history = await follows.fetch_history()
    [retried] = [
        e.activity_task_started_event_attributes
        for e in history.events
        if e.HasField("activity_task_started_event_attributes")
    ]
    assert retried.attempt == 2
    assert retried.last_failure.application_failure_info.type == "StreamEndedError"
    assert not retried.last_failure.application_failure_info.non_retryable


@pytest.mark.requires_postgres
@pytest.mark.requires_temporal
# The workflow closes with the subscriber's poll waiting: that is the case under test.
@pytest.mark.filterwarnings("ignore::temporalio.workflow.UnfinishedUpdateHandlersWarning")
async def test_a_stream_whose_workflow_closed_completes(
    agent_db: Conn, connect: object, temporal_env: WorkflowEnvironment
) -> None:
    """#4019: the stream ending because the workflow closed is still the end."""
    client = temporal_env.client
    sid = await insert_session(agent_db)
    streamed, queue = f"session-{sid}", f"wf-{sid}"
    env = ActivityEnvironment(client=client)
    acts = SessionEvents(connect)  # type: ignore[arg-type]

    async def wrote() -> bool:
        return [e["delta"] for e in await events(agent_db, sid) if "delta" in e] == ["bye"]

    async with Worker(client, task_queue=queue, workflows=[Streamed]):
        stream = await client.start_workflow(Streamed.run, id=streamed, task_queue=queue)
        follow = asyncio.create_task(
            env.run(
                acts.follow_session,
                FollowArgs(session_id=sid, workflow_id=streamed, prompted=True),
            )
        )
        await stream.signal(Streamed.emit, {"type": "text", "text": "bye", "at": _now()})
        await until(wrote)
        await stream.signal(Streamed.close)
        await stream.result()
        result = await asyncio.wait_for(follow, 60)
    assert result.ended == "closed"
    assert await wrote()


@pytest.mark.requires_postgres
async def test_a_retry_past_its_prompt_skips_an_earlier_turns_end(
    agent_db: Conn, connect: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4019: a retry resumes past its turn's prompt, so it tells an earlier turn's end,
    left unread, by its time: written, but the turn ends on its own end only."""
    sid = await insert_session(agent_db)
    since = "2026-10-17T12:00:00+00:00"
    stream = [
        {"type": "done", "result": "old", "at": "2026-10-17T11:59:58+00:00", "offset": 4},
        {"type": "text", "text": "hello", "at": "2026-10-17T12:00:03+00:00", "offset": 5},
        {"type": "done", "result": "hello", "at": "2026-10-17T12:00:04+00:00", "offset": 6},
    ]
    read: list[int] = []

    async def follow(*_: object, **__: object) -> AsyncIterator[dict[str, object]]:
        for e in stream:
            read.append(int(e["offset"]))  # type: ignore[call-overload]
            yield dict(e)

    monkeypatch.setattr(events_module, "follow_agent", follow)
    env = ActivityEnvironment(client=cast(Client, object()))
    env.info = dataclasses.replace(env.info, attempt=2)
    acts = SessionEvents(connect)  # type: ignore[arg-type]
    result = await env.run(
        acts.follow_session,
        FollowArgs(session_id=sid, workflow_id=f"session-{sid}", prompted=True, since=since),
    )
    assert result.ended == "done"
    assert read == [4, 5, 6]
    assert [e["type"] for e in await events(agent_db, sid)] == [
        "assistant.text.delta",
        "assistant.text.done",
    ]


@pytest.mark.requires_postgres
@pytest.mark.requires_temporal
# The stream's workflow closes with the next poll waiting, as in the closed case.
@pytest.mark.filterwarnings("ignore::temporalio.workflow.UnfinishedUpdateHandlersWarning")
async def test_the_workflows_own_cancel_is_a_cancel(
    agent_db: Conn, connect: object, temporal_env: WorkflowEnvironment
) -> None:
    """#4019: the workflow cancelling its follower (past FOLLOW_DRAIN) ends the stream as
    a shutdown does, in the poll; that is reported as cancelled, neither completed nor a
    failure to retry."""
    client = temporal_env.client
    sid = await insert_session(agent_db)
    streamed, queue, follower = f"session-{sid}", f"wf-{sid}", f"follow-{sid}"
    acts = SessionEvents(connect)  # type: ignore[arg-type]
    async with (
        Worker(client, task_queue=queue, workflows=[Streamed, CancelsFollow]),
        Worker(client, task_queue=follower, activities=[acts.follow_session]),
    ):
        stream = await client.start_workflow(Streamed.run, id=streamed, task_queue=queue)
        args = FollowArgs(session_id=sid, workflow_id=streamed, prompted=True, since=_now())
        cancels = await client.start_workflow(
            CancelsFollow.run, args=[args, follower], id=f"cancels-{sid}", task_queue=queue
        )
        await until(lambda: stream.query(Streamed.polled))
        await cancels.signal(CancelsFollow.stop)
        # The cancel reaches the activity with its next heartbeat (every 10 s).
        assert await asyncio.wait_for(cancels.result(), 60) == "CancelledError"
        await stream.signal(Streamed.close)
        await stream.result()
    history = await cancels.fetch_history()
    kinds = {e.WhichOneof("attributes") for e in history.events}
    assert "activity_task_canceled_event_attributes" in kinds
    assert "activity_task_completed_event_attributes" not in kinds
    assert "activity_task_failed_event_attributes" not in kinds
