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
import json
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from claude_agent_sdk import HookMatcher, ProcessError, ResultError
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
from temporalio.claude_agent_sdk._events import _EMIT, emit
from temporalio.claude_agent_sdk.testing import Final, HistoryItem, ScriptedClaude, ToolCall
from temporalio.client import Client, WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from scadbuddy_durable.codec import PgPayloadKeys, data_converter
from scadbuddy_durable.credentials import Credential, credential_aad
from scadbuddy_durable.gate.names import PENDING_INPUT_QUERY, RESPOND_UPDATE
from scadbuddy_durable.payload_store import OFFLOAD_BYTES, external_storage
from scadbuddy_durable.secrets import Kek, kek_from_base64, seal_bytes
from scadbuddy_durable.session import access, models, tools
from scadbuddy_durable.session import runner as runner_module
from scadbuddy_durable.session.events import translate
from scadbuddy_durable.session.models import (
    BUDGET_EXHAUSTED,
    BUILTIN_CALL_INPUT_MAX,
    BUILTIN_CALL_UPDATE,
    BUILTIN_RELEASE_UPDATE,
    PARK_DEADLINE_S,
    SEGMENT_TIMEOUT,
    ImageRef,
    TurnPackage,
)
from scadbuddy_durable.session.package_hash import hash_tree
from scadbuddy_durable.session.runner import (
    DURABLE_PREFIX,
    IMAGE_TOO_LARGE,
    IMAGES_SERVER,
    NO_CREDENTIAL,
    PLUGIN_REJECTION,
    VIEW_IMAGES_TOOL,
    VIEW_TOOL_IMAGES_TOOL,
    CallAccess,
    HeldText,
    Image,
    ScadBuddyRunner,
    SegmentCalls,
    TurnSettings,
    call_builtins_of,
    call_plugins_of,
    image_problem,
    image_size,
    merged_hooks,
    own_call_plugin,
    own_plugin_copy,
    same_subagent,
    segment_gate,
    sweep_own_plugin_copies,
    tool_images_loader,
    view_tool_images_tool,
    with_refusals,
)
from scadbuddy_durable.session.workflow import _tool_refusal, refusal
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


async def _client(
    env: WorkflowEnvironment, connect: Connect, payload_dir: Path | None = None
) -> Client:
    """The worker's client; with ``payload_dir``, its External Storage too, as the worker
    has whenever SCADBUDDY_PAYLOAD_DIR is set (worker.py ``connect_client``)."""
    return await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=data_converter(
            PgPayloadKeys(connect, KEK), external_storage(payload_dir) if payload_dir else None
        ),
    )


@contextlib.asynccontextmanager
async def running(
    env: WorkflowEnvironment,
    connect: Connect,
    runner: Any,
    stand_in: StandInTools | None = None,
    payload_dir: Path | None = None,
) -> AsyncIterator[tuple[Client, str]]:
    tools.use_manifest(MANIFEST)
    client = await _client(env, connect, payload_dir)
    queue = f"agent-{uuid.uuid4().hex[:8]}"
    async with (
        build_worker(client, connect, runner, task_queue=queue),
        tools_worker(client, stand_in or StandInTools(connect)),
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
    # A limit's "resets 10:30pm (UTC)" is named in the engine's zone (#4107).
    assert made["env"]["TZ"] == "UTC"
    assert made["max_budget_usd"] == pytest.approx(0.6)
    assert made["env"]["SCADBUDDY_TEST_DATABASE_URL"] == ""  # the worker's settings, emptied
    # No images, no skills: Claude Code's prompt with the session's append, and the
    # images server offering view_tool_images only (#2320).
    extra = dict(made["extra_options"])
    assert set(extra.pop("mcp_servers")) == {IMAGES_SERVER}
    # The segment's own calls, published as they run (SegmentCalls).
    assert set(extra.pop("hooks")) == {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SubagentStart",
    }
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
    agent: tuple[str | None, str | None, str | None] | None = None,
    agents_dir: str | None = None,
) -> dict[str, Any]:
    await insert_gateway(agent_db, KEK, "http://127.0.0.1:9")
    recording = Recording()
    runner = ScadBuddyRunner(
        connect,
        KEK,
        cwd=str(tmp_path),
        plugin_dir=plugin_dir,
        agents_dir=agents_dir,
        own_plugin_root=str(tmp_path / "own"),
        make_runner=recording,
    )
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        if agent:
            await agent_db.execute(
                "UPDATE ai_sessions SET agent_model = %s, agent_effort = %s, agent_type = %s"
                " WHERE id = %s",
                (*agent, sid),
            )
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


async def test_the_built_ins_the_agent_provides_are_named_in_the_append(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#2393: what the turn's listing says to tell the model (the package built-ins it
    gets as turn tools) follows the turn's append, or stands alone without one."""
    await insert_gateway(agent_db, KEK, "http://127.0.0.1:9")
    recording = Recording()
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=recording)
    stand_in = StandInTools(connect)
    stand_in.turn_note = "Read is builtin__Read."
    async with running(temporal_env, connect, runner, stand_in) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue, system_append="Be careful.")
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    appends = [m["extra_options"]["system_prompt"].get("append") for m in recording.made]
    assert appends == ["Be careful.\n\nRead is builtin__Read.", "Read is builtin__Read."]


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


