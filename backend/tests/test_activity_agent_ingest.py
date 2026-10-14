"""The agent's operations ingested into activity (spec 2026-10-09 §5)."""

import json
from dataclasses import dataclass
from typing import Any

import pytest

from scadbuddy.activity.index import ActivityIndex
from tests.conftest import PgPool

#: `agent/src/db/migrations/20261004T1345Z_operations.sql` plus the columns the agent's
#: attribution migration adds (principal, session, via_tool).
AGENT_TABLES = """
CREATE TABLE ai_operations (
  id              text PRIMARY KEY,
  kind            text NOT NULL,
  subject         text NOT NULL,
  operation_key   text NOT NULL,
  status          text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
  request         jsonb NOT NULL,
  result          jsonb,
  error           jsonb,
  workflow_id     text NOT NULL,
  workflow_run_id text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  principal       text,
  session         text,
  via_tool        text,
  CHECK ((status = 'running') = (finished_at IS NULL))
)
"""


@dataclass
class AgentTables:
    pool: PgPool

    def insert_operation(
        self,
        *,
        id: str,
        kind: str,
        subject: str,
        status: str,
        principal: str | None,
        via_tool: str | None,
        request: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ai_operations (id, kind, subject, operation_key, status, request,"
                " result, workflow_id, workflow_run_id, principal, via_tool, finished_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " CASE WHEN %s = 'running' THEN NULL ELSE now() END)",
                (
                    id,
                    kind,
                    subject,
                    id,
                    status,
                    json.dumps(request),
                    None if result is None else json.dumps(result),
                    f"agent-op-{id}",
                    "run1",
                    principal,
                    via_tool,
                    status,
                ),
            )


@pytest.fixture
def agent_tables(pg_pool: PgPool) -> AgentTables:
    with pg_pool.connection() as conn:
        conn.execute(AGENT_TABLES)
    return AgentTables(pg_pool)


@pytest.fixture
def index(pg_pool: PgPool) -> ActivityIndex:
    return ActivityIndex(pg_pool, events=None)


@pytest.mark.requires_postgres
async def test_ingest_agent_operation(index: ActivityIndex, agent_tables: AgentTables) -> None:
    agent_tables.insert_operation(
        id="a1",
        kind="plugin_package_install",
        subject="hindsight",
        status="succeeded",
        principal="browser",
        via_tool=None,
        request={"source": "x"},
        result={"ok": True},
    )
    await index.ingest_agent_operation("a1")
    got = await index.get("agent_operation:a1")
    assert got is not None
    assert (got.kind, got.status, got.principal) == (
        "agent.plugin_package_install",
        "succeeded",
        "browser",
    )
    assert got.detail["result"] == {"ok": True}
    assert got.finished_at is not None


@pytest.mark.requires_postgres
async def test_ingest_without_agent_tables_is_a_no_op(index: ActivityIndex) -> None:
    await index.ingest_agent_operation("missing")
    assert await index.get("agent_operation:missing") is None


@pytest.mark.requires_postgres
async def test_ingest_of_an_unknown_row_is_a_no_op(
    index: ActivityIndex, agent_tables: AgentTables
) -> None:
    await index.ingest_agent_operation("missing")
    assert await index.get("agent_operation:missing") is None


@pytest.mark.requires_postgres
async def test_an_unknown_agent_status_leaves_no_row_and_raises_nothing(
    pg_pool: PgPool, index: ActivityIndex, agent_tables: AgentTables
) -> None:
    with pg_pool.connection() as conn:
        conn.execute("ALTER TABLE ai_operations DROP CONSTRAINT ai_operations_status_check")
    agent_tables.insert_operation(
        id="a2",
        kind="x",
        subject="",
        status="exploded",
        principal=None,
        via_tool=None,
        request={},
    )
    await index.ingest_agent_operation("a2")
    assert await index.get("agent_operation:a2") is None


@pytest.mark.requires_postgres
async def test_a_second_ingest_follows_the_status(
    index: ActivityIndex, agent_tables: AgentTables, pg_pool: PgPool
) -> None:
    agent_tables.insert_operation(
        id="a3",
        kind="x",
        subject="s",
        status="running",
        principal="token:t1",
        via_tool="tool",
        request={},
    )
    await index.ingest_agent_operation("a3")
    first = await index.get("agent_operation:a3")
    assert first is not None and first.status == "running" and first.principal == "token:t1"
    with pg_pool.connection() as conn:
        conn.execute("UPDATE ai_operations SET status='failed', finished_at=now() WHERE id='a3'")
    await index.ingest_agent_operation("a3")
    second = await index.get("agent_operation:a3")
    assert second is not None and second.status == "failed" and second.via_tool == "tool"
