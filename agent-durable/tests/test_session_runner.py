"""ScadBuddyRunner: the credential, the budget and the images, per segment (Rulings 2, 3, 5).

The scripted cases wrap the plugin's ScriptedClaude so the runner's own work is seen;
the last one runs the bundled Claude Code against a local fake Anthropic endpoint.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import ProcessError, ResultError
from fake_anthropic import FakeAnthropic
from session_support import (
    MANIFEST,
    Conn,
    Connect,
    StandInTools,
    events,
    insert_session,
    send,
    send_next,
    settled,
    start_session,
    status,
    tools_worker,
    until,
)
from temporalio.claude_agent_sdk import (
    ClaudeAgentSdkRunner,
    SegmentInput,
    SegmentOutput,
    ToolOutcome,
)
from temporalio.claude_agent_sdk.testing import Final, HistoryItem, ScriptedClaude, ToolCall
from temporalio.client import Client
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from scadbuddy_durable.codec import PgPayloadKeys, data_converter
from scadbuddy_durable.credentials import Credential, credential_aad
from scadbuddy_durable.gate.names import PENDING_INPUT_QUERY, RESPOND_UPDATE
from scadbuddy_durable.secrets import Kek, kek_from_base64, seal_bytes
from scadbuddy_durable.session import tools
from scadbuddy_durable.session.models import BUDGET_EXHAUSTED, ImageRef
from scadbuddy_durable.session.runner import (
    IMAGE_TOO_LARGE,
    IMAGES_SERVER,
    NO_CREDENTIAL,
    PLUGIN_REJECTION,
    VIEW_IMAGES_TOOL,
    VIEW_TOOL_IMAGES_TOOL,
    Image,
    ScadBuddyRunner,
    TurnSettings,
    image_problem,
    image_size,
    tool_images_loader,
    view_tool_images_tool,
    with_refusals,
)
from scadbuddy_durable.session.workflow import refusal
from scadbuddy_durable.worker import build_worker

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

KEK = kek_from_base64("WlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlo=")
TOKEN = "sk-test-gateway-token-0123456789"
# A 1x1 red PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


async def insert_gateway(
    conn: Conn, kek: Kek, base_url: str, secret: str = TOKEN, priority: int = 0
) -> None:
    cid = str(uuid.uuid4())
    aad = credential_aad(cid, "gateway", base_url)
    dek = os.urandom(32)
    await conn.execute(
        "INSERT INTO ai_credentials (id, priority, kind, base_url, secret_sealed, dek_sealed,"
        " kek_id, last4, status) VALUES (%s, %s, 'gateway', %s, %s, %s, %s, '', 'active')",
        (
            cid,
            priority,
            base_url,
            seal_bytes(dek, secret.encode(), aad),
            seal_bytes(kek.key, dek, f"dek:{aad}"),
            kek.id,
        ),
    )


class Recording:
    """make_runner for the runner: records what each segment's runner was given."""

    def __init__(self) -> None:
        self.made: list[dict[str, Any]] = []
        self.scripted = ScriptedClaude(lambda prompt, _: Final("done"), cost_per_segment=0.25)

    def __call__(self, **kwargs: Any) -> Any:
        self.made.append(kwargs)
        return self.scripted


async def _client(env: WorkflowEnvironment, connect: Connect) -> Client:
    return await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=data_converter(PgPayloadKeys(connect, KEK)),
    )


@contextlib.asynccontextmanager
async def running(
    env: WorkflowEnvironment, connect: Connect, runner: Any
) -> AsyncIterator[tuple[Client, str]]:
    tools.use_manifest(MANIFEST)
    client = await _client(env, connect)
    queue = f"agent-{uuid.uuid4().hex[:8]}"
    async with (
        build_worker(client, connect, runner, task_queue=queue),
        tools_worker(client, StandInTools(connect)),
    ):
        yield client, queue


