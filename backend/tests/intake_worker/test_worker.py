"""The intake worker's process (#6133, spec 2026-11-13 §7): it refuses a database secret or
the KEK, needs one proof, and imports no database code."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from scadbuddy.intake_worker.__main__ import main
from scadbuddy.intake_worker.settings import FORBIDDEN_ENV, IntakeSettings
from scadbuddy.intake_worker.worker import _healthz

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture
def worker_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in FORBIDDEN_ENV:
        monkeypatch.delenv(name, raising=False)
    proof = tmp_path / "token"
    proof.write_text("t")
    monkeypatch.setenv("SCADBUDDY_API_INTERNAL_URL", "http://scadbuddy:8080")
    monkeypatch.setenv("SCADBUDDY_TEMPORAL_ADDRESS", "temporal:7233")
    monkeypatch.setenv("SCADBUDDY_SERVICE_TOKEN_FILE", str(proof))
    monkeypatch.delenv("SCADBUDDY_SERVICE_BOOTSTRAP_FILE", raising=False)
    monkeypatch.setenv("SCADBUDDY_INTAKE_SANDBOX", "true")
    monkeypatch.setenv("SCADBUDDY_INTAKE_SANDBOX_UID", str(os.geteuid() + 1))
    return proof


@pytest.mark.parametrize("name", FORBIDDEN_ENV)
def test_a_database_secret_or_the_key_refuses_the_start(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "x")
    assert main([]) == 2


def test_the_settings_read_the_queue_and_need_one_proof(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = IntakeSettings()  # type: ignore[call-arg]
    assert settings.temporal_task_queue_customer_files == "customer-files"
    monkeypatch.setenv("SCADBUDDY_SERVICE_BOOTSTRAP_FILE", str(worker_env))
    with pytest.raises(ValidationError, match="exactly one"):
        IntakeSettings()  # type: ignore[call-arg]


@pytest.mark.parametrize("value", [None, "false", "", "yes"])
def test_the_settings_need_the_sandbox_named(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """No default: a missing or unknown value refuses the start, so production never
    falls back to parsing in the worker's container."""
    if value is None:
        monkeypatch.delenv("SCADBUDDY_INTAKE_SANDBOX")
    else:
        monkeypatch.setenv("SCADBUDDY_INTAKE_SANDBOX", value)
    with pytest.raises(ValidationError, match="intake_sandbox"):
        IntakeSettings()  # type: ignore[call-arg]
    monkeypatch.setenv("SCADBUDDY_INTAKE_SANDBOX", "unsafe-local")
    assert IntakeSettings().intake_sandbox == "unsafe-local"  # type: ignore[call-arg]


@pytest.mark.parametrize("uid", [None, "0", "same"])
def test_the_sandbox_needs_a_uid_of_its_own(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch, uid: str | None
) -> None:
    if uid is None:
        monkeypatch.delenv("SCADBUDDY_INTAKE_SANDBOX_UID")
    else:
        monkeypatch.setenv(
            "SCADBUDDY_INTAKE_SANDBOX_UID", str(os.geteuid()) if uid == "same" else uid
        )
    with pytest.raises(ValidationError, match="SANDBOX_UID"):
        IntakeSettings()  # type: ignore[call-arg]


def test_the_worker_imports_nothing_that_touches_the_database() -> None:
    """The worker process; a file is parsed only in its child (`child.py`)."""
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import scadbuddy.intake_worker.__main__, sys;"
            " forbidden = {'psycopg', 'psycopg_pool', 'scadbuddy.orders', 'scadbuddy.render',"
            " 'scadbuddy.store', 'scadbuddy.library', 'scadbuddy.main'};"
            " print(sorted({'.'.join(m.split('.')[:2]) for m in sys.modules} & forbidden))",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=BACKEND,
    )
    assert out.stdout.strip() == "[]"


def test_healthz_answers_200() -> None:
    assert _get(b"/healthz").startswith(b"HTTP/1.1 200 OK")


def test_metrics_count_the_checks_the_sandbox_never_answered() -> None:
    answer = _get(b"/metrics")
    assert answer.startswith(b"HTTP/1.1 200 OK")
    assert b"scadbuddy_intake_sandbox_unanswered_total" in answer


def _get(path: bytes) -> bytes:
    async def probe() -> bytes:
        server = await asyncio.start_server(_healthz, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET " + path + b" HTTP/1.1\r\n\r\n")
            await writer.drain()
            answer = await reader.read()
            writer.close()
            return answer
        finally:
            server.close()

    return asyncio.run(probe())
