"""ScadBuddy's tools for a durable segment's subagents (#2320, session/subagents.py)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fake_anthropic import FakeAnthropic
from mcp.server.mcpserver import MCPServer
from session_support import Conn, Connect, events, insert_session, send, settled, start_session
from temporalio.claude_agent_sdk import ClaudeAgentSdkRunner
from temporalio.testing import WorkflowEnvironment
from test_session_runner import KEK, insert_gateway, running

from scadbuddy_durable.secrets import Kek
from scadbuddy_durable.session import subagents
from scadbuddy_durable.session.runner import ScadBuddyRunner

# agent/src/temporal/subagentToken.ts with the key bytes 0..31, measured 2026-10-11 (tsx).
TS_KEY = "bc894da44adc3eb7b9ae28157c522fa844548a85811fd015f3d5203ab63f4aa3"
TS_TOKEN = (
    "sbsub.0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60.6a1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d."
    "4d72ea7011b7aa59c4cf65dcd2dec101.2000000000."
    "bb5d2abba38b8701b5871972ff9f9e1798e42bdabec63cc455cb68926fed1744"
)
VECTOR_KEK = Kek(id="k", key=bytes(range(32)))


def test_the_key_and_token_are_the_agents() -> None:
    key = subagents.subagent_key(VECTOR_KEK)
    assert key.hex() == TS_KEY
    token = subagents.mint(
        key,
        "0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60",
        "6a1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        "bearer",
        "token:7",
        2000000000,
    )
    assert token == TS_TOKEN


REPO = Path(__file__).resolve().parents[2]
AGENTS = REPO / "plugins" / "scadbuddy" / "agents"


def test_the_default_url_is_the_agents_port_and_path() -> None:
    main = (REPO / "agent" / "src" / "main.ts").read_text()
    route = (REPO / "agent" / "src" / "routes" / "subagentMcp.ts").read_text()
    port = re.search(r"^const PORT = (\d+)$", main, re.M)
    path = re.search(r"^export const SUBAGENT_MCP_PATH = '([^']+)'$", route, re.M)
    assert port and path
    assert f"http://127.0.0.1:{port[1]}{path[1]}" == subagents.DEFAULT_URL
    assert not path[1].startswith("/api/v1/ai/")  # the ingress routes that prefix to the agent


def test_the_plugins_subagents_are_read_as_they_declare_themselves() -> None:
    agents = subagents.load_agents(str(AGENTS))
    assert sorted(agents) == ["model-author", "print-analyst"]
    author = agents["model-author"]
    assert author.skills == ["scadbuddy:authoring", "scadbuddy:customize"]
    assert author.maxTurns == 40
    assert author.tools is None  # inherits the session's
    assert author.prompt.startswith("You author ScadBuddy templates")
    for agent in agents.values():
        assert "Bash" in (agent.disallowedTools or [])
        assert "WebFetch" in (agent.disallowedTools or [])
    assert "mcp__scadbuddy__print_output" in (agents["print-analyst"].disallowedTools or [])
    served = subagents.with_server(agents, "http://127.0.0.1:1/x", "tok")
    assert served["model-author"].mcpServers == [
        {"scadbuddy": subagents.server_config("http://127.0.0.1:1/x", "tok")}
    ]
    assert agents["model-author"].mcpServers is None  # the loaded ones are left alone


@pytest.mark.requires_postgres
async def test_a_segment_gives_its_subagents_the_loopback_server_for_its_turn(
    agent_db: Conn, connect: Connect, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    kek = Kek(id="k", key=b"\1" * 32)
    runner = ScadBuddyRunner(
        connect, kek, cwd=str(tmp_path), subagent_url="http://127.0.0.1:1/x", agents_dir=str(AGENTS)
    )
    sid = await insert_session(agent_db)
    extra: dict[str, Any] = {"allowed_tools": ["mcp__scadbuddy_images__view_user_images"]}
    # No running turn: nothing to give a token for.
    await runner._subagent_tools(sid, extra)
    assert "agents" not in extra
    turn = "6a1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    await agent_db.execute("UPDATE ai_sessions SET durable_turn_id = %s WHERE id = %s", (turn, sid))
    await runner._subagent_tools(sid, extra)
    # The subagents' own: the main agent gets no server, and no hook is needed to keep it off.
    assert "mcp_servers" not in extra and "hooks" not in extra
    [server] = extra["agents"]["model-author"].mcpServers
    assert server["scadbuddy"]["url"] == "http://127.0.0.1:1/x"
    token = server["scadbuddy"]["headers"]["Authorization"].removeprefix("Bearer ")
    parts = token.split(".")
    assert parts[:4] == ["sbsub", sid, turn, subagents.owner_digest("browser", "browser")]
    assert extra["allowed_tools"] == ["mcp__scadbuddy_images__view_user_images", "mcp__scadbuddy"]
    # Settings names another URL (agent-durable outside the agent's pod): it replaces the
    # worker's own, read at each segment; a value the agent could not have stored does not.
    # A URL set but refused is warned of with why, never with the URL (#3054).
    for stored, expected, problem in (
        (
            '{"url": "http://agent.svc:8081/internal/durable/subagent-mcp", "peers": []}',
            "http://agent.svc:8081/internal/durable/subagent-mcp",
            None,
        ),
        ('{"url": null, "peers": []}', "http://127.0.0.1:1/x", None),
        ('"nonsense"', "http://127.0.0.1:1/x", None),
        # Not http(s) with a host: the worker's own (#2883).
        ('{"url": "file:///etc/passwd", "peers": []}', "http://127.0.0.1:1/x", "scheme 'file'"),
        (
            '{"url": "javascript:alert(1)", "peers": []}',
            "http://127.0.0.1:1/x",
            "scheme 'javascript'",
        ),
        ('{"url": "http://", "peers": []}', "http://127.0.0.1:1/x", "no host"),
        ('{"url": "http://[::1", "peers": []}', "http://127.0.0.1:1/x", "unparseable"),
        ('{"url": "agent.svc:8081/mcp", "peers": []}', "http://127.0.0.1:1/x", "no scheme"),
        ('{"url": "http:agent.svc/mcp", "peers": []}', "http://127.0.0.1:1/x", "no host"),
    ):
        caplog.clear()
        await agent_db.execute(
            "INSERT INTO ai_settings (key, value) VALUES ('durable_subagents', %s::jsonb)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (stored,),
        )
        again: dict[str, Any] = {}
        await runner._subagent_tools(sid, again)
        [server] = again["agents"]["model-author"].mcpServers
        assert server["scadbuddy"]["url"] == expected
        warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        if problem is None:
            assert warned == []
        else:
            [message] = warned
            assert f"({problem})" in message
            assert (
                "etc/passwd" not in message
                and "alert" not in message
                and "agent.svc" not in message
            )
    # Off, or no definitions: no subagents, whatever Settings names.
    for off_runner in (
        ScadBuddyRunner(connect, kek, cwd=str(tmp_path), agents_dir=str(AGENTS)),
        ScadBuddyRunner(connect, kek, cwd=str(tmp_path), subagent_url="http://127.0.0.1:1/x"),
    ):
        off: dict[str, Any] = {}
        await off_runner._subagent_tools(sid, off)
        assert off == {}


class StandInServer:
    """The agent's loopback server as a subagent reaches it: one tool, and the
    Authorization each request carried."""

    def __init__(self) -> None:
        self.authorization: list[str] = []
        self.calls = 0

    def start(self) -> StandInServer:
        mcp = MCPServer("scadbuddy")

        @mcp.tool()
        def probe_model() -> str:
            self.calls += 1
            return "probed"

        app = mcp.streamable_http_app(stateless_http=True, json_response=True)

        async def recording(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                self.authorization.append(
                    dict(scope["headers"]).get(b"authorization", b"").decode()
                )
            await app(scope, receive, send)

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.server = uvicorn.Server(
            uvicorn.Config(recording, host="127.0.0.1", port=port, log_level="error")
        )
        threading.Thread(target=self.server.run, daemon=True).start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        return self

    def close(self) -> None:
        self.server.should_exit = True


def _tool_names(body: dict[str, Any]) -> list[str]:
    return [str(t.get("name")) for t in body.get("tools") or []]


def _answered(body: dict[str, Any]) -> bool:
    """Whether the conversation holds a tool result (the script's one call was answered)."""
    return any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
        for m in body.get("messages") or []
    )


