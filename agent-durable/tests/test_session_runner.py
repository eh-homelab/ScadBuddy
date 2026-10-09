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
from scadbuddy_durable.session.runner import NO_CREDENTIAL, VIEW_IMAGES_TOOL, ScadBuddyRunner
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