async def _row(conn: Conn, sid: str) -> tuple[float, int]:
    cur = await conn.execute("SELECT cost_usd, turns FROM ai_sessions WHERE id = %s", (sid,))
    row = await cur.fetchone()
    assert row is not None
    return float(row[0]), int(row[1])


async def test_a_segment_runs_with_the_credential_and_what_is_left_of_the_budget(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    await insert_gateway(agent_db, KEK, "http://127.0.0.1:9")
    recording = Recording()
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=recording)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db, budget=1.0)
        await agent_db.execute("UPDATE ai_sessions SET cost_usd = 0.4 WHERE id = %s", (sid,))
        handle = await start_session(client, sid, queue, "Be careful.")
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    [made] = recording.made
    assert made["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    assert made["env"]["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert made["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert made["max_budget_usd"] == pytest.approx(0.6)
    assert made["env"]["SCADBUDDY_TEST_DATABASE_URL"] == ""  # the worker's settings, emptied
    # No images, no skills: Claude Code's prompt with the session's append, and the
    # images server offering view_tool_images only (#2320).
    extra = dict(made["extra_options"])
    assert set(extra.pop("mcp_servers")) == {IMAGES_SERVER}
    assert extra == {
        "system_prompt": {"type": "preset", "preset": "claude_code", "append": "Be careful."},
        "allowed_tools": [VIEW_TOOL_IMAGES_TOOL],
    }
    cost, turns = await _row(agent_db, sid)
    assert cost == pytest.approx(0.65)
    assert turns == 1
    result = [e for e in await events(agent_db, sid) if e["type"] == "session.result"][-1]
    assert result["costUsd"] == pytest.approx(0.65)
    assert result["budgetUsd"] == 1.0
    # The credential is in no history the server holds.
    raw = b"".join(e.SerializeToString() for e in (await handle.fetch_history()).events)
    assert TOKEN.encode() not in raw


async def test_a_spent_budget_ends_the_turn_without_a_model_call(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    await insert_gateway(agent_db, KEK, "http://127.0.0.1:9")
    recording = Recording()
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=recording)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db, budget=1.0)
        await agent_db.execute("UPDATE ai_sessions SET cost_usd = 1.0 WHERE id = %s", (sid,))
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    assert recording.made == []
    [error] = [e for e in await events(agent_db, sid) if e["type"] == "error"]
    assert error["code"] == "error_max_budget_usd"
    assert error["message"] == "this chat used its $1.00 budget ($1.00 spent)"


async def test_no_usable_credential_fails_the_turn(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    recording = Recording()
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=recording)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "failed"
        finally:
            await handle.terminate()
    [error] = [e for e in await events(agent_db, sid) if e["type"] == "error"]
    assert error["code"] == "turn_failed"
    assert NO_CREDENTIAL in error["message"]


async def test_a_classic_session_is_refused(
    agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    sid = await insert_session(agent_db)
    await agent_db.execute("UPDATE ai_sessions SET mode = 'classic' WHERE id = %s", (sid,))
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=Recording())
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, workflow_id=f"session-{sid}")

    async def run() -> SegmentOutput:
        return await runner.run(SegmentInput(session_id="", prompt="hi", tools=[]), 1)

    out = await env.run(run)
    assert out.is_error
    assert out.error == "this workflow is no durable ScadBuddy session"


def _has_image(body: dict[str, Any]) -> bool:
    for message in body.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_result":
                continue
            for inner in block.get("content") or []:
                if isinstance(inner, dict) and inner.get("type") == "image":
                    return True
    return False


