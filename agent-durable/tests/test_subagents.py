"""ScadBuddy's tools for a durable segment's subagents (#2320, session/subagents.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from session_support import Conn, Connect, insert_session

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


async def _decide(event: dict[str, Any]) -> Any:
    [matcher] = subagents.hooks()["PreToolUse"]
    return await matcher.hooks[0](event, None, {"signal": None})  # type: ignore[arg-type]


async def test_the_main_agent_is_kept_to_its_durable_tools() -> None:
    main = await _decide({"tool_name": "mcp__scadbuddy__render_model"})
    assert main["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "mcp__durable__" in main["hookSpecificOutput"]["permissionDecisionReason"]
    assert await _decide({"tool_name": "mcp__scadbuddy__render_model", "agent_id": "a1"}) == {}
    assert await _decide({"tool_name": "mcp__durable__render_model"}) == {}


@pytest.mark.requires_postgres
async def test_a_segment_gives_its_subagents_the_loopback_server_for_its_turn(
    agent_db: Conn, connect: Connect, tmp_path: Path
) -> None:
    kek = Kek(id="k", key=b"\1" * 32)
    runner = ScadBuddyRunner(connect, kek, cwd=str(tmp_path), subagent_url="http://127.0.0.1:1/x")
    sid = await insert_session(agent_db)
    extra: dict[str, Any] = {"allowed_tools": ["mcp__scadbuddy_images__view_user_images"]}
    # No running turn: nothing to give a token for.
    await runner._subagent_tools(sid, extra)
    assert "mcp_servers" not in extra
    turn = "6a1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    await agent_db.execute("UPDATE ai_sessions SET durable_turn_id = %s WHERE id = %s", (turn, sid))
    await runner._subagent_tools(sid, extra)
    server = extra["mcp_servers"]["scadbuddy"]
    assert server["url"] == "http://127.0.0.1:1/x"
    token = server["headers"]["Authorization"].removeprefix("Bearer ")
    parts = token.split(".")
    assert parts[:4] == ["sbsub", sid, turn, subagents.owner_digest("browser", "browser")]
    assert extra["allowed_tools"] == ["mcp__scadbuddy_images__view_user_images", "mcp__scadbuddy"]
    assert "PreToolUse" in extra["hooks"]
    # Off: no server.
    off: dict[str, Any] = {}
    await ScadBuddyRunner(connect, kek, cwd=str(tmp_path))._subagent_tools(sid, off)
    assert off == {}
