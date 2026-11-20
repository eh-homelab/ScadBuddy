"""The profile-fetch worker's process (#6625 Task 6, #6780): it imports no database code and
no browser driver, waits for its CRUD areas, runs no browser (each step goes to the browser
service) and answers ``/healthz`` and ``/metrics``."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from temporalio.client import Client

from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.crud.client import CrudClient
from scadbuddy.profile_fetch import __main__ as entry
from scadbuddy.profile_fetch import worker
from scadbuddy.profile_fetch.remote import RemoteSite
from scadbuddy.profile_fetch.settings import ProfileFetchSettings
from scadbuddy.profile_fetch.site import SiteHolder
from scadbuddy.profile_fetch.worker import _healthz, run
from tests.crud.test_client import Source

BACKEND = Path(__file__).resolve().parents[2]
HEADER = {"X-ScadBuddy-Crud": "1"}
TOKEN = "r" * 40


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
    rpc = tmp_path / "rpc"
    rpc.write_text(TOKEN)
    return ProfileFetchSettings(
        api_internal_url="http://api.example",
        temporal_address="127.0.0.1:1",
        service_token_file=proof,
        profile_fetch_browser_url="http://browser.example:8091",
        profile_fetch_browser_token_file=rpc,
    )


def _crud(events: list[str], served: list[list[str]]) -> CrudClient:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/internal/crud/profile-fetch/areas"
        events.append("areas")
        areas = served.pop(0) if len(served) > 1 else served[0]
        return httpx.Response(200, json={"areas": areas}, headers=HEADER)

    return CrudClient(
        "http://api.example",
        "profile-fetch",
        ServiceAuth(Source()),
        transport=httpx.MockTransport(answer),
    )


def test_worker_waits_for_its_areas(tmp_path: Path) -> None:
    """During a roll the API may lack the ``runs`` area: the worker polls only once the
    API serves it."""
    events: list[str] = []

    async def go() -> None:
        stop = asyncio.Event()
        waiting = asyncio.create_task(
            run(
                _settings(tmp_path),
                stop=stop,
                health_port=None,
                crud=_crud(events, [[]]),
                areas_poll=0.05,
            )
        )
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(waiting, 5)

    asyncio.run(go())
    assert events and set(events) == {"areas"}


class ReachedTemporalError(Exception):
    """The worker went on to connect to Temporal."""


def test_the_worker_opens_no_browser_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker signs in nowhere at start: once its areas are served it connects to
    Temporal, and its site is the browser service's (`RemoteSite`), opened only by an
    export (#6780)."""
    events: list[str] = []
    opened: list[object] = []

    async def connect(*args: object, **kwargs: object) -> None:
        events.append("connect")
        raise ReachedTemporalError

    real = SiteHolder

    def holder(open_site: object) -> object:
        opened.append(open_site)
        return real(open_site)  # type: ignore[arg-type]

    monkeypatch.setattr(Client, "connect", connect)
    monkeypatch.setattr(worker, "SiteHolder", holder)

    async def go() -> None:
        await run(
            _settings(tmp_path),
            health_port=None,
            crud=_crud(events, [["runs"]]),
            areas_poll=0.05,
        )

    with pytest.raises(ReachedTemporalError):
        asyncio.run(go())
    assert events == ["areas", "connect"]
    [open_site] = opened
    site = open_site()  # type: ignore[operator]
    assert isinstance(site, RemoteSite)
    assert site._base == "http://browser.example:8091"
    assert site._token == TOKEN


def test_the_worker_imports_no_browser_service_code() -> None:
    """The worker's start does not import the browser service (`browser.py`), which holds
    the 3DFP login."""
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import scadbuddy.profile_fetch.__main__, sys;"
            " print('scadbuddy.profile_fetch.browser' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=BACKEND,
    )
    assert out.stdout.strip() == "False"


def test_main_runs_with_the_worker_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rpc = tmp_path / "rpc"
    rpc.write_text(TOKEN)
    proof = tmp_path / "token"
    proof.write_text("t")
    for name, value in {
        "SCADBUDDY_API_INTERNAL_URL": "http://api.example",
        "SCADBUDDY_TEMPORAL_ADDRESS": "127.0.0.1:1",
        "SCADBUDDY_SERVICE_TOKEN_FILE": str(proof),
        "SCADBUDDY_PROFILE_FETCH_BROWSER_URL": "http://browser.example:8091",
        "SCADBUDDY_PROFILE_FETCH_BROWSER_TOKEN_FILE": str(rpc),
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("SCADBUDDY_SERVICE_BOOTSTRAP_FILE", "SCADBUDDY_TDFP_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SCADBUDDY_TDFP_PASSWORD", raising=False)
    seen: list[ProfileFetchSettings] = []

    async def running(settings: ProfileFetchSettings, **kwargs: object) -> None:
        seen.append(settings)

    monkeypatch.setattr(entry, "forbid_dumping", lambda: None)
    monkeypatch.setattr(entry, "configure_logging", lambda *args: None)
    monkeypatch.setattr(entry, "install_stop", lambda stop: None)
    monkeypatch.setattr(entry, "run", running)
    assert entry.main([]) == 0
    [settings] = seen
    assert settings.browser_token == TOKEN


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


def test_a_refused_start_logs_no_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The worker refuses the 3DFP login in its environment, naming it, never its value."""
    monkeypatch.setenv("SCADBUDDY_TDFP_PASSWORD", "pw-6780-not-real")
    monkeypatch.setattr(entry, "configure_logging", lambda *args: None)
    caplog.set_level(logging.ERROR)
    assert entry.main([]) == 2
    assert "SCADBUDDY_TDFP_PASSWORD" in caplog.text
    assert "pw-6780-not-real" not in caplog.text
