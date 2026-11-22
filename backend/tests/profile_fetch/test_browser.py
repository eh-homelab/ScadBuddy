"""The profile-fetch browser service's process (#6780): it imports no Temporal, CRUD or
database code, signs in at start (a login refused on every try or a browser that does not
launch ends the process with 1), forbids core dumps before any browser launches, and drops
the 3DFP login from its environment once read."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import resource
import subprocess
import sys
from pathlib import Path
from typing import Self

import httpx
import pytest
from playwright.async_api import Error as PlaywrightError
from pydantic import SecretStr

from scadbuddy.profile_fetch import browser
from scadbuddy.profile_fetch.browser import StartRefusedError, serve, sign_in_at_start
from scadbuddy.profile_fetch.settings import BrowserSettings
from scadbuddy.profile_fetch.steps import Kind, SiteError, SiteHolder
from tests.profile_fetch.test_activities import FakeSite

BACKEND = Path(__file__).resolve().parents[2]
EMAIL = "owner-6625@example.invalid"
PASSWORD = "pw-6625-not-a-real-password"
TOKEN = "b" * 40


def test_the_browser_service_imports_no_temporal_crud_or_database_code() -> None:
    """The pod that runs untrusted web content loads no Temporal client, no CRUD client
    and no database code: it has no route to any of them (#6780)."""
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import scadbuddy.profile_fetch.browser, sys;"
            " forbidden = {'psycopg', 'psycopg_pool', 'temporalio', 'scadbuddy.crud',"
            " 'scadbuddy.auth', 'scadbuddy.main', 'scadbuddy.filament', 'scadbuddy.bambuddy'};"
            " print(sorted({'.'.join(m.split('.')[:2]) if m.startswith('scadbuddy.')"
            " else m.split('.')[0] for m in sys.modules} & forbidden))",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=BACKEND,
    )
    assert out.stdout.strip() == "[]"


def _settings(tmp_path: Path) -> BrowserSettings:
    rpc = tmp_path / "rpc"
    rpc.write_text(TOKEN)
    return BrowserSettings(
        tdfp_email=SecretStr(EMAIL),
        tdfp_password=SecretStr(PASSWORD),
        profile_fetch_work_dir=tmp_path,
        profile_fetch_browser_token_file=rpc,
    )


