import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import psycopg
import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment

from scadbuddy_durable.worker import connector

VECTORS = (
    Path(__file__).resolve().parents[2] / "agent" / "test" / "fixtures" / "secret-vectors.json"
)


@pytest.fixture(scope="session")
def vectors() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(VECTORS.read_text())
    return data


MIGRATIONS = Path(__file__).resolve().parents[2] / "agent" / "src" / "db" / "migrations"


@pytest.fixture
async def agent_db() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    """A throwaway schema holding the agent's tables, from its own migration files."""
    url = os.environ.get("SCADBUDDY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("SCADBUDDY_TEST_DATABASE_URL is not set")
    schema = f"durable_{uuid.uuid4().hex[:12]}"
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        for sql in sorted(MIGRATIONS.glob("*.sql")):
            await conn.execute(sql.read_text())
        yield conn
    finally:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


Conn = psycopg.AsyncConnection[Any]


@pytest.fixture
async def connect(agent_db: Conn) -> Callable[[], AbstractAsyncContextManager[Conn]]:
    """More connections to the fixture's schema."""
    cur = await agent_db.execute("SHOW search_path")
    row = await cur.fetchone()
    assert row is not None
    return connector(os.environ["SCADBUDDY_TEST_DATABASE_URL"], row[0])


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    """A Temporal dev server for the session (``requires_temporal``), from the CLI the
    agent's tests use (SCADBUDDY_TEST_TEMPORAL_DEV_SERVER)."""
    path = os.environ.get("SCADBUDDY_TEST_TEMPORAL_DEV_SERVER")
    if not path:
        pytest.skip("SCADBUDDY_TEST_TEMPORAL_DEV_SERVER is not set")
    env = await WorkflowEnvironment.start_local(dev_server_existing_path=path)
    try:
        yield env
    finally:
        await env.shutdown()
