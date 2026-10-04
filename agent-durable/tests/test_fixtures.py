import psycopg
import pytest
from temporalio.client import Client


@pytest.mark.requires_postgres
def test_pg_conninfo_has_the_agent_schema(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT to_regclass('ai_sessions')").fetchone()
    assert row is not None and row[0] is not None


@pytest.mark.requires_temporal
async def test_temporal_env_connects(temporal_env: Client) -> None:
    assert temporal_env.namespace == "default"
    await temporal_env.service_client.check_health()