async def test_a_workflow_agents_chat_runs_on_its_own_model_and_effort(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#2393: agent() options kept on its chat (workflowAgents.ts) win over Settings'; a
    subagent type with no definitions here is not passed on."""
    await _setting(agent_db, "model", '"claude-sonnet-test"')
    made = await _segment_options(
        temporal_env, agent_db, connect, tmp_path, agent=("claude-haiku-test", "low", "nope")
    )
    assert made["model"] == "claude-haiku-test"
    assert made["extra_options"]["effort"] == "low"
    assert "extra_args" not in made["extra_options"]


async def test_no_model_setting_leaves_the_engine_default(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    await _setting(agent_db, "model", '""')
    made = await _segment_options(temporal_env, agent_db, connect, tmp_path)
    assert "model" not in made


async def test_the_own_plugin_off_in_ai_access_is_not_loaded(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#2171: the own plugin's switch is its ai_access row (builtin:own-plugin, item '*'),
    as a classic turn reads it. A stray old ai_settings key does not decide (#4828)."""
    await _setting(agent_db, "scadbuddy_plugin_enabled", "false")
    on = await _segment_options(temporal_env, agent_db, connect, tmp_path, plugin_dir="/skills")
    assert on["extra_options"]["plugins"] == [{"type": "local", "path": "/skills"}]
    await agent_db.execute(
        "INSERT INTO ai_access (source, item, access, updated_by)"
        " VALUES ('builtin:own-plugin', '*', 'off', 'browser:browser')"
    )
    await agent_db.execute("DELETE FROM ai_credentials")
    off = await _segment_options(temporal_env, agent_db, connect, tmp_path, plugin_dir="/skills")
    assert "plugins" not in off["extra_options"]


class Refusing:
    """make_runner for the runner: a segment on a credential in ``refused`` raises
    ``error``; any other answers. Records each runner's token. With ``scripted``, the
    refused segment says ``said`` through the plugin's ScriptedClaude, not this test's
    own emit() call."""

    def __init__(
        self,
        refused: set[str],
        error: BaseException,
        said: str | None = None,
        *,
        scripted: bool = False,
    ) -> None:
        if scripted and said is None:
            # Without it the plain Raises runner would run, not the path the test names (#3899).
            raise ValueError("Refusing(scripted=True) needs said")
        self.refused = refused
        self.error = error
        self.said = said
        self.said_via_scripted = scripted
        self.tokens: list[str] = []
        self.scripted = ScriptedClaude(lambda prompt, _: Final("done"), cost_per_segment=0.25)

    def __call__(self, **kwargs: Any) -> Any:
        token = kwargs["env"]["ANTHROPIC_AUTH_TOKEN"]
        self.tokens.append(token)
        if token in self.refused:
            error, said = self.error, self.said
            if self.said_via_scripted and said is not None:
                return RefusesAfterSaying(said, error)

            class Raises:
                async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput:
                    if said is not None:
                        # As Claude Code answers a refused request: the notice, then the error.
                        emit({"type": "text", "text": said})
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


async def test_a_refused_credentials_notice_never_reaches_the_transcript(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#2945: the refused attempt's "session limit" text is dropped; the turn's answer,
    on the next credential, is what the session log holds."""
    await _two_credentials(agent_db)
    notice = "You've hit your session limit · resets 6pm (UTC)"
    refusing = Refusing(
        {TOKEN}, _result_error("API Error: 429 rate limited", status=429), said=notice
    )
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=refusing)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    texts = [e["delta"] for e in await events(agent_db, sid) if e["type"] == "assistant.text.delta"]
    assert texts == ["done"]
    assert refusing.tokens == [TOKEN, TOKEN + "-second"]


class RefusesAfterSaying(ScriptedClaude):
    """A scripted model whose answer is the refusal notice, published through the
    plugin's own ``emit`` (as its runner publishes each ``TextBlock``), and which then
    fails the way a refused credential does."""

    def __init__(self, notice: str, error: BaseException) -> None:
        super().__init__(lambda prompt, _: Final(notice))
        self.error = error

    async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput:
        await super().run(inp, attempt)
        raise self.error


async def test_a_refused_credentials_scripted_notice_never_reaches_the_transcript(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#3383: the same as above with the notice published by the plugin's ScriptedClaude,
    not this test's own emit() call, so the hold covers the plugin's publishing."""
    await _two_credentials(agent_db)
    notice = "You've hit your session limit · resets 6pm (UTC)"
    refusing = Refusing(
        {TOKEN},
        _result_error("API Error: 429 rate limited", status=429),
        said=notice,
        scripted=True,
    )
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=refusing)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    texts = [e["delta"] for e in await events(agent_db, sid) if e["type"] == "assistant.text.delta"]
    assert texts == ["done"]
    assert refusing.tokens == [TOKEN, TOKEN + "-second"]


async def test_a_refused_last_credentials_notice_is_shown_and_marked_as_no_reply(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#3703: with no credential to fall back to, the refusal notice is the turn's answer
    to the panel (why it failed), so it is published; but it is marked ``notice``, so the
    turn's retain (memoryActivities.ts transcriptOf) never keeps it as the assistant's."""
    await insert_gateway(agent_db, KEK, "http://127.0.0.1:9", secret=TOKEN)
    notice = "You've hit your weekly limit · resets Oct 18, 1pm (UTC)"
    refused = RefusesAfterSaying(notice, _result_error("API Error: 429 rate limited", status=429))

    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=lambda **_: refused)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")

            # The published notice is what is checked; the turn's end is
            # test_a_segment_every_credential_refuses_ends_the_turn_once's (#3705).
            async def shown() -> bool:
                return any(e["type"] == "assistant.text.delta" for e in await events(agent_db, sid))

            await until(shown, 60.0)
        finally:
            await handle.terminate()
    deltas = [e for e in await events(agent_db, sid) if e["type"] == "assistant.text.delta"]
    assert [(e["delta"], e.get("notice")) for e in deltas[:1]] == [(notice, True)]


def test_the_plugins_runner_publishes_text_through_the_held_contextvar() -> None:
    """#3383: HeldText holds by setting the plugin's private ``_EMIT``. The plugin's real
    runner publishes each ``TextBlock`` with ``emit`` (no Claude Code CLI runs here, so
    this checks the path rather than driving it); a pin bump that moves that publishing
    off ``_EMIT`` fails here, not silently in production."""
    from temporalio.claude_agent_sdk import _runner as plugin_runner

    publish = vars(plugin_runner)["emit"]  # what its TextBlock loop calls
    assert publish is emit
    seen: list[dict[str, Any]] = []
    token = _EMIT.set(seen.append)
    try:
        publish({"type": "text", "text": "held"})
    finally:
        _EMIT.reset(token)
    assert [e["text"] for e in seen] == ["held"]


def test_segment_calls_skip_what_the_plugin_publishes_itself() -> None:
    """DURABLE_PREFIX is the plugin's own server's: a pin bump that renames it would
    publish each durable call twice."""
    from temporalio.claude_agent_sdk import _runner as plugin_runner

    assert vars(plugin_runner)["PREFIX"] == DURABLE_PREFIX


async def test_segment_calls_publish_a_segments_own_calls_as_they_run() -> None:
    """Skill, Agent and a subagent's calls go out as they start and end, the subagent's
    under its Agent call; the calls the workflow runs and publishes do not."""
    calls = SegmentCalls(["Bash"])
    hooks = calls.hooks()
    started: Any = hooks["PreToolUse"][0].hooks[0]
    ended: Any = hooks["PostToolUse"][0].hooks[0]
    failed: Any = hooks["PostToolUseFailure"][0].hooks[0]
    seen: list[dict[str, Any]] = []
    token = _EMIT.set(seen.append)
    try:
        await started({"tool_name": "Skill", "tool_input": {"skill": "authoring"}}, "s1", None)
        await ended({"tool_name": "Skill"}, "s1", None)
        await started({"tool_name": "Agent", "tool_input": {}}, "a1", None)
        sub = {"tool_name": "mcp__scadbuddy__get_model", "tool_input": {}, "agent_id": "x"}
        await started(sub, "c1", None)
        await failed({**sub, "hook_event_name": "PostToolUseFailure"}, "c1", None)
        await started({"tool_name": f"{DURABLE_PREFIX}render_model", "tool_input": {}}, "d1", None)
        await started({"tool_name": "Bash", "tool_input": {}}, "b1", None)
        await ended({"tool_name": "Agent"}, "a1", None)
        await started(sub, "c2", None)  # no Agent call open: no parent
    finally:
        _EMIT.reset(token)
    assert [(e["type"], e["id"], e.get("parent"), e.get("status")) for e in seen] == [
        ("tool_call", "s1", None, None),
        ("tool_result", "s1", None, "done"),
        ("tool_call", "a1", None, None),
        ("tool_call", "c1", "a1", None),
        ("tool_result", "c1", None, "failed"),
        ("tool_result", "a1", None, "done"),
        ("tool_call", "c2", None, None),
    ]


async def test_segment_calls_name_each_subagents_agent_call_by_its_start() -> None:
    """#4184: with several Agent calls open, a subagent's calls name the one whose
    subagent_type its SubagentStart names; of two alike, none (never the wrong one)."""
    calls = SegmentCalls([])
    hooks = calls.hooks()
    started: Any = hooks["PreToolUse"][0].hooks[0]
    sub_started: Any = hooks["SubagentStart"][0].hooks[0]
    seen: list[dict[str, Any]] = []
    token = _EMIT.set(seen.append)
    try:
        await started(
            {"tool_name": "Agent", "tool_input": {"subagent_type": "scadbuddy:reviewer"}},
            "a1",
            None,
        )
        await started({"tool_name": "Agent", "tool_input": {}}, "a2", None)
        await sub_started({"agent_id": "x", "agent_type": "reviewer"}, None, None)
        await sub_started({"agent_id": "y", "agent_type": "general-purpose"}, None, None)
        await started({"tool_name": "Read", "tool_input": {}, "agent_id": "y"}, "c1", None)
        await started({"tool_name": "Read", "tool_input": {}, "agent_id": "x"}, "c2", None)
        await started({"tool_name": "Agent", "tool_input": {}}, "a3", None)
        await sub_started({"agent_id": "z", "agent_type": "general-purpose"}, None, None)
        # a2 and a3 are both general-purpose and z came after a2 was claimed: a3 is z's.
        await started({"tool_name": "Read", "tool_input": {}, "agent_id": "z"}, "c3", None)
        await started({"tool_name": "Agent", "tool_input": {}}, "a4", None)
        await started({"tool_name": "Agent", "tool_input": {}}, "a5", None)
        await sub_started({"agent_id": "w", "agent_type": "general-purpose"}, None, None)
        await started({"tool_name": "Read", "tool_input": {}, "agent_id": "w"}, "c4", None)
    finally:
        _EMIT.reset(token)
    parents = {e["id"]: e.get("parent") for e in seen if e["id"].startswith("c")}
    assert parents == {"c1": "a2", "c2": "a1", "c3": "a3", "c4": None}


async def test_a_claimed_subagents_late_call_names_no_other_agent_call() -> None:
    """#4404 review: once its Agent call ended, a claimed subagent's call is not given
    the one Agent call still open."""
    calls = SegmentCalls([])
    hooks = calls.hooks()
    started: Any = hooks["PreToolUse"][0].hooks[0]
    ended: Any = hooks["PostToolUse"][0].hooks[0]
    sub_started: Any = hooks["SubagentStart"][0].hooks[0]
    seen: list[dict[str, Any]] = []
    token = _EMIT.set(seen.append)
    try:
        await started({"tool_name": "Agent", "tool_input": {}}, "a1", None)
        await sub_started({"agent_id": "x", "agent_type": "general-purpose"}, None, None)
        await ended({"tool_name": "Agent"}, "a1", None)
        await started({"tool_name": "Agent", "tool_input": {"subagent_type": "other"}}, "a2", None)
        await started({"tool_name": "Read", "tool_input": {}, "agent_id": "x"}, "c1", None)
    finally:
        _EMIT.reset(token)
    assert [e.get("parent") for e in seen if e["id"] == "c1"] == [None]


@pytest.mark.parametrize("error", ["it broke", "Reached maximum budget ($1.00)"])
async def test_an_attempt_answering_an_error_ends_the_calls_it_opened(error: str) -> None:
    """#4404 review: a segment that returns an error, not raises one, leaves no call running;
    the budget cap's (answered as BUDGET_EXHAUSTED) too. #4421 review: its own held text goes
    out after, as the raised-error path already does (held.flush() in the ``finally``)."""
    calls = SegmentCalls([])
    started: Any = calls.hooks()["PreToolUse"][0].hooks[0]
    seen: list[dict[str, Any]] = []
    token = _EMIT.set(seen.append)
    try:
        await started({"tool_name": "Skill", "tool_input": {}}, "s1", None)
        failed = SegmentOutput(session_id="s", is_error=True, error=error)
        runner = ScadBuddyRunner(
            _no_database, KEK, cwd="/tmp", make_runner=_Answers(failed, said="partial")
        )
        settings = TurnSettings(long_edge=1568, model=None, own_plugin=False)
        inp = SegmentInput(session_id="s", prompt="hi", tools=[])
        out = await runner._on_credentials(
            "s", "wf", inp, 1, [_credential()], 1.0, {}, settings, calls
        )
    finally:
        _EMIT.reset(token)
    assert out.is_error
    assert [(e["type"], e.get("id"), e.get("status"), e.get("text")) for e in seen] == [
        ("tool_call", "s1", None, None),
        ("tool_result", "s1", "failed", None),
        ("text", None, None, "partial"),
    ]


async def test_segment_calls_close_what_a_failed_attempt_left_open() -> None:
    """#4184: an attempt that fails ends its open calls as failed, so none shows running."""
    calls = SegmentCalls([])
    hooks = calls.hooks()
    started: Any = hooks["PreToolUse"][0].hooks[0]
    ended: Any = hooks["PostToolUse"][0].hooks[0]
    seen: list[dict[str, Any]] = []
    token = _EMIT.set(seen.append)
    try:
        await started({"tool_name": "Skill", "tool_input": {}}, "s1", None)
        await ended({"tool_name": "Skill"}, "s1", None)
        await started({"tool_name": "Agent", "tool_input": {}}, "a1", None)
        await started({"tool_name": "Read", "tool_input": {}, "agent_id": "x"}, "c1", None)
    finally:
        _EMIT.reset(token)
    closed: list[dict[str, Any]] = []
    calls.close_open(closed.append)
    assert closed == [
        {"type": "tool_result", "id": "a1", "name": "Agent", "status": "failed"},
        {"type": "tool_result", "id": "c1", "name": "Read", "status": "failed"},
    ]
    calls.close_open(closed.append)
    assert len(closed) == 2


def test_a_subagent_type_matches_its_plugin_qualified_form() -> None:
    assert same_subagent("scadbuddy:reviewer", "reviewer")
    assert same_subagent("reviewer", "scadbuddy:reviewer")
    assert same_subagent("reviewer", "reviewer")
    assert not same_subagent("scadbuddy:reviewer", "author")
    assert not same_subagent("viewer", "reviewer")
    # Two plugins' subagents of one bare name are not each other's.
    assert not same_subagent("a:reviewer", "b:reviewer")


def test_segment_hooks_are_added_to_hooks_already_set() -> None:
    """#4184: extra_options' own hooks stay; ours go after them, per event."""
    theirs = HookMatcher(hooks=[])
    ours = SegmentCalls([]).hooks()
    merged = merged_hooks({"PreToolUse": [theirs], "Stop": [theirs]}, ours)
    assert merged["PreToolUse"] == [theirs, *ours["PreToolUse"]]
    assert merged["Stop"] == [theirs]
    assert merged["SubagentStart"] == ours["SubagentStart"]
    assert merged_hooks(None, ours) == ours


def test_refusing_scripted_needs_what_it_says() -> None:
    with pytest.raises(ValueError, match="needs said"):
        Refusing(set(), RuntimeError("x"), scripted=True)


async def test_a_first_credentials_answer_still_reaches_the_transcript(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#2945: a non-last credential's text is held, and flushed when it answers."""
    await _two_credentials(agent_db)
    refusing = Refusing(set(), _result_error("API Error: 429 rate limited", status=429))
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=refusing)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    texts = [e["delta"] for e in await events(agent_db, sid) if e["type"] == "assistant.text.delta"]
    assert texts == ["done"]
    assert refusing.tokens == [TOKEN]


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


@pytest.mark.parametrize(
    "error",
    [
        _result_error("API Error: 401 invalid x-api-key", status=401),
        _result_error("API Error: 429 rate limited", status=429),
        _result_error("API Error: 400 Your credit balance is too low", status=400),
    ],
)
async def test_a_segment_every_credential_refuses_ends_the_turn_once(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    error: ResultError,
) -> None:
    """#3705: with every credential refused there is nothing a retry could change: the
    segment is not retried (each retry ran the refused keys again and published the
    notice again). The turn ends once, with the notice and its error."""
    await _two_credentials(agent_db)
    notice = "You've hit your weekly limit · resets Oct 18, 1pm (UTC)"
    refusing = Refusing({TOKEN, TOKEN + "-second"}, error, said=notice)
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=refusing)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            await settled(connect, sid, timeout=30.0)
        finally:
            await handle.terminate()
    assert refusing.tokens == [TOKEN, TOKEN + "-second"]
    logged = await events(agent_db, sid)
    assert [e["delta"] for e in logged if e["type"] == "assistant.text.delta"] == [notice]
    assert len([e for e in logged if e["type"] == "error"]) == 1


class FailsOnce:
    """make_runner whose first segment fails with ``error``, and every later one answers."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.tokens: list[str] = []
        self.scripted = ScriptedClaude(lambda prompt, _: Final("done"), cost_per_segment=0.25)

    def __call__(self, **kwargs: Any) -> Any:
        self.tokens.append(kwargs["env"]["ANTHROPIC_AUTH_TOKEN"])
        if len(self.tokens) > 1:
            return self.scripted
        error = self.error

        class Raises:
            async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput:
                raise error

        return Raises()


async def test_a_transient_failure_of_the_last_credential_is_still_retried(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#3705: only a refusal is final. A 5xx is the endpoint's, not the key's: the
    segment is retried, on the same key, and the turn answers."""
    await insert_gateway(agent_db, KEK, "http://127.0.0.1:9", secret=TOKEN)
    failing = FailsOnce(_result_error("API Error: 529 overloaded", status=529))
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=failing)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    assert failing.tokens == [TOKEN, TOKEN]
    texts = [e["delta"] for e in await events(agent_db, sid) if e["type"] == "assistant.text.delta"]
    assert texts == ["done"]


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