class Sleeps:
    """`sign_in_at_start`'s ``sleep``: records each pause and returns at once."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def test_a_refused_login_at_start_refuses_the_start() -> None:
    """A login the site refuses on every try refuses the start, after three tries 10 s and
    30 s apart (#6941)."""
    site = FakeSite(login_error=SiteError("login_failed"))
    sleeps = Sleeps()
    with pytest.raises(StartRefusedError) as caught:
        await sign_in_at_start(SiteHolder(lambda: site), sleep=sleeps)
    assert str(caught.value) == "3DFP login failed"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert (site.opened, site.logins, site.closed) == (3, 3, 3)
    assert sleeps.delays == [10.0, 30.0]


async def test_a_login_refused_then_accepted_at_start_signs_in() -> None:
    """On prod the site refused three starts' logins and accepted the fourth's, with the
    same login (#6941): a refused login is tried again before it refuses the start."""

    class RefusedTwice(FakeSite):
        async def login(self) -> None:
            self.login_error = SiteError("login_failed") if self.logins < 2 else None
            await super().login()

    site = RefusedTwice()
    sleeps = Sleeps()
    sites = SiteHolder(lambda: site)
    await sign_in_at_start(sites, sleep=sleeps)
    assert (site.opened, site.logins, site.closed) == (3, 3, 2)
    assert sleeps.delays == [10.0, 30.0]
    assert await sites.get() is site
    assert site.logins == 3


@pytest.mark.parametrize("kind", ["bot_check", "timeout", "browser"])
async def test_a_passing_failure_at_start_is_logged(
    caplog: pytest.LogCaptureFixture, kind: Kind
) -> None:
    """Only a refused login stops the start. A bot check, a timeout or a browser failure
    is logged (without the login), the browser is closed, and the first call signs in
    again."""
    caplog.set_level(logging.DEBUG)
    site = FakeSite(login_error=SiteError(kind))
    sleeps = Sleeps()
    await sign_in_at_start(SiteHolder(lambda: site), sleep=sleeps)
    assert (site.logins, site.closed) == (1, 1)
    assert sleeps.delays == []
    assert kind in caplog.text
    assert EMAIL not in caplog.text and PASSWORD not in caplog.text


async def test_a_browser_that_does_not_launch_refuses_the_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Chromium that cannot launch (its sandbox cannot start, Decision 6) refuses the
    start with a fixed text and no chain, so no traceback that could print the
    environment rides along (Task 6 review, M3)."""

    class Unlaunchable(FakeSite):
        async def __aenter__(self) -> Self:
            raise PlaywrightError(f"Browser closed. env: SCADBUDDY_TDFP_PASSWORD={PASSWORD}")

    caplog.set_level(logging.DEBUG)
    with pytest.raises(StartRefusedError) as caught:
        await sign_in_at_start(SiteHolder(Unlaunchable))
    assert str(caught.value) == "the browser did not start"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert PASSWORD not in caplog.text


async def test_serve_signs_in_once_and_answers_healthz(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    site = FakeSite()
    stop = asyncio.Event()
    serving = asyncio.create_task(
        serve(_settings(tmp_path), stop=stop, open_site=lambda: site, host="127.0.0.1", port=0)
    )
    for _ in range(100):
        if site.logins:
            break
        await asyncio.sleep(0.02)
    assert (site.opened, site.logins) == (1, 1)
    # The port bound, not the setting's (claude-review on #6808): ``port=0`` picks one.
    [bound] = re.findall(r"serving on (\d+)", caplog.text)
    assert int(bound) not in (0, _settings(tmp_path).profile_fetch_browser_port)
    async with httpx.AsyncClient() as client:
        assert (await client.get(f"http://127.0.0.1:{bound}/healthz")).status_code == 200
    stop.set()
    await asyncio.wait_for(serving, 5)
    assert site.closed == 1


async def test_serve_forbids_core_dumps_before_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve`` calls ``forbid_core_dumps()`` before it opens the site (build Decision 75):
    a node's ``core_pattern`` must not keep a crashed renderer's memory."""
    events: list[str] = []

    class StopError(Exception):
        pass

    def spy() -> None:
        events.append("forbid")
        raise StopError

    monkeypatch.setattr(browser, "forbid_core_dumps", spy)
    site = FakeSite()
    with pytest.raises(StopError):
        await asyncio.wait_for(
            serve(
                _settings(tmp_path),
                stop=asyncio.Event(),
                open_site=lambda: site,
                host="127.0.0.1",
                port=0,
            ),
            timeout=10,
        )
    assert events == ["forbid"]
    assert site.opened == 0


def test_forbid_core_dumps_sets_both_limits_to_zero() -> None:
    """The call itself runs in a fresh interpreter, so the pytest process's own limits
    are never changed."""
    _, hard = resource.getrlimit(resource.RLIMIT_CORE)
    if hard == 0:
        pytest.skip("the hard RLIMIT_CORE is already 0, so the check cannot fail")
    code = (
        "import resource\n"
        "from scadbuddy.profile_fetch.browser import forbid_core_dumps\n"
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


def _patch_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rpc = tmp_path / "rpc"
    rpc.write_text(TOKEN)
    for name, value in {
        "SCADBUDDY_TDFP_EMAIL": EMAIL,
        "SCADBUDDY_TDFP_PASSWORD": PASSWORD,
        "SCADBUDDY_PROFILE_FETCH_WORK_DIR": str(tmp_path),
        "SCADBUDDY_PROFILE_FETCH_BROWSER_TOKEN_FILE": str(rpc),
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "SCADBUDDY_SERVICE_TOKEN_FILE",
        "SCADBUDDY_SERVICE_BOOTSTRAP_FILE",
        "SCADBUDDY_TEMPORAL_ADDRESS",
        "SCADBUDDY_API_INTERNAL_URL",
        "DEBUG",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(browser, "SECRETS_DIR", tmp_path / "no-secrets")
    monkeypatch.setattr(browser, "forbid_dumping", lambda: None)
    monkeypatch.setattr(browser, "configure_logging", lambda *args: None)


def test_main_drops_the_login_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Once the settings hold the 3DFP login, ``os.environ`` no longer does: no process
    the service starts (Playwright's driver, the browser) inherits it (Task 6 review, I1)."""
    _patch_main(monkeypatch, tmp_path)
    seen: dict[str, object] = {}

    async def running(settings: BrowserSettings) -> None:
        seen["environ"] = dict(os.environ)
        seen["login"] = (
            settings.tdfp_email.get_secret_value(),
            settings.tdfp_password.get_secret_value(),
        )

    monkeypatch.setattr(browser, "_main", running)
    assert browser.main([]) == 0
    assert seen["login"] == (EMAIL, PASSWORD)
    environ = seen["environ"]
    assert isinstance(environ, dict)
    assert "SCADBUDDY_TDFP_EMAIL" not in environ
    assert "SCADBUDDY_TDFP_PASSWORD" not in environ
    assert "SCADBUDDY_TDFP_PASSWORD" not in os.environ


@pytest.mark.parametrize("text", ["3DFP login failed", "the browser did not start"])
def test_main_exits_1_when_the_start_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    text: str,
) -> None:
    _patch_main(monkeypatch, tmp_path)

    async def refused(settings: BrowserSettings) -> None:
        raise StartRefusedError(text)

    monkeypatch.setattr(browser, "_main", refused)
    caplog.set_level(logging.ERROR)
    assert browser.main([]) == 1
    assert text in caplog.text
    assert "Traceback" not in caplog.text
    assert EMAIL not in caplog.text and PASSWORD not in caplog.text
