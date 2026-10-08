import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

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
