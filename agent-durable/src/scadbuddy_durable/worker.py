"""The ``agent`` queue's worker: DurableSession, its activities, the segment runner.

Configuration is the agent service's infrastructure variables plus three of this
container's (spec 2026-10-01 §6.3a):

- ``SCADBUDDY_DATABASE_URL``, ``SCADBUDDY_SECRET_KEY_FILE`` (and
  ``SCADBUDDY_SECRET_KEY_PREVIOUS_FILE`` during a rotation), ``SCADBUDDY_TEMPORAL_ADDRESS``
  and ``SCADBUDDY_TEMPORAL_NAMESPACE``, as the agent reads them;
- ``SCADBUDDY_DURABLE_TOOLS_JSON``: the agent's tool manifest (``dist/tools.json``);
- ``SCADBUDDY_DURABLE_CWD``: the engine's working directory (default ``/srv/agent``);
- ``SCADBUDDY_DURABLE_SKILLS_DIR``: a plugin directory the engine loads skills from
  (optional);
- ``SCADBUDDY_DURABLE_AGENTS_DIR``: ScadBuddy's subagent definitions
  (``plugins/scadbuddy/agents``), given to each segment with the agent's loopback MCP
  server inline (``session/subagents.py``; optional: unset, a segment has no subagents);
- ``SCADBUDDY_DURABLE_SUBAGENT_MCP_URL``: that server, by default
  ``http://127.0.0.1:8081/internal/durable/subagent-mcp`` (the agent container in the
  same pod); ``off`` gives the subagents no server;
- ``SCADBUDDY_PAYLOAD_DIR``: where a session's large payloads are kept, on the data
  share the agent mounts at the same path (``payload_store``). Unset, they stay in
  history, and the plugin fails a turn whose conversation outgrows 2 MB;
- ``SCADBUDDY_DURABLE_SCRIPTED=1``: tests only. The segment runner is a scripted model
  (``session/scripted.py``) instead of Claude, for the agent service's end-to-end test.

The client and the worker seal every payload of a durable subject (``codec``), and keep a
session's large payloads on the data share, history holding a reference
(``payload_store``).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from temporalio.claude_agent_sdk import ClaudeAgentPlugin
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from scadbuddy_durable.codec import PgPayloadKeys, data_converter
from scadbuddy_durable.gate.activities import GateActivities
from scadbuddy_durable.payload_store import external_storage
from scadbuddy_durable.secrets import Kek, load_kek
from scadbuddy_durable.session import subagents, tools
from scadbuddy_durable.session.activities import SessionActivities
from scadbuddy_durable.session.events import SessionEvents
from scadbuddy_durable.session.models import TASK_QUEUE
from scadbuddy_durable.session.runner import ScadBuddyRunner, SegmentRunner
from scadbuddy_durable.session.scripted import scripted_runner
from scadbuddy_durable.session.workflow import DurableSession

Connect = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[Any]]]


@dataclass(frozen=True)
class Config:
    database_url: str
    temporal_address: str
    namespace: str
    tools_json: str
    cwd: str
    skills_dir: str | None
    kek_file: str
    previous_kek_file: str | None
    scripted: bool = False
    payload_dir: str | None = None
    # The agent's loopback MCP server for subagents (session/subagents.py); "off" disables it.
    subagent_url: str | None = None
    # ScadBuddy's subagent definitions (session/subagents.py `load_agents`).
    agents_dir: str | None = None

    def __repr__(self) -> str:  # the database URL can carry a password
        return f"Config(temporal={self.temporal_address}, namespace={self.namespace})"


def config_from_env(env: dict[str, str] | None = None) -> Config:
    e = os.environ if env is None else env
    missing = [
        k
        for k in (
            "SCADBUDDY_DATABASE_URL",
            "SCADBUDDY_TEMPORAL_ADDRESS",
            "SCADBUDDY_SECRET_KEY_FILE",
            "SCADBUDDY_DURABLE_TOOLS_JSON",
        )
        if not e.get(k)
    ]
    if missing:
        raise ValueError(f"the durable worker needs {', '.join(missing)}")
    return Config(
        database_url=e["SCADBUDDY_DATABASE_URL"],
        temporal_address=e["SCADBUDDY_TEMPORAL_ADDRESS"],
        namespace=e.get("SCADBUDDY_TEMPORAL_NAMESPACE") or "default",
        tools_json=e["SCADBUDDY_DURABLE_TOOLS_JSON"],
        cwd=e.get("SCADBUDDY_DURABLE_CWD") or "/srv/agent",
        skills_dir=e.get("SCADBUDDY_DURABLE_SKILLS_DIR") or None,
        kek_file=e["SCADBUDDY_SECRET_KEY_FILE"],
        previous_kek_file=e.get("SCADBUDDY_SECRET_KEY_PREVIOUS_FILE") or None,
        scripted=e.get("SCADBUDDY_DURABLE_SCRIPTED") == "1",
        payload_dir=e.get("SCADBUDDY_PAYLOAD_DIR") or None,
        subagent_url=_subagent_url(e.get("SCADBUDDY_DURABLE_SUBAGENT_MCP_URL")),
        agents_dir=e.get("SCADBUDDY_DURABLE_AGENTS_DIR") or None,
    )


def _subagent_url(raw: str | None) -> str | None:
    if raw == "off":
        return None
    return raw or subagents.DEFAULT_URL


def connector(url: str, search_path: str | None = None) -> Connect:
    @asynccontextmanager
    async def connect() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
        try:
            if search_path:
                await conn.execute(f"SET search_path TO {search_path}")
            yield conn
        finally:
            await conn.close()

    return connect


def build_worker(
    client: Client,
    connect: Connect,
    runner: SegmentRunner,
    *,
    task_queue: str = TASK_QUEUE,
    session: SessionActivities | None = None,
    gate: GateActivities | None = None,
) -> Worker:
    gate = gate or GateActivities(connect)
    session = session or SessionActivities(connect)
    events = SessionEvents(connect)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[DurableSession],
        activities=[
            gate.open_input,
            gate.grant_input,
            gate.resolve_input,
            session.gate_settings,
            session.finish_turn,
            events.follow_session,
        ],
        plugins=[ClaudeAgentPlugin(runner)],
        # The manifest the worker loaded at start is every DurableSession's: the sandbox
        # must not import its module afresh, which would find it empty.
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "scadbuddy_durable.session.tools"
            )
        ),
    )


def segment_runner(cfg: Config, connect: Connect, kek: Kek) -> SegmentRunner:
    """The model's runner: Claude through ScadBuddyRunner, or the tests' scripted one."""
    if cfg.scripted:
        return scripted_runner()
    return ScadBuddyRunner(
        connect,
        kek,
        cwd=cfg.cwd,
        plugin_dir=cfg.skills_dir,
        # agent/src/temporal/turnPackages.ts SHARED_PACKAGES, on the shared payload volume.
        package_root=os.path.join(cfg.payload_dir, "plugins") if cfg.payload_dir else None,
        subagent_url=cfg.subagent_url,
        agents_dir=cfg.agents_dir,
    )


async def connect_client(cfg: Config, connect: Connect, kek: Kek, previous: Kek | None) -> Client:
    keys = PgPayloadKeys(connect, kek, previous=previous)
    return await Client.connect(
        cfg.temporal_address,
        namespace=cfg.namespace,
        data_converter=data_converter(
            keys, external_storage(Path(cfg.payload_dir)) if cfg.payload_dir else None
        ),
    )


async def start(cfg: Config) -> Worker:
    """Loads the manifest and the key, connects, and returns the worker to run."""
    tools.load_manifest(cfg.tools_json)
    kek = load_kek(cfg.kek_file)
    previous = (
        load_kek(cfg.previous_kek_file, "SCADBUDDY_SECRET_KEY_PREVIOUS_FILE")
        if cfg.previous_kek_file
        else None
    )
    connect = connector(cfg.database_url)
    client = await connect_client(cfg, connect, kek, previous)
    return build_worker(client, connect, segment_runner(cfg, connect, kek))
