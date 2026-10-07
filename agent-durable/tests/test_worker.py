"""The worker process (task 11): registrations, codec, /healthz and SIGTERM."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool
from temporalio import activity, workflow
from temporalio.client import Client

from scadbuddy_durable import runner as runner_module
from scadbuddy_durable import worker as worker_module
from scadbuddy_durable.codec import SubjectPayloadCodec
from scadbuddy_durable.secrets import Kek
from scadbuddy_durable.tools import TOOLS
from scadbuddy_durable.worker import Health, WorkerDeps, build_worker, connect_temporal, serve_health

ROOT = Path(__file__).parents[1]
KEK = Kek(id="test0000test0000", key=os.urandom(32))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def healthz(port: int) -> tuple[int, dict[str, Any]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), 30)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


@pytest_asyncio.fixture
async def unused_pool() -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool("postgresql://unused.invalid/none", open=False)
    yield pool


@pytest.mark.requires_temporal
async def test_build_worker_registers_the_workflow_and_its_activities(
    temporal_env: Client, unused_pool: AsyncConnectionPool, tmp_path: Path
) -> None:
    deps = WorkerDeps(pool=unused_pool, keks=[KEK], prompt_append="policy", cwd=str(tmp_path))
    client = worker_module.client_with_codec(temporal_env, unused_pool, [KEK])
    w = build_worker(client, deps)
    config = w.config(active_config=True)
    assert config["task_queue"] == "agent"
    assert config["max_concurrent_activities"] == 4
    workflows = [workflow._Definition.from_class(c) for c in config.get("workflows", [])]  # pyright: ignore[reportPrivateUsage]
    assert [d.name for d in workflows if d is not None] == ["DurableSession"]
    names = sorted(
        str(d.name)
        for d in (activity._Definition.from_callable(a) for a in config.get("activities", []))  # pyright: ignore[reportPrivateUsage]
        if d is not None
    )
    assert names == [
        "durable_load_inputs",
        "durable_save_snapshot",
        "durable_start_input",
        "run_claude_segment",
    ]
    assert not {t.name for t in TOOLS} & set(names)  # tool stubs are served by TypeScript
    codec = w.client.data_converter.payload_codec
    assert isinstance(codec, SubjectPayloadCodec)


def test_segments_run_in_srv_agent_when_it_exists(
    unused_pool: AsyncConnectionPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "srv-agent"
    image.mkdir()
    monkeypatch.setattr(runner_module, "CWD", str(image))
    monkeypatch.chdir(tmp_path)
    assert WorkerDeps(pool=unused_pool, keks=[KEK], prompt_append="policy").cwd == str(image)


def test_segments_run_in_the_working_directory_without_srv_agent(
    unused_pool: AsyncConnectionPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "CWD", str(tmp_path / "none"))
    monkeypatch.chdir(tmp_path)
    assert WorkerDeps(pool=unused_pool, keks=[KEK], prompt_append="policy").cwd == str(tmp_path)


async def test_healthz_is_starting_until_temporal_connects() -> None:
    health = Health(database_probe=None)
    port = _free_port()
    server = await serve_health(health, port, host="127.0.0.1")
    gate, retried = asyncio.Event(), asyncio.Event()
    attempts = 0
    sentinel: Any = object()

    async def connect() -> Client:
        nonlocal attempts
        attempts += 1
        if attempts >= 2:
            retried.set()
        if not gate.is_set():
            raise ConnectionError("not yet")
        return sentinel  # type: ignore[no-any-return]

    try:
        task = asyncio.create_task(connect_temporal(connect, health, first_delay=0.05, max_delay=0.1))
        await asyncio.wait_for(retried.wait(), 30)
        status, body = await healthz(port)
        assert (status, body["status"], body["temporal"]) == (503, "starting", "connecting")
        gate.set()
        assert await asyncio.wait_for(task, 30) is sentinel
        status, body = await healthz(port)
        assert (status, body) == (200, {"status": "ok", "temporal": "ok", "database": "ok"})
    finally:
        server.close()
        await server.wait_closed()


async def test_healthz_is_unavailable_when_the_database_is() -> None:
    async def down() -> None:
        raise OSError("refused")

    health = Health(database_probe=down)
    health.temporal = "ok"
    port = _free_port()
    server = await serve_health(health, port, host="127.0.0.1")
    try:
        status, body = await healthz(port)
        assert (status, body["status"], body["database"]) == (503, "unavailable", "unavailable")
    finally:
        server.close()
        await server.wait_closed()


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCADBUDDY_")}
    env.update(extra)
    return env


async def _until(check: Callable[[], Awaitable[bool]], within: float = 60) -> None:
    deadline = time.monotonic() + within
    while not await check():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.2)


async def _healthy(port: int, want: str) -> bool:
    try:
        return (await healthz(port))[1].get("status") == want
    except (OSError, ValueError, IndexError):
        return False


def _tools_dir(tmp_path: Path) -> Path:
    tools = tmp_path / "dist"
    tools.mkdir()
    (tools / "tools.json").write_text((ROOT / "tests/fixtures/tools.json").read_text())
    (tools / "durable-prompt.txt").write_text("policy")
    return tools


async def test_main_refuses_to_start_without_a_tool_manifest(tmp_path: Path) -> None:
    missing = tmp_path / "none" / "tools.json"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scadbuddy_durable.worker",
        cwd=ROOT,
        env=_env(SCADBUDDY_AGENT_TOOLS_MANIFEST=str(missing)),
        stderr=subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(proc.communicate(), 60)
    assert proc.returncode == 1
    assert f"no tool manifest at {missing}" in err.decode()


async def test_main_without_a_database_serves_the_disabled_health(tmp_path: Path) -> None:
    port = _free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scadbuddy_durable.worker",
        cwd=ROOT,
        env=_env(
            SCADBUDDY_AGENT_TOOLS_MANIFEST=str(_tools_dir(tmp_path) / "tools.json"),
            SCADBUDDY_AGENT_DURABLE_HEALTH_PORT=str(port),
        ),
    )
    try:
        await _until(lambda: _healthy(port, "ok"))
        assert (await healthz(port)) == (200, {"status": "ok", "durable": "disabled (no database)"})
        proc.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(proc.wait(), 30) == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.requires_postgres
@pytest.mark.requires_temporal
async def test_sigterm_shuts_the_worker_down_and_stops_the_projector(
    pg_conninfo: str, temporal_env: Client, _temporal_server: str, tmp_path: Path
) -> None:
    sid = str(uuid.uuid4())
    async with AsyncConnectionPool(pg_conninfo, open=False) as pool, pool.connection() as conn:
        await conn.execute(
            "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
            " creator_id, status, max_turns, budget_usd, mode) VALUES (%s, 'chat', 'u', 'u', 'u', 'u',"
            " 'u', 'running', 7, 2.5, 'durable')",
            (sid,),
        )
        # As the agent service's claim leaves it: its send in flight (manager.ts claimDurable).
        await conn.execute(
            "INSERT INTO ai_durable_streams (session_id, sending) VALUES (%s, 'agent-send')", (sid,)
        )
    key_file = tmp_path / "kek"
    key_file.write_text(base64.b64encode(os.urandom(32)).decode())
    port = _free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scadbuddy_durable.worker",
        cwd=ROOT,
        env=_env(
            SCADBUDDY_AGENT_TOOLS_MANIFEST=str(_tools_dir(tmp_path) / "tools.json"),
            SCADBUDDY_AGENT_DURABLE_HEALTH_PORT=str(port),
            SCADBUDDY_DATABASE_URL=pg_conninfo,
            SCADBUDDY_SECRET_KEY_FILE=str(key_file),
            SCADBUDDY_TEMPORAL_ADDRESS=_temporal_server,
            SCADBUDDY_TEMPORAL_NAMESPACE="default",
        ),
        stderr=subprocess.PIPE,
    )

    async def holder() -> str | None:
        async with AsyncConnectionPool(pg_conninfo, open=False) as pool, pool.connection() as conn:
            cur = await conn.execute("SELECT holder FROM ai_durable_streams WHERE session_id = %s", (sid,))
            row = await cur.fetchone()
        return None if row is None else row[0]

    async def claimed() -> bool:
        return await holder() is not None

    try:
        await _until(lambda: _healthy(port, "ok"))
        await _until(claimed)  # the projector runs and holds the session's lease
        proc.send_signal(signal.SIGTERM)
        started = time.monotonic()
        assert proc.stderr is not None
        err = (await asyncio.wait_for(proc.stderr.read(), 60)).decode()
        assert await asyncio.wait_for(proc.wait(), 30) == 0
        assert time.monotonic() - started < 30  # generous: the host clock jumps
        assert "worker shut down" in err
        assert "projector stopped" in err
        assert await holder() is None  # the projector released its lease on the way out
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