def _image_data(body: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for message in body.get("messages", []):
        for block in message.get("content") if isinstance(message.get("content"), list) else []:
            for inner in block.get("content") or [] if block.get("type") == "tool_result" else []:
                if isinstance(inner, dict) and inner.get("type") == "image":
                    found.append(inner["source"]["data"])
    return found


async def test_a_turn_with_an_image_gets_it_from_view_user_images(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling 5, end to end: the bundled Claude Code, a fake endpoint, the image blocks."""

    def reply(body: dict[str, Any]) -> dict[str, Any]:
        names = [t.get("name") for t in body.get("tools", [])]
        if VIEW_IMAGES_TOOL not in names:
            return {"text": "ok"}  # Claude Code's own side requests
        if _has_image(body):
            return {"text": "I see a red square."}
        return {"tool_use": {"name": VIEW_IMAGES_TOOL, "input": {}}}

    fake = FakeAnthropic(reply).start()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    try:
        await insert_gateway(agent_db, KEK, fake.url)
        runner = ScadBuddyRunner(
            connect, KEK, cwd=os.path.realpath(cwd), make_runner=ClaudeAgentSdkRunner
        )
        async with running(temporal_env, connect, runner) as (client, queue):
            sid = await insert_session(agent_db)
            name = hashlib.sha256(PNG).hexdigest() + ".png"
            await agent_db.execute(
                "INSERT INTO ai_session_blobs (session_id, name, media_type, data)"
                " VALUES (%s, %s, 'image/png', %s)",
                (sid, name, PNG),
            )
            handle = await start_session(client, sid, queue)
            try:
                await send(handle, "what colour is it?", [ImageRef(name, "image/png")])
                assert await settled(connect, sid, timeout=180) == "idle"
            finally:
                await handle.terminate()
        log = await events(agent_db, sid)
        text = "".join(e["delta"] for e in log if e["type"] == "assistant.text.delta")
        assert "I see a red square." in text
        bodies = [b for b in fake.messages() if VIEW_IMAGES_TOOL in str(b.get("tools"))]
        assert "view_user_images" in str(bodies[0]["messages"])  # the prompt asks for it
        assert base64.b64encode(PNG).decode() in [d for b in bodies for d in _image_data(b)]
        # The gateway token reached the endpoint, and no history holds it.
        raw = b"".join(e.SerializeToString() for e in (await handle.fetch_history()).events)
        assert TOKEN.encode() not in raw
    finally:
        fake.close()
        await asyncio.sleep(0)


async def test_a_turn_that_runs_out_mid_segment_says_so_and_a_raise_lets_the_next_run(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bundled Claude Code stops at ``max_budget_usd`` inside a segment: the turn ends
    as the budget's (``error_max_budget_usd``, the code the panel's budget card reads,
    frontend agent/chat/state.ts BUDGET_CODES), not as a failure. A raise (what
    manager.ts raiseBudget writes) is read by the next segment, whose turn then runs."""
    raised = False

    def reply(body: dict[str, Any]) -> dict[str, Any]:
        if VIEW_TOOL_IMAGES_TOOL not in str(body.get("tools")):
            return {"text": "ok"}  # Claude Code's own side requests
        if raised:
            return {"text": "back again"}
        # A million input tokens: more than the $1 budget, so Claude Code stops
        # before the call this tool_use would need next.
        return {"tool_use": {"name": "no_such_tool", "input": {}}, "input_tokens": 1_000_000}

    fake = FakeAnthropic(reply).start()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    try:
        await insert_gateway(agent_db, KEK, fake.url)
        runner = ScadBuddyRunner(
            connect, KEK, cwd=os.path.realpath(cwd), make_runner=ClaudeAgentSdkRunner
        )
        async with running(temporal_env, connect, runner) as (client, queue):
            sid = await insert_session(agent_db, budget=1.0)
            handle = await start_session(client, sid, queue)
            try:
                await send(handle, "go")
                assert await settled(connect, sid, timeout=180) == "idle"
                [error] = [e for e in await events(agent_db, sid) if e["type"] == "error"]
                assert error["code"] == "error_max_budget_usd"
                # The plugin reports no cost for it: charged at least the cap.
                spent, _ = await _row(agent_db, sid)
                assert spent == pytest.approx(1.0)

                # The agent's claim (durable.ts send) refuses this send while spent; were
                # one to reach the workflow anyway, it ends at once, with no model call.
                calls = len(fake.messages())
                await agent_db.execute(
                    "UPDATE ai_sessions SET status = 'running' WHERE id = %s", (sid,)
                )
                await send_next(handle, "go on")
                assert await settled(connect, sid) == "idle"
                assert len(fake.messages()) == calls

                await agent_db.execute(
                    "UPDATE ai_sessions SET budget_usd = %s, status = 'running' WHERE id = %s",
                    (spent + 5, sid),
                )
                raised = True
                await send_next(handle, "go on again")
                assert await settled(connect, sid, timeout=180) == "idle"
            finally:
                await handle.terminate()
        log = await events(agent_db, sid)
        assert [e["code"] for e in log if e["type"] == "error"] == ["error_max_budget_usd"] * 2
        text = "".join(e["delta"] for e in log if e["type"] == "assistant.text.delta")
        assert "back again" in text
        result = [e for e in log if e["type"] == "session.result"][-1]
        assert result["budgetUsd"] == pytest.approx(spent + 5)
    finally:
        fake.close()
        await asyncio.sleep(0)


class Failing:
    """make_runner for the runner: each segment raises the next of ``errors``, then the
    scripted model answers."""

    def __init__(self, *errors: BaseException) -> None:
        self.errors = list(errors)
        self.calls = 0
        self.scripted = ScriptedClaude(lambda prompt, _: Final("done"))

    def __call__(self, **kwargs: Any) -> Any:
        return self

    async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return await self.scripted.run(inp, attempt)


def _result_error(text: str, status: int | None = None) -> ResultError:
    data: dict[str, Any] = {"subtype": "success", "is_error": True, "result": text}
    if status is not None:
        data["api_error_status"] = status
    err = ResultError(f"Claude Code returned an error result: {text}", data=data, exit_code=1)
    err.__cause__ = ProcessError("Command failed with exit code 1", 1, "Check stderr output")
    return err


async def _turn(
    env: WorkflowEnvironment, conn: Conn, connect: Connect, cwd: Path, failing: Failing
) -> tuple[str, Any]:
    await insert_gateway(conn, KEK, "http://127.0.0.1:9")
    runner = ScadBuddyRunner(connect, KEK, cwd=str(cwd), make_runner=failing)
    async with running(env, connect, runner) as (client, queue):
        sid = await insert_session(conn)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            await settled(connect, sid)
            history = await handle.fetch_history()
        finally:
            await handle.terminate()
    return sid, history


@pytest.mark.parametrize(
    "error",
    [
        _result_error("Prompt is too long"),
        _result_error("API Error: 413", status=413),
        ProcessError("the request is too large for the model", 1, "x" * 3_000_000),
    ],
)
async def test_a_failure_no_retry_fixes_ends_the_turn_once(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    error: BaseException,
) -> None:
    """#2243: an oversized request looped for ever, 100 s apart; it now ends the turn."""
    failing = Failing(error, error, error)
    sid, _ = await _turn(temporal_env, agent_db, connect, tmp_path, failing)
    assert failing.calls == 1
    assert await status(agent_db, sid) == "failed"
    [event] = [e for e in await events(agent_db, sid) if e["type"] == "error"]
    assert event["code"] == "turn_failed"
    assert len(event["message"]) <= 500
    assert str(error).splitlines()[0][:40] in event["message"]


async def test_a_retried_failure_reaches_temporal_bounded(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """A transient failure is retried, and what the server keeps of it is readable: 3 MB of
    stderr once came back as only "Failure exceeds size limit."."""
    huge = ProcessError("Command failed with exit code 1", 1, "overloaded\n" + "x" * 3_000_000)
    huge.__cause__ = RuntimeError("y" * 100_000)
    failing = Failing(huge)
    sid, history = await _turn(temporal_env, agent_db, connect, tmp_path, failing)
    assert failing.calls == 2
    assert await status(agent_db, sid) == "idle"
    [started] = [
        e.activity_task_started_event_attributes
        for e in history.events
        if e.HasField("activity_task_started_event_attributes")
        and e.activity_task_started_event_attributes.attempt == 2
    ]
    failure = started.last_failure
    assert failure.ByteSize() < 4096  # limit.mutableStateActivityFailureSize.error
    assert failure.message != "Failure exceeds size limit."
    assert failure.application_failure_info.type == "ProcessError"
    assert not failure.application_failure_info.non_retryable
    assert not failure.HasField("cause")


def _png(width: int, height: int) -> bytes:
    """A PNG's signature and IHDR: all image_size reads."""
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


def test_image_sizes_are_read_from_the_header() -> None:
    assert image_size(PNG) == (1, 1)
    assert image_size(_png(4000, 300)) == (4000, 300)
    assert image_size(b"GIF89a" + (640).to_bytes(2, "little") + (480).to_bytes(2, "little")) == (
        640,
        480,
    )
    jpeg = b"\xff\xd8\xff\xe0\x00\x04xx\xff\xc0\x00\x11\x08" + (300).to_bytes(2, "big")
    assert image_size(jpeg + (500).to_bytes(2, "big") + b"\x03") == (500, 300)
    webp = b"RIFF\x00\x00\x00\x00WEBPVP8X" + bytes(8) + (799).to_bytes(3, "little")
    assert image_size(webp + (599).to_bytes(3, "little")) == (800, 600)
    assert image_size(b"not an image") is None


def test_image_problems() -> None:
    ok = Image("a.png", "image/png", _png(1568, 900))
    assert image_problem([ok], 1568) is None
    assert IMAGE_TOO_LARGE in (
        image_problem([Image("b.png", "image/png", _png(4000, 3000))], 1568) or ""
    )
    assert "at most 4" in (image_problem([ok] * 5, 1568) or "")
    huge = Image("c.png", "image/png", _png(10, 10) + bytes(4 * 1024 * 1024))
    assert "over 3.75 MB" in (image_problem([huge], 1568) or "")
    assert "cannot be read" in (image_problem([Image("d.png", "image/png", b"xx")], 1568) or "")


async def test_an_image_over_the_long_edge_ends_the_turn_before_any_model_call(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#2243: an image nothing scaled made a request Claude refused, again on every retry."""
    await insert_gateway(agent_db, KEK, "http://127.0.0.1:9")
    await agent_db.execute(
        "INSERT INTO ai_settings (key, value) VALUES ('image_long_edge', '1000')"
    )
    recording = Recording()
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=recording)
    big = _png(1200, 800)
    name = hashlib.sha256(big).hexdigest() + ".png"
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        await agent_db.execute(
            "INSERT INTO ai_session_blobs (session_id, name, media_type, data)"
            " VALUES (%s, %s, 'image/png', %s)",
            (sid, name, big),
        )
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "look", [ImageRef(name, "image/png")])
            assert await settled(connect, sid) == "failed"
        finally:
            await handle.terminate()
    assert recording.made == []
    [event] = [e for e in await events(agent_db, sid) if e["type"] == "error"]
    assert "input too large" in event["message"]
    assert "1200x800" in event["message"] and "1000 px" in event["message"]


async def _segment_options(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    *,
    plugin_dir: str | None = None,
) -> dict[str, Any]:
    await insert_gateway(agent_db, KEK, "http://127.0.0.1:9")
    recording = Recording()
    runner = ScadBuddyRunner(
        connect, KEK, cwd=str(tmp_path), plugin_dir=plugin_dir, make_runner=recording
    )
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    [made] = recording.made
    return made


async def test_each_turn_runs_with_the_append_it_was_sent_with(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#2525: Settings changed between turns (the headless browser turned on, say) reach
    the next turn's prompt; a turn sent without an append keeps the session's first."""
    await insert_gateway(agent_db, KEK, "http://127.0.0.1:9")
    recording = Recording()
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=recording)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue, system_append="no browser")
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
            await agent_db.execute(
                "UPDATE ai_sessions SET status = 'running' WHERE id = %s", (sid,)
            )
            await send_next(handle, "again", system_append="a headless browser")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    appends = [m["extra_options"]["system_prompt"].get("append") for m in recording.made]
    assert appends == ["no browser", "a headless browser"]


async def _setting(conn: Conn, key: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO ai_settings (key, value) VALUES (%s, %s::jsonb)"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )


async def test_a_segment_runs_on_the_model_chosen_in_settings(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    await _setting(agent_db, "model", '"claude-sonnet-test"')
    made = await _segment_options(temporal_env, agent_db, connect, tmp_path)
    assert made["model"] == "claude-sonnet-test"


async def test_no_model_setting_leaves_the_engine_default(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    await _setting(agent_db, "model", '""')
    made = await _segment_options(temporal_env, agent_db, connect, tmp_path)
    assert "model" not in made


async def test_the_own_plugin_loads_unless_settings_switched_it_off(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    on = await _segment_options(temporal_env, agent_db, connect, tmp_path, plugin_dir="/skills")
    assert on["extra_options"]["plugins"] == [{"type": "local", "path": "/skills"}]
    await _setting(agent_db, "scadbuddy_plugin_enabled", "false")
    await agent_db.execute("DELETE FROM ai_credentials")
    off = await _segment_options(temporal_env, agent_db, connect, tmp_path, plugin_dir="/skills")
    assert "plugins" not in off["extra_options"]


class Refusing:
    """make_runner for the runner: a segment on a credential in ``refused`` raises
    ``error``; any other answers. Records each runner's token."""

    def __init__(self, refused: set[str], error: BaseException) -> None:
        self.refused = refused
        self.error = error
        self.tokens: list[str] = []
        self.scripted = ScriptedClaude(lambda prompt, _: Final("done"), cost_per_segment=0.25)

    def __call__(self, **kwargs: Any) -> Any:
        token = kwargs["env"]["ANTHROPIC_AUTH_TOKEN"]
        self.tokens.append(token)
        if token in self.refused:
            error = self.error

            class Raises:
                async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput:
                    raise error

            return Raises()
        return self.scripted


async def _two_credentials(conn: Conn) -> None:
    await insert_gateway(conn, KEK, "http://127.0.0.1:9", secret=TOKEN)
    await insert_gateway(conn, KEK, "http://127.0.0.1:10", secret=TOKEN + "-second", priority=1)


@pytest.mark.parametrize(
    "error",
    [
        _result_error("API Error: 401 invalid x-api-key", status=401),
        _result_error("API Error: 429 rate limited", status=429),
        _result_error("API Error: 400 Your credit balance is too low", status=400),
    ],
)
async def test_a_refused_credential_falls_back_to_the_next(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    error: BaseException,
) -> None:
    """As a classic turn does (harness/fallback.ts): the credential's own failure moves
    the segment to the next credential, once each, instead of retrying the same key."""
    await _two_credentials(agent_db)
    refusing = Refusing({TOKEN}, error)
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=refusing)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    assert refusing.tokens == [TOKEN, TOKEN + "-second"]


async def test_a_failure_of_the_request_does_not_try_another_credential(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    await _two_credentials(agent_db)
    refusing = Refusing({TOKEN}, _result_error("API Error: 400 bad tool schema", status=400))
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=refusing)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            # The segment is retried (on the same key, as the plugin retries any failure).
            for _ in range(100):
                if len(refusing.tokens) >= 2:
                    break
                await asyncio.sleep(0.1)
        finally:
            await handle.terminate()
    assert len(refusing.tokens) >= 2
    assert set(refusing.tokens) == {TOKEN}


async def test_a_fallback_never_runs_past_the_budget_a_refused_attempt_spent(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """What the refused attempt cost is spent: with nothing left, no other credential
    runs, and the turn ends as the budget's (security review of the fallback)."""
    await _two_credentials(agent_db)
    error = _result_error("API Error: 429 rate limited", status=429)
    error.data["total_cost_usd"] = 1.0
    refusing = Refusing({TOKEN}, error)
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=refusing)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db, budget=1.0)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    assert refusing.tokens == [TOKEN]
    [event] = [e for e in await events(agent_db, sid) if e["type"] == "error"]
    assert event["code"] == "error_max_budget_usd"
    cost, _ = await _row(agent_db, sid)
    assert cost == pytest.approx(1.0)


async def test_view_tool_images_returns_this_sessions_blobs_as_images(
    agent_db: Conn, connect: Connect
) -> None:
    """#2320: an image a durable tool returned is kept by the agent and named in the
    result; view_tool_images hands it to the model as an image block, as a classic tool
    result is, and never another session's."""
    sid = await insert_session(agent_db)
    other = await insert_session(agent_db)
    name = hashlib.sha256(PNG).hexdigest() + ".png"
    await agent_db.execute(
        "INSERT INTO ai_session_blobs (session_id, name, media_type, data)"
        " VALUES (%s, %s, 'image/png', %s)",
        (other, name, PNG),
    )
    view = view_tool_images_tool(tool_images_loader(connect, sid))
    missing = await view.handler({"names": [name]})
    assert missing.get("is_error") is True
    assert name in missing["content"][0]["text"]
    await agent_db.execute(
        "INSERT INTO ai_session_blobs (session_id, name, media_type, data)"
        " VALUES (%s, %s, 'image/png', %s)",
        (sid, name, PNG),
    )
    shown = await view.handler({"names": [name]})
    assert shown["content"] == [
        {"type": "image", "data": base64.b64encode(PNG).decode(), "mimeType": "image/png"}
    ]
    # A name that is not a blob's is never looked up; more than four is refused.
    bad = await view.handler({"names": ["../etc/passwd"]})
    assert bad.get("is_error") is True
    many = await view.handler({"names": [name] * 5})
    assert many.get("is_error") is True


def test_a_refusal_is_said_as_a_classic_turn_says_it() -> None:
    """#2392: agent/src/approvals/service.ts ``refusal``, word for word."""
    what = "print_output (approval durable:s:r:t)"
    assert refusal("print_output", "durable:s:r:t", "denied", None) == (
        f"The user denied {what}; it was not run. Do not retry it unless the user asks you to."
    )
    assert refusal("print_output", "durable:s:r:t", "expired", None) == (
        f"Nobody decided on {what} before it expired, so it was not run. "
        "Tell the user it needs their approval."
    )
    assert refusal("print_output", "durable:s:r:t", "cancelled", "the turn was stopped") == (
        f"The approval for {what} was cancelled (the turn was stopped), so it was not run."
    )
    assert refusal("print_output", "durable:s:r:t", "withdrawn", None) == (
        f"The approval for {what} was withdrawn before it ran (no reason given), so it was not run."
    )


def test_only_the_plugins_rejection_is_added_to_and_it_stays_an_error() -> None:
    injected: dict[str, Any] = {
        "t1": ToolOutcome(content=PLUGIN_REJECTION, is_error=True),
        "t2": {"content": PLUGIN_REJECTION, "is_error": True},
        "t3": ToolOutcome(content="printed", is_error=False),
    }
    out = with_refusals(injected, {"t1": "why 1", "t2": "why 2", "t3": "why 3", "t4": "gone"})
    assert out["t1"] == ToolOutcome(content=f"{PLUGIN_REJECTION}\n\nwhy 1", is_error=True)
    assert out["t2"] == {"content": f"{PLUGIN_REJECTION}\n\nwhy 2", "is_error": True}
    assert out["t3"] == injected["t3"]
    assert set(out) == {"t1", "t2", "t3"}


class Delivered(ScriptedClaude):
    """A scripted model that records the results each segment delivers."""

    def __init__(self) -> None:
        def policy(prompt: str, history: list[HistoryItem]) -> ToolCall | Final:
            if history:
                return Final("told")
            return ToolCall("print_output", {"output_id": "o1"})

        super().__init__(policy)
        self.injected: list[dict[str, Any]] = []

    async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput:
        self.injected.append(dict(inp.injected))
        return await super().run(inp, attempt)


async def test_a_denied_call_is_followed_by_why_in_a_classic_turns_words(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#2392: the plugin's decide(False) and its fixed rejection stay; the model is also
    told the user denied the call, as a classic turn tells it."""
    await insert_gateway(agent_db, KEK, "http://127.0.0.1:9")
    model = Delivered()
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=lambda **_: model)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "print it")
            entries: list[dict[str, Any]] = []

            async def parked() -> bool:
                nonlocal entries
                entries = await handle.query(PENDING_INPUT_QUERY)
                return bool(entries)

            await until(parked)
            [entry] = entries
            await handle.execute_update(
                RESPOND_UPDATE,
                {
                    "request_id": entry["id"],
                    "response": {"kind": "approval", "decision": "deny"},
                    "responder": {"kind": "browser", "id": "browser", "label": "You"},
                    "role": "browser",
                },
            )
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    [result] = [r for seg in model.injected for r in seg.values()]
    content = result.content if isinstance(result, ToolOutcome) else result["content"]
    is_error = result.is_error if isinstance(result, ToolOutcome) else result["is_error"]
    assert content == (
        f"{PLUGIN_REJECTION}\n\n" + refusal("print_output", entry["id"], "denied", None)
    )
    assert is_error is True


class _Answers:
    """make_runner whose segment answers ``out``, whatever it is asked."""

    def __init__(self, out: SegmentOutput) -> None:
        self.out = out
        self.budgets: list[float] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.budgets.append(kwargs["max_budget_usd"])
        out = self.out

        class Answers:
            async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput:
                return out

        return Answers()


def _credential() -> Credential:
    return Credential(id="c1", priority=0, kind="gateway", base_url="http://x", secret=TOKEN)


async def _segment(out: SegmentOutput, left: float) -> SegmentOutput:
    answers = _Answers(out)
    runner = ScadBuddyRunner(cast(Any, None), KEK, cwd="/tmp", make_runner=answers)
    settings = TurnSettings(long_edge=1568, model=None, own_plugin=False)
    got = await runner._on_credentials(
        "s", "wf", cast(Any, None), 1, [_credential()], left, {}, settings
    )
    assert answers.budgets == [left]
    return got


@pytest.mark.parametrize(("cost", "charged"), [(0.25, 1.0), (0.0, 1.0), (1.5, 1.5)])
async def test_a_segment_that_reached_its_cap_is_charged_at_least_what_was_left(
    cost: float, charged: float
) -> None:
    """#2675: Claude Code stops once the cost reached the cap, so the segment is charged
    `max(cost_usd, left)`: what was left when its own report is lower, its report when
    higher. Either way the turn ends as the budget's."""
    out = SegmentOutput(
        session_id="s", is_error=True, error="Reached maximum budget ($1.00)", cost_usd=cost
    )
    got = await _segment(out, left=1.0)
    assert got.error == BUDGET_EXHAUSTED
    assert got.cost_usd == pytest.approx(charged)


@pytest.mark.parametrize(
    "text",
    [
        "the tool said error_max_budget_usd somewhere in its output",
        "Quoting: Reached maximum budget is what it says",
    ],
)
async def test_an_error_that_only_quotes_the_budget_text_is_not_the_cap(text: str) -> None:
    """#2675: the error text is session content. Only Claude Code's own message,
    "Reached maximum budget ($N)", is the cap; a result that quotes the subtype or the
    words is the error it is, charged what it reported."""
    out = SegmentOutput(session_id="s", is_error=True, error=text, cost_usd=0.1)
    got = await _segment(out, left=1.0)
    assert got.error == text
    assert got.cost_usd == pytest.approx(0.1)