def test_an_agent_refusal_replaces_the_rejection_the_model_reads() -> None:
    """#4442 gate review: a call the agent refused before it parked is not one a person
    rejected; the model reads only the agent's reason, still as an error."""
    why = "print_output was not run: Garage is Bambuddy printer 2, not printer_id 3."
    injected: dict[str, Any] = {
        "t1": ToolOutcome(content=PLUGIN_REJECTION, is_error=True),
        "t2": {"content": PLUGIN_REJECTION, "is_error": True},
        "t3": ToolOutcome(content=PLUGIN_REJECTION, is_error=True),
    }
    out = with_refusals(injected, {"t3": "denied"}, {"t1": why, "t2": why})
    assert out["t1"] == ToolOutcome(content=why, is_error=True)
    assert out["t2"] == {"content": why, "is_error": True}
    assert "human reviewer" not in str(out["t1"]) + str(out["t2"])
    assert out["t3"] == ToolOutcome(content=f"{PLUGIN_REJECTION}\n\ndenied", is_error=True)


def test_a_describe_that_stayed_unavailable_says_try_again_and_no_one_rejected_it() -> None:
    """#4442 gate review: the backend away through describe_call's retries is no refusal
    of the call; the model is told it may try again. The agent's own refusal is its text."""
    away = ApplicationError("read the printer failed (HTTP 503)", type="BackendUnavailable")
    assert _tool_refusal(away, "mcp__scadbuddy__print_output") == (
        "mcp__scadbuddy__print_output was not run: what it names could not be checked just now;"
        " try the call again."
    )
    assert _tool_refusal(ApplicationError("no", type="ToolError"), "t") == "no"
    assert _tool_refusal(ApplicationError("gone", type="UnknownSession"), "t") is None


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
    """make_runner whose segment answers ``out``, or raises it, whatever it is asked.
    ``said`` is text the segment emits (live) before it does."""

    def __init__(self, out: SegmentOutput | BaseException, said: str | None = None) -> None:
        self.out = out
        self.said = said
        self.budgets: list[float] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.budgets.append(kwargs["max_budget_usd"])
        out, said = self.out, self.said

        class Answers:
            async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput:
                if said is not None:
                    emit({"type": "text", "text": said})
                if isinstance(out, BaseException):
                    raise out
                return out

        return Answers()


def _credential() -> Credential:
    return Credential(id="c1", priority=0, kind="gateway", base_url="http://x", secret=TOKEN)


def _no_database() -> Any:
    raise AssertionError("_on_credentials connected to the database: _segment has none")


