"""The profile-fetch worker's process (#6625 Task 6): it imports no database code, waits
for its CRUD areas, signs in once at start (a refused login ends the process with 1) and
answers ``/healthz`` and ``/metrics``."""

from __future__ import annotations

import asyncio
import logging
import os
import resource
import subprocess
import sys
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx
import pytest
from playwright.async_api import Error as PlaywrightError
from pydantic import SecretStr
from temporalio.client import Client

from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.crud.client import CrudClient
from scadbuddy.profile_fetch import __main__ as entry
from scadbuddy.profile_fetch import worker
from scadbuddy.profile_fetch.settings import ProfileFetchSettings
from scadbuddy.profile_fetch.site import ExportedFile, Kind, SiteError
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


def test_main_drops_the_login_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Once the settings hold the 3DFP login, ``os.environ`` no longer does: no process
    the worker starts (Playwright's driver, the browser) inherits it (Task 6 review, I1)."""
    proof = tmp_path / "token"
    proof.write_text("t")
    for name, value in {
        "SCADBUDDY_API_INTERNAL_URL": "http://api",
        "SCADBUDDY_TEMPORAL_ADDRESS": "127.0.0.1:1",
        "SCADBUDDY_SERVICE_TOKEN_FILE": str(proof),
        "SCADBUDDY_TDFP_EMAIL": EMAIL,
        "SCADBUDDY_TDFP_PASSWORD": PASSWORD,
        "SCADBUDDY_PROFILE_FETCH_WORK_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SCADBUDDY_SERVICE_BOOTSTRAP_FILE", raising=False)
    seen: dict[str, object] = {}

    async def running(settings: ProfileFetchSettings, **kwargs: object) -> None:
        seen["environ"] = dict(os.environ)
        seen["login"] = (
            settings.tdfp_email.get_secret_value(),
            settings.tdfp_password.get_secret_value(),
        )

    monkeypatch.setattr(entry, "check_environment", lambda environ: None)
    monkeypatch.setattr(entry, "forbid_dumping", lambda: None)
    monkeypatch.setattr(entry, "configure_logging", lambda *args: None)
    monkeypatch.setattr(entry, "install_stop", lambda stop: None)
    monkeypatch.setattr(entry, "run", running)
    assert entry.main([]) == 0
    assert seen["login"] == (EMAIL, PASSWORD)
    environ = seen["environ"]
    assert isinstance(environ, dict)
    assert "SCADBUDDY_TDFP_EMAIL" not in environ
    assert "SCADBUDDY_TDFP_PASSWORD" not in environ
    assert "SCADBUDDY_TDFP_EMAIL" not in os.environ
    assert "SCADBUDDY_TDFP_PASSWORD" not in os.environ


class ReachedTemporalError(Exception):
    """The worker went on to connect to Temporal."""


@pytest.mark.parametrize("kind", ["bot_check", "timeout", "browser"])
def test_a_passing_failure_at_start_is_logged_and_the_worker_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    kind: Kind,
) -> None:
    """Only a refused login stops the start. A bot check, a timeout or a browser failure
    is logged (without the login), the browser is closed, and the worker goes on to
    Temporal: the first fetch signs in again under its retries (Task 6 concern 5)."""
    events: list[str] = []

    async def connect(*args: object, **kwargs: object) -> None:
        events.append("connect")
        raise ReachedTemporalError

    monkeypatch.setattr(Client, "connect", connect)
    caplog.set_level(logging.DEBUG)

    async def go() -> None:
        await run(
            _settings(tmp_path),
            health_port=None,
            crud=_crud(events, [["runs"]]),
            open_site=lambda: StartSite(events, SiteError(kind)),
            areas_poll=0.05,
        )

    with pytest.raises(ReachedTemporalError):
        asyncio.run(go())
    assert events == ["areas", "open", "login", "close", "connect"]
    assert kind in caplog.text
    assert EMAIL not in caplog.text and PASSWORD not in caplog.text


def test_the_start_path_forbids_core_dumps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` calls ``forbid_core_dumps()`` before it opens the site (build Decision 75):
    a node's ``core_pattern`` must not keep a crashed renderer's memory, where the page
    and the login lived. The spy stops the start right after the call."""
    events: list[str] = []

    def spy() -> None:
        events.append("forbid")
        raise ReachedTemporalError

    monkeypatch.setattr(worker, "forbid_core_dumps", spy)

    async def go() -> None:
        # The timeout turns a missing call into a failure: without it the start runs on.
        await asyncio.wait_for(
            run(
                _settings(tmp_path),
                health_port=None,
                crud=_crud(events, [["runs"]]),
                open_site=lambda: StartSite(events, None),
                areas_poll=0.05,
            ),
            timeout=10,
        )

    with pytest.raises(ReachedTemporalError):
        asyncio.run(go())
    assert events == ["forbid"]


def test_forbid_core_dumps_sets_both_limits_to_zero() -> None:
    """The call itself runs in a fresh interpreter, so the pytest process's own limits
    are never changed."""
    _, hard = resource.getrlimit(resource.RLIMIT_CORE)
    if hard == 0:
        pytest.skip("the hard RLIMIT_CORE is already 0, so the check cannot fail")
    code = (
        "import resource\n"
        "from scadbuddy.profile_fetch.worker import forbid_core_dumps\n"
        "hard = resource.getrlimit(resource.RLIMIT_CORE)[1]\n"
        "resource.setrlimit(resource.RLIMIT_CORE, (hard, hard))\n"
        "forbid_core_dumps()\n"
        "print(resource.getrlimit(resource.RLIMIT_CORE))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        timeout=60,
        check=True,
        capture_output=True,
        text=True,
        cwd=BACKEND,
    )
    assert out.stdout.strip() == "(0, 0)"


def test_a_browser_that_does_not_launch_stops_the_worker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Chromium that cannot launch (its sandbox cannot start, Decision 6) refuses the
    start with a fixed text and no chain, so no traceback that could print the
    environment rides along (Task 6 review, M3)."""
    events: list[str] = []

    class Unlaunchable(StartSite):
        async def __aenter__(self) -> Self:
            events.append("launch")
            raise PlaywrightError(f"Browser closed. env: SCADBUDDY_TDFP_PASSWORD={PASSWORD}")

    async def go() -> None:
        await run(
            _settings(tmp_path),
            health_port=None,
            crud=_crud(events, [["runs"]]),
            open_site=lambda: Unlaunchable(events, None),
            areas_poll=0.05,
        )

    caplog.set_level(logging.DEBUG)
    with pytest.raises(StartRefusedError) as caught:
        asyncio.run(go())
    assert str(caught.value) == "the browser did not start"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert events == ["areas", "launch"]
    assert PASSWORD not in caplog.text


def test_main_exits_1_when_the_browser_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path)

    async def refused(*args: object, **kwargs: object) -> None:
        raise StartRefusedError("the browser did not start")

    monkeypatch.setattr(entry, "check_environment", lambda environ: None)
    monkeypatch.setattr(entry, "forbid_dumping", lambda: None)
    monkeypatch.setattr(entry, "ProfileFetchSettings", lambda: settings)
    monkeypatch.setattr(entry, "configure_logging", lambda *args: None)
    monkeypatch.setattr(entry, "run", refused)
    caplog.set_level(logging.ERROR)
    assert entry.main([]) == 1
    assert "the browser did not start" in caplog.text
    assert "Traceback" not in caplog.text
