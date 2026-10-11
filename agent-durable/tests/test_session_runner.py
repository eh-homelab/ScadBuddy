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
from typing import Any

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
    settled,
    start_session,
    status,
    tools_worker,
)
from temporalio.claude_agent_sdk import ClaudeAgentSdkRunner, SegmentInput, SegmentOutput
from temporalio.claude_agent_sdk.testing import Final, ScriptedClaude
from temporalio.client import Client
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from scadbuddy_durable.codec import PgPayloadKeys, data_converter
from scadbuddy_durable.credentials import credential_aad
from scadbuddy_durable.secrets import Kek, kek_from_base64, seal_bytes
from scadbuddy_durable.session import tools
from scadbuddy_durable.session.models import ImageRef
from scadbuddy_durable.session.runner import (
    IMAGE_TOO_LARGE,
    NO_CREDENTIAL,
    VIEW_IMAGES_TOOL,
    Image,
    ScadBuddyRunner,
    image_problem,
    image_size,
)
from scadbuddy_durable.worker import build_worker

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

KEK = kek_from_base64("WlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlo=")
TOKEN = "sk-test-gateway-token-0123456789"
# A 1x1 red PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


async def insert_gateway(conn: Conn, kek: Kek, base_url: str, secret: str = TOKEN) -> None:
    cid = str(uuid.uuid4())
    aad = credential_aad(cid, "gateway", base_url)
    dek = os.urandom(32)
    await conn.execute(
        "INSERT INTO ai_credentials (id, priority, kind, base_url, secret_sealed, dek_sealed,"
        " kek_id, last4, status) VALUES (%s, 0, 'gateway', %s, %s, %s, %s, '', 'active')",
        (
            cid,
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
    # No images, no skills: Claude Code's prompt with the session's append.
    assert made["extra_options"] == {
        "system_prompt": {"type": "preset", "preset": "claude_code", "append": "Be careful."}
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