async def _segment(out: SegmentOutput | Exception, left: float) -> SegmentOutput:
    """`_on_credentials` on one credential whose runner (`_Answers`) answers ``out``. It
    has no database: a path that charges a refused attempt fails here saying so."""
    answers = _Answers(out)
    runner = ScadBuddyRunner(_no_database, KEK, cwd="/tmp", make_runner=answers)
    settings = TurnSettings(long_edge=1568, model=None, own_plugin=False)
    inp = SegmentInput(session_id="s", prompt="hi", tools=[])
    got = await runner._on_credentials("s", "wf", inp, 1, [_credential()], left, {}, settings)
    assert answers.budgets == [left]
    return got


async def test_a_cancelled_attempt_ends_the_calls_it_opened() -> None:
    """#4184 review: a cancelled segment (an interrupt, a shutdown) leaves no call running."""
    calls = SegmentCalls([])
    started: Any = calls.hooks()["PreToolUse"][0].hooks[0]
    seen: list[dict[str, Any]] = []
    token = _EMIT.set(seen.append)
    try:
        await started({"tool_name": "Skill", "tool_input": {}}, "s1", None)
        runner = ScadBuddyRunner(
            _no_database, KEK, cwd="/tmp", make_runner=_Answers(asyncio.CancelledError())
        )
        settings = TurnSettings(long_edge=1568, model=None, own_plugin=False)
        inp = SegmentInput(session_id="s", prompt="hi", tools=[])
        with pytest.raises(asyncio.CancelledError):
            await runner._on_credentials(
                "s", "wf", inp, 1, [_credential()], 1.0, {}, settings, calls
            )
    finally:
        _EMIT.reset(token)
    assert [(e["type"], e["id"], e.get("status")) for e in seen] == [
        ("tool_call", "s1", None),
        ("tool_result", "s1", "failed"),
    ]


@pytest.mark.parametrize(
    "error",
    [
        "Reached maximum budget ($1.00)",
        "Claude Code returned an error result: Reached maximum budget ($1.00) (exit code: 1)",
    ],
)
@pytest.mark.parametrize(("cost", "charged"), [(0.25, 1.0), (0.0, 1.0), (1.5, 1.5)])
async def test_a_segment_that_reached_its_cap_is_charged_at_least_what_was_left(
    error: str, cost: float, charged: float
) -> None:
    """#2675: Claude Code stops once the cost reached the cap, so the segment is charged
    `max(cost_usd, left)`: what was left when its own report is lower, its report when
    higher. Either way the turn ends as the budget's."""
    out = SegmentOutput(session_id="s", is_error=True, error=error, cost_usd=cost)
    got = await _segment(out, left=1.0)
    assert got.error == BUDGET_EXHAUSTED
    assert got.cost_usd == pytest.approx(charged)


@pytest.mark.parametrize(("cost", "charged"), [(0.0, 1.0), (1.5, 1.5)])
async def test_a_raised_cap_ends_the_turn_as_the_budgets_too(cost: float, charged: float) -> None:
    """#3356: a cap the plugin raises rather than returns is the same cap: charged at
    least what was left, and ended as the budget's, not failed."""
    err = ResultError(
        "Claude Code returned an error result: Reached maximum budget ($1.00)",
        data={"total_cost_usd": cost},
        exit_code=1,
    )
    got = await _segment(err, left=1.0)
    assert got.error == BUDGET_EXHAUSTED
    assert got.cost_usd == pytest.approx(charged)


