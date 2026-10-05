from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from temporalio.client import Client

# Before any scadbuddy_durable import: the workflow tests see the sample tools.
os.environ.setdefault("SCADBUDDY_AGENT_TOOLS_MANIFEST", str(Path(__file__).parent / "fixtures/tools.json"))

MIGRATIONS = Path(__file__).parents[2] / "agent" / "src" / "db" / "migrations"


@pytest.fixture
def pg_conninfo() -> Iterator[str]:
    """A throwaway schema with every agent migration applied in name order."""
    base = os.environ.get("SCADBUDDY_TEST_DATABASE_URL")
    if not base:
        pytest.skip("SCADBUDDY_TEST_DATABASE_URL is not set")
    schema = f"t_{uuid.uuid4().hex}"
    ident = sql.Identifier(schema)
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(ident))
        try:
            with psycopg.connect(base, autocommit=True, options=f"-csearch_path={schema}") as conn:
                for path in sorted(MIGRATIONS.glob("*.sql")):
                    conn.execute(path.read_text())
            sep = "&" if "?" in base else "?"
            yield f"{base}{sep}options=-csearch_path%3D{schema}"
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(ident))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def _temporal_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    cli = os.environ.get("SCADBUDDY_TEST_TEMPORAL_DEV_SERVER") or shutil.which("temporal")
    if not cli:
        pytest.skip("no Temporal CLI (SCADBUDDY_TEST_TEMPORAL_DEV_SERVER or temporal on PATH)")
    port = _free_port()
    db = tmp_path_factory.mktemp("temporal") / "t.db"
    proc = subprocess.Popen(
        [
            cli,
            "server",
            "start-dev",
            "--headless",
            "--ip",
            "127.0.0.1",
            "--port",
            str(port),
            "--db-filename",
            str(db),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    address = f"127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("temporal dev server did not start") from None
                time.sleep(0.2)
        yield address
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest_asyncio.fixture
async def temporal_env(_temporal_server: str) -> AsyncIterator[Client]:
    """A client on namespace `default` of the session's dev server."""
    deadline = time.monotonic() + 60
    while True:
        try:
            client = await Client.connect(_temporal_server, namespace="default")
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)  # noqa: ASYNC251
    yield client
