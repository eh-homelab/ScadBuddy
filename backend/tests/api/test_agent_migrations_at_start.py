"""The API applies the agent's migrations at start, after its own (#5299 D6)."""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.render.agent_migrations import (
    AGENT_MIGRATIONS,
    agent_migration_checksum,
)

pytestmark = [pytest.mark.requires_postgres, pytest.mark.agent_migrations]


def test_the_start_applies_every_agent_migration(client: TestClient, pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        rows = conn.execute("SELECT id, checksum FROM ai_migrations ORDER BY id").fetchall()
        # After the backend's: its ledger is complete too.
        assert conn.execute("SELECT count(*) FROM scadbuddy_migrations").fetchone() != (0,)
    assert rows == [(m.id, agent_migration_checksum(m)) for m in AGENT_MIGRATIONS]