@pytest.mark.parametrize(
    "text",
    [
        "the tool said error_max_budget_usd somewhere in its output",
        "Quoting: Reached maximum budget is what it says",
        "the user pasted: Reached maximum budget ($5.00) from an old log",
        "Claude Code returned an error result: tool said Reached maximum budget ($2.00)",
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


async def test_an_error_that_names_max_budget_usd_fails_the_turn(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#3094: only the runner's own BUDGET_EXHAUSTED ends a turn as the budget's. A
    segment's error that merely names `max_budget_usd` (session content, or a plugin
    complaining about its options) is the failure it is."""
    await insert_gateway(agent_db, KEK, "http://127.0.0.1:9")
    text = "the plugin refused ClaudeAgentSdkRunner(max_budget_usd=...)"
    answers = _Answers(SegmentOutput(session_id="s", is_error=True, error=text))
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=answers)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db, budget=1.0)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "failed"
        finally:
            await handle.terminate()
    [error] = [e for e in await events(agent_db, sid) if e["type"] == "error"]
    assert error["code"] == "turn_failed"


def test_held_text_goes_out_in_order_before_the_next_other_event() -> None:
    """#2945: text is only held, never reordered; a drop loses only what is held."""
    out: list[dict[str, Any]] = []
    held = HeldText(out.append)
    held({"type": "text", "text": "a"})
    held({"type": "tool_call", "id": "1"})
    held({"type": "text", "text": "b"})
    assert out == [{"type": "text", "text": "a"}, {"type": "tool_call", "id": "1"}]
    held.drop()
    held.flush()
    held({"type": "text", "text": "c"})
    held.flush()
    assert [e.get("text", e["type"]) for e in out] == ["a", "tool_call", "c"]


async def _health(conn: Conn) -> dict[str, tuple[str, Any, Any, str | None]]:
    """Each gateway credential's status, cooldown_until, last_used_at and last_error,
    by its base URL."""
    cur = await conn.execute(
        "SELECT base_url, status, cooldown_until, last_used_at, last_error FROM ai_credentials"
    )
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in await cur.fetchall()}


async def test_an_answering_credential_is_recorded_as_used(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#2320: a durable segment stamps its credential's ``last_used_at`` as a classic turn
    does (fallback.ts ``report`` with ``used``), so Settings shows when it last answered."""
    await insert_gateway(agent_db, KEK, "http://127.0.0.1:9", secret=TOKEN)
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=Recording())
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    status_, _, used, error = (await _health(agent_db))["http://127.0.0.1:9"]
    assert (status_, error) == ("active", None)
    assert used is not None


async def test_a_rate_limited_credential_cools_down_and_the_next_is_used(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#2320: the rate-limited key is recorded cooling down, as a classic turn records it
    (credentials.ts ``record``, ``cooling_down``), so the next turn skips it instead of
    running into the same limit; the key that answered is stamped used; the cooldown is
    audited."""
    await _two_credentials(agent_db)
    refusing = Refusing({TOKEN}, _result_error("API Error: 429 rate limited", status=429))
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=refusing)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    health = await _health(agent_db)
    first, second = health["http://127.0.0.1:9"], health["http://127.0.0.1:10"]
    assert first[0] == "cooling_down"
    assert first[1] is not None
    assert first[3] is not None and "429" in first[3]
    assert second[0] == "active" and second[2] is not None
    cur = await agent_db.execute(
        "SELECT action, outcome, session_id FROM ai_audit WHERE kind = 'credential'"
    )
    assert (await cur.fetchall()) == [("cooldown", "error", uuid.UUID(sid))]


async def test_a_rate_limit_that_names_its_reset_cools_down_until_then(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """#4088: a segment never sees the 429's reset headers, but Claude Code's result
    names the reset ("resets 10:30pm (UTC)"), and the key cools down until then, not
    for the default 60 s."""
    # The runner reads its own clock; 5 h out keeps the named time well clear of it
    # (and of RESET_GRACE), so do not shrink the margin.
    at = (datetime.now(UTC) + timedelta(hours=5)).replace(second=0, microsecond=0)
    named = f"{at.hour % 12 or 12}:{at.minute:02d}{'am' if at.hour < 12 else 'pm'}"
    text = f"API Error: 429 You've hit your session limit · resets {named} (UTC)"
    await _two_credentials(agent_db)
    refusing = Refusing({TOKEN}, _result_error(text, status=429))
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=refusing)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    first = (await _health(agent_db))["http://127.0.0.1:9"]
    assert first[0] == "cooling_down"
    assert first[1] == at


async def test_a_refused_credential_is_noted_but_never_disabled_unconfirmed(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """A classic turn disables a refused key only once its probe confirms the refusal
    (fallback.ts); a durable segment has no probe, so it records the error and leaves
    the key usable, never disabling one on an unconfirmed 401."""
    await _two_credentials(agent_db)
    refusing = Refusing({TOKEN}, _result_error("API Error: 401 invalid x-api-key", status=401))
    runner = ScadBuddyRunner(connect, KEK, cwd=str(tmp_path), make_runner=refusing)
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    first = (await _health(agent_db))["http://127.0.0.1:9"]
    assert first[0] == "active"
    assert first[3] is not None and "401" in first[3]


# ---- #4757: the own plugin's items, and Skill and Agent calls by access ----

REPO = Path(__file__).resolve().parents[2]
OWN_SKILLS = REPO / "plugins" / "scadbuddy" / "skills"
OWN_AGENTS = REPO / "plugins" / "scadbuddy" / "agents"
# The first line of print-analyst.md's prompt: a request that holds it is the subagent's.
SUBAGENT_PROMPT = "You run one print-analyzer session"


def _own_plugin(tmp_path: Path) -> Path:
    """The image's layout: the own plugin's skills in a folder named scadbuddy."""
    plugin = tmp_path / "scadbuddy"
    plugin.mkdir()
    (plugin / "skills").symlink_to(OWN_SKILLS)
    return plugin


def _results(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        b
        for m in body.get("messages") or []
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if b.get("type") == "tool_result"
    ]


def _text_of(result: dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        return "".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content)


async def _two_calls(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    call: dict[str, Any],
    off: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The bundled Claude Code makes ``call`` twice in one segment; between the two, the
    own plugin's ``off`` item is switched off. Answers the main thread's tool results and
    the session's events."""
    cur = await agent_db.execute("SHOW search_path")
    row = await cur.fetchone()
    assert row is not None
    url, search_path = os.environ["SCADBUDDY_TEST_DATABASE_URL"], row[0]
    main: list[list[dict[str, Any]]] = []

    def switch_off() -> None:
        with psycopg.connect(url, autocommit=True, options=f"-c search_path={search_path}") as c:
            c.execute(
                "INSERT INTO ai_access (source, item, access, updated_by)"
                " VALUES ('builtin:own-plugin', %s, 'off', 'browser:browser')",
                (off,),
            )

    def reply(body: dict[str, Any]) -> dict[str, Any]:
        names = [t.get("name") for t in body.get("tools") or []]
        if call["name"] not in names or SUBAGENT_PROMPT in str(body.get("system")):
            return {"text": "ok"}  # Claude Code's own side requests, and the subagent's
        results = _results(body)
        main.append(results)
        if len(results) == 0:
            return {"tool_use": call}
        if len(results) == 1:
            switch_off()
            return {"tool_use": call}
        return {"text": "All done."}

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
            connect,
            KEK,
            cwd=os.path.realpath(cwd),
            plugin_dir=str(_own_plugin(tmp_path)),
            subagent_url="http://127.0.0.1:9/mcp",
            agents_dir=str(OWN_AGENTS),
            make_runner=ClaudeAgentSdkRunner,
            own_plugin_root=str(tmp_path / "own"),
        )
        async with running(temporal_env, connect, runner) as (client, queue):
            sid = await insert_session(agent_db)
            await agent_db.execute(
                "UPDATE ai_sessions SET durable_turn_id = %s WHERE id = %s",
                (str(uuid.uuid4()), sid),
            )
            handle = await start_session(client, sid, queue)
            try:
                await send(handle, "go")
                assert await settled(connect, sid, timeout=180) == "idle"
            finally:
                await handle.terminate()
        log = await events(agent_db, sid)
    finally:
        fake.close()
        await asyncio.sleep(0)
    final = next(r for r in reversed(main) if len(r) >= 2)
    # The panel sees each call end, the refused one too.
    opened = {e["id"] for e in log if e["type"] == "tool.call"}
    ended = {e["id"] for e in log if e["type"] == "tool.result"}
    assert len(opened) >= 2 and opened <= ended, (opened, ended)
    return final, log


async def test_a_skill_set_off_mid_segment_is_refused_on_its_next_call(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#4757: the first Skill call runs; the skill is switched off; the next call of it in
    the same segment is refused, naming the item as a classic turn names it."""
    (first, second), _ = await _two_calls(
        temporal_env,
        agent_db,
        connect,
        tmp_path,
        monkeypatch,
        {"name": "Skill", "input": {"skill": "scadbuddy:print"}},
        "skill:print",
    )
    assert not first.get("is_error"), first
    assert second.get("is_error") is True
    assert (
        'skill "print" of ScadBuddy\'s own plugin is switched off in Settings → Tools & plugins'
        in _text_of(second)
    )


async def test_a_subagent_set_off_mid_segment_is_refused_on_its_next_call(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (first, second), _ = await _two_calls(
        temporal_env,
        agent_db,
        connect,
        tmp_path,
        monkeypatch,
        {
            "name": "Agent",
            "input": {
                "subagent_type": "print-analyst",
                "description": "Analyse",
                "prompt": "Analyse the print.",
            },
        },
        "subagent:print-analyst",
    )
    assert not first.get("is_error"), first
    assert second.get("is_error") is True
    assert 'subagent "print-analyst" of ScadBuddy\'s own plugin is switched off' in _text_of(second)


async def test_a_call_is_refused_when_access_cannot_be_read(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """Fail closed: the segment's call hook refuses a Skill or Agent call whose rows cannot
    be read, and lets every other tool past it."""
    options = await _segment_options(
        temporal_env, agent_db, connect, tmp_path, plugin_dir=str(_own_plugin(tmp_path))
    )
    [matcher] = options["extra_options"]["hooks"]["PreToolUse"]
    [check] = matcher.hooks
    calls = check.__self__
    call = {"tool_name": "Skill", "tool_input": {"skill": "print"}, "tool_use_id": "t1"}
    assert await check(call, "t1", None) == {}

    @contextlib.asynccontextmanager
    async def broken() -> AsyncIterator[Conn]:
        raise OSError("the database is away")
        yield  # pragma: no cover

    calls._judge.__self__._connect = broken
    published: list[dict[str, Any]] = []
    token = _EMIT.set(published.append)
    try:
        denied = await check({**call, "tool_use_id": "t2"}, "t2", None)
        assert (
            await check({"tool_name": "Bash", "tool_input": {}, "tool_use_id": "t3"}, "t3", None)
            == {}
        )
    finally:
        _EMIT.reset(token)
    output = denied["hookSpecificOutput"]
    assert (output["hookEventName"], output["permissionDecision"]) == ("PreToolUse", "deny")
    assert output["permissionDecisionReason"] == (
        "Skill is refused: its Settings → Tools & plugins entry cannot be read"
    )
    # The refused call ends as it is refused; the Bash call runs on.
    assert [(e["type"], e["id"]) for e in published] == [
        ("tool_call", "t2"),
        ("tool_result", "t2"),
        ("tool_call", "t3"),
    ]
    assert published[1]["status"] == "failed"


def test_own_plugin_copy_leaves_out_off_skill_and_subagent(tmp_path: Path) -> None:
    plugin = tmp_path / "scadbuddy"
    shutil.copytree(OWN_SKILLS, plugin / "skills")
    (plugin / "agents").mkdir()
    (plugin / "agents" / "helper.md").write_text("---\nname: aide\ndescription: d\n---\nHelp.\n")
    (plugin / "agents" / "other.md").write_text("---\nname: other\ndescription: d\n---\nO.\n")
    root = tmp_path / "own"
    off = frozenset({"skill:print", "subagent:aide"})
    copy = Path(own_plugin_copy(str(plugin), off, str(root)))
    # Named after the plugin's folder, under the copy's own hash.
    assert copy.name == "scadbuddy"
    assert copy.parent.name == hash_tree(copy).removeprefix("sha256:")
    assert sorted(p.name for p in (copy / "skills").iterdir()) == ["authoring", "customize"]
    assert [p.name for p in (copy / "agents").iterdir()] == ["other.md"]
    assert (plugin / "skills" / "print").is_dir()  # the plugin itself is left as it is
    # A copy already there is kept once it hashes to its name, replaced once it does not.
    assert own_plugin_copy(str(plugin), off, str(root)) == str(copy)
    (copy / "skills" / "authoring" / "SKILL.md").write_text("TAMPERED-4757")
    again = Path(own_plugin_copy(str(plugin), off, str(root)))
    assert again == copy
    assert "TAMPERED-4757" not in (again / "skills" / "authoring" / "SKILL.md").read_text()


async def test_own_plugin_loads_as_it_is_with_nothing_off(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    plugin = _own_plugin(tmp_path)
    assert own_plugin_copy(str(plugin), frozenset(), str(tmp_path / "own")) == str(plugin)
    # An off row for an item the plugin does not have changes nothing.
    assert own_plugin_copy(str(plugin), frozenset({"skill:gone"}), str(tmp_path / "o")) == str(
        plugin
    )
    on = await _segment_options(temporal_env, agent_db, connect, tmp_path, plugin_dir=str(plugin))
    assert on["extra_options"]["plugins"] == [{"type": "local", "path": str(plugin)}]


async def test_an_off_skill_loads_the_own_plugin_without_it(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    plugin = _own_plugin(tmp_path)
    await agent_db.execute(
        "INSERT INTO ai_access (source, item, access, updated_by)"
        " VALUES ('builtin:own-plugin', 'skill:print', 'off', 'browser:browser')"
    )
    options = await _segment_options(
        temporal_env, agent_db, connect, tmp_path, plugin_dir=str(plugin)
    )
    [loaded] = options["extra_options"]["plugins"]
    assert loaded["path"] != str(plugin)
    assert Path(loaded["path"]).name == "scadbuddy"
    assert not (Path(loaded["path"]) / "skills" / "print").exists()
    assert (Path(loaded["path"]) / "skills" / "authoring" / "SKILL.md").is_file()


async def test_off_subagent_is_not_defined_inline(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    await insert_gateway(agent_db, KEK, "http://127.0.0.1:9")
    await agent_db.execute(
        "INSERT INTO ai_access (source, item, access, updated_by)"
        " VALUES ('builtin:own-plugin', 'subagent:print-analyst', 'off', 'browser:browser')"
    )
    recording = Recording()
    runner = ScadBuddyRunner(
        connect,
        KEK,
        cwd=str(tmp_path),
        plugin_dir=str(_own_plugin(tmp_path)),
        subagent_url="http://127.0.0.1:9/mcp",
        agents_dir=str(OWN_AGENTS),
        make_runner=recording,
        own_plugin_root=str(tmp_path / "own"),
    )
    async with running(temporal_env, connect, runner) as (client, queue):
        sid = await insert_session(agent_db)
        await agent_db.execute(
            "UPDATE ai_sessions SET durable_turn_id = %s WHERE id = %s", (str(uuid.uuid4()), sid)
        )
        handle = await start_session(client, sid, queue)
        try:
            await send(handle, "hi")
            assert await settled(connect, sid) == "idle"
        finally:
            await handle.terminate()
    [made] = recording.made
    assert sorted(made["extra_options"]["agents"]) == ["model-author"]


def test_own_call_plugin_names_its_items_as_the_agent_does(tmp_path: Path) -> None:
    plugin = own_call_plugin(str(_own_plugin(tmp_path)), str(OWN_AGENTS))
    assert (plugin.name, plugin.source) == ("scadbuddy", "builtin:own-plugin")
    assert plugin.items == (
        "skill:authoring",
        "skill:customize",
        "skill:print",
        "subagent:model-author",
        "subagent:print-analyst",
    )


# ---- #4757 Task 6 review, fix round 1 ----


def test_call_plugins_put_the_packages_before_the_own_plugin(tmp_path: Path) -> None:
    """I1: the classic turn's order (turnRunner.ts callPlugins), so the first plugin that
    refuses, and so the reason, is the same in both runtimes."""
    own = own_call_plugin(str(_own_plugin(tmp_path)), str(OWN_AGENTS))
    pkg = TurnPackage("pa", f"{'1' * 64}/pa", "sha256:" + "1" * 64, items=["skill:print"])
    plugins = call_plugins_of(own, [pkg])
    assert [p.name for p in plugins] == ["pa", "scadbuddy"]
    rows = [
        access.AccessRow("builtin:own-plugin", "skill:print", "off", "browser:browser"),
        access.AccessRow("package:pa", "skill:print", "off", "browser:browser"),
    ]
    off = access.off_call_item("Skill", {"skill": "print"}, plugins, rows)
    assert off == access.OffCallItem("package:pa", "skill:print")


def test_own_call_plugin_keeps_a_frontmatter_name_over_the_cap_whole(tmp_path: Path) -> None:
    """m1: alias keys are whole frontmatter names, as the classic call rule has them."""
    plugin = _own_plugin(tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir()
    over = "deep-" * 40
    (agents / "deep.md").write_text(f"---\nname: {over}\ndescription: d\n---\nD.\n")
    assert own_call_plugin(str(plugin), str(agents)).aliases == {
        f"subagent:{over}": ("subagent:deep",)
    }


async def test_a_packages_builtin_skill_is_read_per_call(agent_db: Conn, connect: Connect) -> None:
    """I2 (Task 5 review M4), and review round 2: an allowed package that names Skill gives
    every Skill call that built-in's access, read as each call is made, as a classic turn's
    seam does: off refuses it, read or write runs it, outward (or no row, outward as in
    classic) parks it at the gate, running it only once approved."""
    parked: list[tuple[str, str, Any]] = []
    answers: list[dict[str, Any]] = []

    async def park(call: str, tool: str, tool_input: Any) -> dict[str, Any]:
        parked.append((call, tool, tool_input))
        return answers.pop(0)

    judge = CallAccess(connect, [], {"Skill": ["notes"]}, park).check
    call = {"tool_name": "Skill", "tool_input": {"skill": "anything"}, "tool_use_id": "t1"}

    async def set_tier(tier: str) -> None:
        await agent_db.execute(
            "INSERT INTO ai_access (source, item, access, updated_by)"
            " VALUES ('package:notes', 'builtin:Skill', %s, 'browser:browser')"
            " ON CONFLICT (source, item) DO UPDATE SET access = EXCLUDED.access",
            (tier,),
        )

    # No row: outward, parked; approved, it runs.
    answers.append({"go": True, "reason": None})
    assert await judge(call) == {}
    assert parked == [("t1", "Skill", {"skill": "anything"})]
    # Denied: refused with the gate's reason.
    answers.append({"go": False, "reason": "the user denied it"})
    denied = await judge(call)
    assert denied["hookSpecificOutput"]["permissionDecisionReason"] == "the user denied it"
    await set_tier("read")
    assert await judge(call) == {}
    await set_tier("write")
    assert await judge(call) == {}
    await set_tier("off")
    off = await judge(call)
    assert off["hookSpecificOutput"]["permissionDecisionReason"] == (
        "Skill is disabled in Settings → Tools & plugins; the user can turn it back on there"
    )
    assert len(parked) == 2  # neither read, write nor off was parked
    # A tool no package names is never parked.
    assert await judge({"tool_name": "Agent", "tool_input": {"subagent_type": "x"}}) == {}
    # Outward with no gate to ask, or a gate that fails: refused (fail closed).
    await set_tier("outward")
    alone = await CallAccess(connect, [], {"Skill": ["notes"]}).check(call)
    assert "could not be asked for" in alone["hookSpecificOutput"]["permissionDecisionReason"]

    async def broken(call: str, tool: str, tool_input: Any) -> dict[str, Any]:
        raise RuntimeError("away")

    failed = await CallAccess(connect, [], {"Skill": ["notes"]}, broken).check(call)
    assert failed["hookSpecificOutput"]["permissionDecision"] == "deny"


SKILL_CALL = {"tool_name": "Skill", "tool_input": {"skill": "anything"}, "tool_use_id": "t1"}


async def test_an_outward_call_over_the_cap_is_refused_unasked_only_without_offloading(
    connect: Connect,
) -> None:
    """#4757 final review M3: with no External Storage to take it out of history, an input
    over the cap is refused with a plain reason before the Update is sent, never failed as
    one that could not be asked for; one at the cap is asked about. #5000: with External
    Storage (``input_max=None``) the same input is asked about, as in a classic turn."""
    parked: list[str] = []

    async def park(call: str, tool: str, tool_input: Any) -> dict[str, Any]:
        parked.append(call)
        return {"go": True, "reason": None}

    judge = CallAccess(connect, [], {"Agent": ["notes"]}, park).check
    fixed = len(json.dumps({"subagent_type": "x", "prompt": ""}, separators=(",", ":")).encode())
    at_cap = {"subagent_type": "x", "prompt": "p" * (BUILTIN_CALL_INPUT_MAX - fixed)}
    over = {"subagent_type": "x", "prompt": "p" * (BUILTIN_CALL_INPUT_MAX - fixed + 1)}
    refused = await judge({"tool_name": "Agent", "tool_input": over, "tool_use_id": "t1"})
    assert refused["hookSpecificOutput"]["permissionDecisionReason"] == (
        f"Agent's input is over {BUILTIN_CALL_INPUT_MAX} bytes, too large to ask the user"
        " about, so the call is refused; make it shorter"
    )
    assert parked == []
    assert await judge({"tool_name": "Agent", "tool_input": at_cap, "tool_use_id": "t2"}) == {}
    assert parked == ["t2"]
    offloaded = CallAccess(connect, [], {"Agent": ["notes"]}, park, input_max=None).check
    assert await offloaded({"tool_name": "Agent", "tool_input": over, "tool_use_id": "t3"}) == {}
    assert parked == ["t2", "t3"]


async def test_a_park_that_never_answers_is_refused_at_the_hooks_own_deadline(
    connect: Connect, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 6 re-review R2: a gate that never answers refuses the call at the hook's own
    deadline, past the gate's timer and below the segment's hour, and the hook's matcher
    outlasts it, so Claude Code never times the hook out and runs the call unasked."""
    assert models.HOOKED_WAIT_S < PARK_DEADLINE_S < SEGMENT_TIMEOUT.total_seconds()
    [matcher] = SegmentCalls([]).hooks()["PreToolUse"]
    assert matcher.timeout is not None and matcher.timeout > SEGMENT_TIMEOUT.total_seconds()
    monkeypatch.setattr(runner_module, "PARK_DEADLINE_S", 0.2)
    released: list[str] = []

    async def never(call: str, tool: str, tool_input: Any) -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError

    async def release(call: str) -> None:
        released.append(call)

    out = await CallAccess(connect, [], {"Skill": ["notes"]}, never, release).check(SKILL_CALL)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "was not decided in time" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert released == ["t1"]


async def test_a_call_still_waiting_when_its_segment_ends_is_released(connect: Connect) -> None:
    """Task 6 re-review R5: the segment's end releases every call still waiting at the gate,
    so its panel entry is cancelled rather than left pending with nothing to run."""
    entered = asyncio.Event()
    released: list[str] = []

    async def waits(call: str, tool: str, tool_input: Any) -> dict[str, Any]:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError

    async def release(call: str) -> None:
        released.append(call)

    gate = CallAccess(connect, [], {"Skill": ["notes"]}, waits, release)
    task = asyncio.ensure_future(gate.check(SKILL_CALL))
    await entered.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await gate.release_waiting()
    await gate.release_waiting()  # once only
    assert released == ["t1"]

    async def answers(call: str, tool: str, tool_input: Any) -> dict[str, Any]:
        return {"go": True}

    decided = CallAccess(connect, [], {"Skill": ["notes"]}, answers, release)
    assert await decided.check(SKILL_CALL) == {}
    await decided.release_waiting()
    assert released == ["t1"]  # a decided call is not released


async def test_the_segments_gate_updates_its_own_run() -> None:
    """Task 6 re-review R4: the hook's Updates name the run the segment belongs to."""
    seen: list[tuple[str, str | None, str, Any]] = []

    class Handle:
        def __init__(self, wid: str, run_id: str | None) -> None:
            self.wid, self.run_id = wid, run_id

        async def execute_update(self, name: str, arg: Any, **_kw: Any) -> Any:
            seen.append((self.wid, self.run_id, name, arg))
            return {"go": True, "reason": None}

    class FakeClient:
        def get_workflow_handle(self, wid: str, *, run_id: str | None = None) -> Handle:
            return Handle(wid, run_id)

    park, release = segment_gate(FakeClient(), "session-1", "run-7")  # type: ignore[arg-type]
    assert await park("t1", "Skill", {"skill": "x"}) == {"go": True, "reason": None}
    await release("t1")
    assert seen == [
        (
            "session-1",
            "run-7",
            BUILTIN_CALL_UPDATE,
            {"call_id": "t1", "tool": "Skill", "input": {"skill": "x"}},
        ),
        ("session-1", "run-7", BUILTIN_RELEASE_UPDATE, {"call_id": "t1"}),
    ]


def test_builtin_access_is_the_strictest_of_the_packages_naming_it() -> None:
    rows = [
        access.AccessRow("package:a", "builtin:Skill", "read", "b"),
        access.AccessRow("package:b", "builtin:Skill", "write", "b"),
        access.AccessRow("package:c", "builtin:Skill", "bogus", "b"),
    ]
    assert access.builtin_access("Skill", ["a", "b"], rows) == "write"
    assert access.builtin_access("Skill", ["a", "c"], rows) == "outward"  # no tier: outward
    assert access.builtin_access("Skill", [], rows) == "outward"


async def test_the_call_hook_names_a_packages_builtins_from_its_listing(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """I2: the runner hands the call hook each allowed package's named built-ins (before the
    listing's switched-off filter), so one off at turn start and back on mid-turn reads right."""
    pkg = TurnPackage(
        "notes", "x", "sha256:" + "0" * 64, allowed=True, builtin_tools=[], named_builtins=["Skill"]
    )
    other = TurnPackage("plain", "y", "sha256:" + "0" * 64, named_builtins=["Skill"])
    assert call_builtins_of([pkg, other]) == {"Skill": ["notes"]}
    old = TurnPackage("old", "z", "sha256:" + "0" * 64, allowed=True, builtin_tools=["Agent"])
    assert call_builtins_of([old]) == {"Agent": ["old"]}  # a listing from an older agent


@pytest.mark.parametrize("input_max", [BUILTIN_CALL_INPUT_MAX, None])
async def test_an_input_with_a_lone_surrogate_is_measured_and_asked_about(
    connect: Connect, input_max: int | None
) -> None:
    """#5017 security review: an input Claude Code sends with a lone surrogate (JSON may
    carry one) is measured without raising, with or without a cap, and asked about: never
    an exception in the hook, whose outcome Claude Code decides."""
    parked: list[str] = []

    async def park(call: str, tool: str, tool_input: Any) -> dict[str, Any]:
        parked.append(call)
        return {"go": False, "reason": "Skill was not approved"}

    gate = CallAccess(connect, [], {"Skill": ["notes"]}, park, input_max=input_max)
    lone = {
        "tool_name": "Skill",
        "tool_input": {"skill": "x", "args": "\ud800"},
        "tool_use_id": "t1",
    }
    out = await gate.check(lone)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert parked == ["t1"]


@pytest.mark.parametrize("tool_use_id", ["t1", None])
async def test_a_judge_that_raises_refuses_the_call(tool_use_id: str | None) -> None:
    """#5017 security review: the hook refuses a call whose judge raises, in a plain reason,
    and the panel sees it end; it never answers the hook with an exception."""

    async def judge(data: Any) -> dict[str, Any]:
        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")

    started: Any = SegmentCalls([], judge).hooks()["PreToolUse"][0].hooks[0]
    seen: list[dict[str, Any]] = []
    token = _EMIT.set(seen.append)
    try:
        data = {"tool_name": "Skill", "tool_input": {"skill": "x"}}
        if tool_use_id:
            data["tool_use_id"] = tool_use_id
        answer = await started(data, tool_use_id, None)
    finally:
        _EMIT.reset(token)
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert answer["hookSpecificOutput"]["permissionDecisionReason"] == (
        "Skill is refused: its access could not be checked"
    )
    if tool_use_id:
        assert [e["type"] for e in seen] == ["tool_call", "tool_result"]
        assert seen[-1]["status"] == "failed"


async def test_a_call_without_a_tool_use_id_is_judged_too() -> None:
    """m2: fail closed whatever Claude Code sends."""
    asked: list[Any] = []

    async def judge(data: Any) -> dict[str, Any]:
        asked.append(data)
        return {"hookSpecificOutput": {"permissionDecision": "deny"}}

    started: Any = SegmentCalls([], judge).hooks()["PreToolUse"][0].hooks[0]
    seen: list[dict[str, Any]] = []
    token = _EMIT.set(seen.append)
    try:
        answer = await started({"tool_name": "Skill", "tool_input": {"skill": "x"}}, None, None)
    finally:
        _EMIT.reset(token)
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert len(asked) == 1 and seen == []


async def test_a_workflow_agents_type_switched_off_runs_as_no_subagent(
    temporal_env: WorkflowEnvironment, agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    """I3: a Workflow agent's chat whose type is an off subagent does not run as it, from the
    next segment on; on, it does."""
    plugin = str(_own_plugin(tmp_path))

    async def options() -> dict[str, Any]:
        await agent_db.execute("DELETE FROM ai_credentials")
        made = await _segment_options(
            temporal_env,
            agent_db,
            connect,
            tmp_path,
            plugin_dir=plugin,
            agent=(None, None, "print-analyst"),
            agents_dir=str(OWN_AGENTS),
        )
        return dict(made["extra_options"])

    assert (await options())["extra_args"] == {"agent": "print-analyst"}
    await agent_db.execute(
        "INSERT INTO ai_access (source, item, access, updated_by)"
        " VALUES ('builtin:own-plugin', 'subagent:print-analyst', 'off', 'browser:browser')"
    )
    assert "extra_args" not in await options()
    await agent_db.execute("UPDATE ai_access SET item = '*' WHERE source = 'builtin:own-plugin'")
    assert "extra_args" not in await options()


async def test_a_failed_own_plugin_copy_tells_the_session(tmp_path: Path) -> None:
    """m4: as a classic turn says it (a plugin_unavailable error event), not only a log line."""
    plugin = _own_plugin(tmp_path)
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a folder")
    runner = ScadBuddyRunner(
        None,  # type: ignore[arg-type]
        KEK,
        cwd=str(tmp_path),
        plugin_dir=str(plugin),
        own_plugin_root=str(blocked / "own"),
    )
    settings = TurnSettings(
        long_edge=1, model=None, own_plugin=True, own_off=frozenset({"skill:print"})
    )
    seen: list[dict[str, Any]] = []
    token = _EMIT.set(seen.append)
    try:
        assert runner._own_plugin("session-x", settings) is None
    finally:
        _EMIT.reset(token)
    [event] = seen
    assert event["type"] == "plugin_unavailable"
    assert event["message"].startswith("ScadBuddy's own plugin was not loaded: ")
    [panel] = translate("s", event, {})
    assert (panel["type"], panel["code"]) == ("error", "plugin_unavailable")
    assert panel["message"] == event["message"]


def test_own_plugin_copies_are_swept(tmp_path: Path) -> None:
    """m5: as the agent sweeps its package copies (turnPackages.ts sweepPackageCopies): a copy
    no segment used for a week, and a half-made one after an hour; a used one is kept."""
    plugin = tmp_path / "scadbuddy"
    shutil.copytree(OWN_SKILLS, plugin / "skills")
    root = tmp_path / "own"
    used = Path(own_plugin_copy(str(plugin), frozenset({"skill:print"}), str(root))).parent
    stale = root / ("e" * 64)
    (stale / "scadbuddy").mkdir(parents=True)
    half = root / ".tmp-dead"
    half.mkdir()
    fresh = root / ".tmp-live"
    fresh.mkdir()
    now = time.time()
    week = 7 * 24 * 3600
    os.utime(stale, (now - week - 60, now - week - 60))
    os.utime(half, (now - 3700, now - 3700))
    os.utime(used, (now - week - 60, now - week - 60))
    # Using a copy touches it, and the sweep runs before it is used.
    assert Path(own_plugin_copy(str(plugin), frozenset({"skill:print"}), str(root))).parent == used
    assert sorted(p.name for p in root.iterdir()) == sorted([used.name, ".tmp-live"])
    later = sweep_own_plugin_copies(str(root), now + week + 120)
    assert sorted(later) == sorted([used.name, ".tmp-live"])


def _skill_package(connect: Connect, root: Path) -> StandInTools:
    """Stages a package ``notes`` with one skill under ``root`` and answers the stand-in that
    offers it to the turn with Skill named (so, no row: outward)."""
    staging = root / "staging" / "notes"
    (staging / "skills" / "jot").mkdir(parents=True)
    (staging / "skills" / "jot" / "SKILL.md").write_text("---\nname: jot\n---\nJot.\n")
    digest = hash_tree(staging)
    hex_ = digest.removeprefix("sha256:")
    (root / "staging").rename(root / hex_)
    stand_in = StandInTools(connect)
    stand_in.turn_packages = [
        {
            "name": "notes",
            "dir": f"{hex_}/notes",
            "content_hash": digest,
            "allowed": True,
            "builtin_tools": ["Skill"],
            "named_builtins": ["Skill"],
            "items": ["skill:jot"],
        }
    ]
    return stand_in


def _claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty Claude config dir and home for the bundled Claude Code; the cwd it runs in."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    return cwd


async def _skill_approved_then_denied(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill_input: dict[str, Any],
    payload_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], WorkflowHistory]:
    """The bundled Claude Code calls Skill with ``skill_input`` twice in a turn whose allowed
    package names Skill (no row: outward); the first wait at the gate is approved, the
    second denied. Answers the main thread's last tool results and the session's history."""
    root = (payload_dir or tmp_path) / "plugins"
    stand_in = _skill_package(connect, root)
    main: list[list[dict[str, Any]]] = []
    call = {"name": "Skill", "input": skill_input}

    def reply(body: dict[str, Any]) -> dict[str, Any]:
        if "Skill" not in [t.get("name") for t in body.get("tools") or []]:
            return {"text": "ok"}
        results = _results(body)
        main.append(results)
        return {"tool_use": call} if len(results) < 2 else {"text": "All done."}

    fake = FakeAnthropic(reply).start()
    cwd = _claude_home(tmp_path, monkeypatch)
    try:
        await insert_gateway(agent_db, KEK, fake.url)
        runner = ScadBuddyRunner(
            connect,
            KEK,
            cwd=os.path.realpath(cwd),
            plugin_dir=str(_own_plugin(tmp_path)),
            package_root=str(root),
            make_runner=ClaudeAgentSdkRunner,
            own_plugin_root=str(tmp_path / "own"),
        )
        async with running(temporal_env, connect, runner, stand_in, payload_dir) as (
            client,
            queue,
        ):
            sid = await insert_session(agent_db)
            handle = await start_session(client, sid, queue)
            try:
                await send(handle, "go")
                for decision in ("approve", "deny"):
                    entries: list[dict[str, Any]] = []

                    async def parked() -> bool:
                        nonlocal entries
                        entries = [
                            e
                            for e in await handle.query(PENDING_INPUT_QUERY)
                            if e["tool"] == "Skill"
                        ]
                        return bool(entries)

                    await until(parked, timeout=120)
                    [entry] = entries
                    await handle.execute_update(
                        RESPOND_UPDATE,
                        {
                            "request_id": entry["id"],
                            "response": {"kind": "approval", "decision": decision},
                            "responder": {"kind": "browser", "id": "browser", "label": "You"},
                            "role": "browser",
                        },
                    )
                    if decision == "approve":
                        await until(lambda: _seen_results(main, 1), timeout=120)
                assert await settled(connect, sid, timeout=180) == "idle"
                history = await handle.fetch_history()
            finally:
                await handle.terminate()
    finally:
        fake.close()
        await asyncio.sleep(0)
    return next(r for r in reversed(main) if len(r) >= 2), history


def _segment_attempts(history: WorkflowHistory) -> list[int]:
    """Each activity attempt the history started: a segment retried would run a call again."""
    return [
        e.activity_task_started_event_attributes.attempt
        for e in history.events
        if e.HasField("activity_task_started_event_attributes")
    ]


async def test_an_outward_package_skill_parks_in_a_real_segment_and_runs_once_approved(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review round 2, end to end: each call waits at the gate in the segment's hook; the
    first, approved, runs; the second, denied, is refused with why."""
    results, _ = await _skill_approved_then_denied(
        temporal_env, agent_db, connect, tmp_path, monkeypatch, {"skill": "scadbuddy:print"}
    )
    first, second = results
    assert not first.get("is_error"), first
    assert second.get("is_error") is True
    assert "denied" in _text_of(second), _text_of(second)


async def test_an_outward_package_skill_over_the_offload_size_parks_and_runs_once_approved(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#5000, end to end: a Skill call whose input is over OFFLOAD_BYTES (once refused
    unasked) parks as a classic turn's does; the worker's External Storage keeps the input
    out of history (the Update's argument is a reference). Approved, it runs once (one
    segment attempt, one result); denied, it is refused with why."""
    payloads = tmp_path / "payloads"
    big = {"skill": "scadbuddy:print", "args": "a long brief " + "q" * (OFFLOAD_BYTES * 2)}
    results, history = await _skill_approved_then_denied(
        temporal_env, agent_db, connect, tmp_path, monkeypatch, big, payloads
    )
    first, second = results
    assert not first.get("is_error"), first
    assert second.get("is_error") is True
    assert "denied" in _text_of(second), _text_of(second)
    assert "too large" not in _text_of(second)
    assert all(attempt == 1 for attempt in _segment_attempts(history))
    # Each builtin_call Update's argument is a reference to the stored input, and no event
    # holds a payload over the threshold (the input is sealed, so its text never shows).
    inputs = [
        e.workflow_execution_update_accepted_event_attributes.accepted_request.input
        for e in history.events
        if e.HasField("workflow_execution_update_accepted_event_attributes")
    ]
    args = [p for i in inputs if i.name == BUILTIN_CALL_UPDATE for p in i.args.payloads]
    assert len(args) == 2
    assert all(len(a.external_payloads) == 1 and a.ByteSize() < 4096 for a in args)
    assert max(e.ByteSize() for e in history.events) < OFFLOAD_BYTES + 4096


async def test_a_gate_that_raises_in_a_real_segment_refuses_the_call(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#5017 security review, end to end: the bundled Claude Code's Skill call, whose gate
    raises, is refused, never run."""
    root = tmp_path / "plugins"
    stand_in = _skill_package(connect, root)

    async def raises(self: CallAccess, data: Any) -> dict[str, Any]:
        raise RuntimeError("the gate broke")

    monkeypatch.setattr(CallAccess, "check", raises)
    main: list[list[dict[str, Any]]] = []
    call = {"name": "Skill", "input": {"skill": "scadbuddy:print"}}

    def reply(body: dict[str, Any]) -> dict[str, Any]:
        if "Skill" not in [t.get("name") for t in body.get("tools") or []]:
            return {"text": "ok"}
        results = _results(body)
        main.append(results)
        return {"tool_use": call} if not results else {"text": "All done."}

    fake = FakeAnthropic(reply).start()
    cwd = _claude_home(tmp_path, monkeypatch)
    try:
        await insert_gateway(agent_db, KEK, fake.url)
        runner = ScadBuddyRunner(
            connect,
            KEK,
            cwd=os.path.realpath(cwd),
            plugin_dir=str(_own_plugin(tmp_path)),
            package_root=str(root),
            make_runner=ClaudeAgentSdkRunner,
            own_plugin_root=str(tmp_path / "own"),
        )
        async with running(temporal_env, connect, runner, stand_in) as (client, queue):
            sid = await insert_session(agent_db)
            handle = await start_session(client, sid, queue)
            try:
                await send(handle, "go")
                assert await settled(connect, sid, timeout=180) == "idle"
            finally:
                await handle.terminate()
    finally:
        fake.close()
        await asyncio.sleep(0)
    [result] = next(r for r in reversed(main) if r)
    assert result.get("is_error") is True, result
    assert "its access could not be checked" in _text_of(result), _text_of(result)


async def _seen_results_async(main: list[list[dict[str, Any]]], n: int) -> bool:
    return any(len(r) >= n for r in main)


def _seen_results(main: list[list[dict[str, Any]]], n: int) -> Any:
    return _seen_results_async(main, n)