@pytest.mark.requires_postgres
@pytest.mark.requires_temporal
async def test_the_bundled_claude_code_gives_the_server_to_the_subagent_only(
    temporal_env: WorkflowEnvironment,
    agent_db: Conn,
    connect: Connect,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2394, #2562: the definitions load, a subagent's request lists the server's tool
    and its call reaches the server with the segment's token, and the main agent's
    request lists no `mcp__scadbuddy__` tool."""
    subagent_prompt = "You author ScadBuddy templates"

    def reply(body: dict[str, Any]) -> dict[str, Any]:
        if not body.get("tools"):
            return {"text": "ok"}  # Claude Code's own side requests
        if subagent_prompt in json.dumps(body.get("system")):
            if _answered(body):
                return {"text": "The model probed fine."}
            return {"tool_use": {"name": "mcp__scadbuddy__probe_model", "input": {}}}
        if _answered(body):
            return {"text": "All done."}
        return {
            "tool_use": {
                "name": "Agent",
                "input": {
                    "subagent_type": "model-author",
                    "description": "Probe the model",
                    "prompt": "Probe the model and report.",
                },
            }
        }

    fake = FakeAnthropic(reply).start()
    stand_in = StandInServer().start()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # The image's layout: the own plugin's skills, its subagents outside it.
    plugin = tmp_path / "scadbuddy"
    plugin.mkdir()
    (plugin / "skills").symlink_to(REPO / "plugins" / "scadbuddy" / "skills")
    try:
        await insert_gateway(agent_db, KEK, fake.url)
        runner = ScadBuddyRunner(
            connect,
            KEK,
            cwd=os.path.realpath(cwd),
            plugin_dir=str(plugin),
            subagent_url=stand_in.url,
            agents_dir=str(AGENTS),
            make_runner=ClaudeAgentSdkRunner,
        )
        async with running(temporal_env, connect, runner) as (client, queue):
            sid = await insert_session(agent_db)
            turn = str(uuid.uuid4())
            await agent_db.execute(
                "UPDATE ai_sessions SET durable_turn_id = %s WHERE id = %s", (turn, sid)
            )
            handle = await start_session(client, sid, queue)
            try:
                await send(handle, "probe the model")
                assert await settled(connect, sid, timeout=180) == "idle"
            finally:
                await handle.terminate()
        bodies = [b for b in fake.messages() if b.get("tools")]
        sub = [b for b in bodies if subagent_prompt in json.dumps(b.get("system"))]
        main = [b for b in bodies if b not in sub]
        assert main and sub
        for body in main:
            assert "Agent" in _tool_names(body)
            assert not [n for n in _tool_names(body) if n.startswith(subagents.PREFIX)]
        assert "mcp__scadbuddy__probe_model" in _tool_names(sub[0])
        assert stand_in.calls == 1
        assert stand_in.authorization
        for authorization in stand_in.authorization:
            assert authorization.startswith(f"Bearer sbsub.{sid}.{turn}.")
        log = await events(agent_db, sid)
        text = "".join(e["delta"] for e in log if e["type"] == "assistant.text.delta")
        assert "All done." in text
    finally:
        fake.close()
        stand_in.close()
        await asyncio.sleep(0)
