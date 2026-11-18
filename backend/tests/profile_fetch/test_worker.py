"""The profile-fetch worker's process (#6625 Task 6): it imports no database code, waits
for its CRUD areas, signs in once at start (a refused login ends the process with 1) and
answers ``/healthz`` and ``/metrics``."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx
import pytest
from pydantic import SecretStr

from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.crud.client import CrudClient
from scadbuddy.profile_fetch import __main__ as entry
from scadbuddy.profile_fetch.settings import ProfileFetchSettings
from scadbuddy.profile_fetch.site import ExportedFile, SiteError
from scadbuddy.profile_fetch.worker import StartRefusedError, _healthz, run
from tests.crud.test_client import Source

BACKEND = Path(__file__).resolve().parents[2]
HEADER = {"X-ScadBuddy-Crud": "1"}
EMAIL = "owner-6625@example.invalid"
PASSWORD = "pw-6625-not-a-real-password"


def test_worker_imports_no_database_code() -> None:
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import scadbuddy.profile_fetch.__main__, scadbuddy.profile_fetch.worker, sys;"
            " forbidden = {'psycopg', 'psycopg_pool', 'scadbuddy.orders', 'scadbuddy.render',"
            " 'scadbuddy.store', 'scadbuddy.library', 'scadbuddy.main', 'scadbuddy.filament',"
            " 'scadbuddy.bambuddy'};"
            " print(sorted({'.'.join(m.split('.')[:2]) for m in sys.modules} & forbidden))",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=BACKEND,
    )
    assert out.stdout.strip() == "[]"


def _settings(tmp_path: Path) -> ProfileFetchSettings:
    proof = tmp_path / "token"
    proof.write_text("t")
    return ProfileFetchSettings(
        api_internal_url="http://api",
        temporal_address="127.0.0.1:1",
        service_token_file=proof,
        tdfp_email=SecretStr(EMAIL),
        tdfp_password=SecretStr(PASSWORD),
        profile_fetch_work_dir=tmp_path,
    )


class StartSite:
    """A site whose login records when it ran, and fails as ``login_error`` says."""

    def __init__(self, events: list[str], login_error: SiteError | None) -> None:
        self.events = events
        self.login_error = login_error

    async def __aenter__(self) -> Self:
        self.events.append("open")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.events.append("close")

    async def login(self) -> None:
        self.events.append("login")
        if self.login_error is not None:
            raise self.login_error

    async def ensure_in_my_spools(self, tdfp_id: int) -> None:
        raise AssertionError("not at start")

    async def export(self, tdfp_id: int, printer_model: str, nozzle: str) -> ExportedFile:
        raise AssertionError("not at start")


def _crud(events: list[str], served: list[list[str]]) -> CrudClient:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/internal/crud/profile-fetch/areas"
        events.append("areas")
        areas = served.pop(0) if len(served) > 1 else served[0]
        return httpx.Response(200, json={"areas": areas}, headers=HEADER)

    return CrudClient(
        "http://api", "profile-fetch", ServiceAuth(Source()), transport=httpx.MockTransport(answer)
    )


def test_worker_waits_for_its_areas(tmp_path: Path) -> None:
    """During a roll the API may lack the ``runs`` area: the worker signs in and polls
    only once the API serves it; stopped before then, it never opens the browser."""
    events: list[str] = []

    async def go() -> None:
        stop = asyncio.Event()
        crud = _crud(events, [[]])
        waiting = asyncio.create_task(
            run(
                _settings(tmp_path),
                stop=stop,
                health_port=None,
                crud=crud,
                open_site=lambda: StartSite(events, None),
                areas_poll=0.05,
            )
        )
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(waiting, 5)

    asyncio.run(go())
    assert events and set(events) == {"areas"}


def test_a_refused_login_at_start_stops_the_worker(tmp_path: Path) -> None:
    """The login is tried once the areas are served; refused, the start fails with a
    text that names neither the email nor the password, and the browser is closed."""
    events: list[str] = []

    async def go() -> None:
        await run(
            _settings(tmp_path),
            health_port=None,
            crud=_crud(events, [[], ["runs"]]),
            open_site=lambda: StartSite(events, SiteError("login_failed")),
            areas_poll=0.05,
        )

    with pytest.raises(StartRefusedError) as caught:
        asyncio.run(go())
    assert str(caught.value) == "3DFP login failed"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert events == ["areas", "areas", "open", "login", "close"]


def test_main_exits_1_when_the_login_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path)

    async def refused(*args: object, **kwargs: object) -> None:
        raise StartRefusedError("3DFP login failed")

    monkeypatch.setattr(entry, "check_environment", lambda environ: None)
    monkeypatch.setattr(entry, "forbid_dumping", lambda: None)
    monkeypatch.setattr(entry, "ProfileFetchSettings", lambda: settings)
    monkeypatch.setattr(entry, "configure_logging", lambda *args: None)
    monkeypatch.setattr(entry, "run", refused)
    caplog.set_level(logging.ERROR)
    assert entry.main([]) == 1
    assert "3DFP login failed" in caplog.text
    assert EMAIL not in caplog.text and PASSWORD not in caplog.text


def test_healthz_answers_200() -> None:
    assert _get(b"/healthz").startswith(b"HTTP/1.1 200 OK")


def test_metrics_count_the_exports() -> None:
    from scadbuddy.profile_fetch.activities import EXPORTS

    EXPORTS.labels(outcome="exported")
    answer = _get(b"/metrics")
    assert answer.startswith(b"HTTP/1.1 200 OK")
    assert b'scadbuddy_profile_fetch_exports_total{outcome="exported"}' in answer


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
