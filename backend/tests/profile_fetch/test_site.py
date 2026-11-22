"""#6625 Task 5: the 3DFP driver, `ThreeDfpSite`, against the local replica in `site/`
(plan Decision 17). The real site is never reached: every test passes the replica's
127.0.0.1 URL as ``base_url``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import sys
import time
import traceback
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import BrowserType, Page, async_playwright
from playwright.async_api import Error as PlaywrightError
from pydantic import SecretStr

from scadbuddy.profile_fetch import site
from scadbuddy.profile_fetch.site import USER_AGENT, ThreeDfpSite
from scadbuddy.profile_fetch.steps import SiteError
from scadbuddy.profile_fetch.wire import MAX_FILE_BYTES
from tests.profile_fetch.site.server import (
    EXPORT_FILE,
    EXPORT_NAME,
    Replica,
    ReplicaServer,
)

pytestmark = pytest.mark.requires_chromium

EMAIL = "owner-6625@example.invalid"
PASSWORD = "pw-6625-not-a-real-password"


@pytest.fixture(autouse=True)
def _quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The replica answers at once: `3dfp.mjs`'s settle waits shrink, and a check
    that never clears gives up within a second."""
    monkeypatch.setattr(site, "PACE", 0.05)
    monkeypatch.setattr(site, "BOT_CHECK_SECONDS", 1.0)
    monkeypatch.setattr(site, "STEP_TIMEOUT_MS", 3_000)
    monkeypatch.setattr(site, "LOGIN_WAIT_MS", 1_500)


@pytest.fixture
def replica() -> Iterator[ReplicaServer]:
    server = ReplicaServer(Replica(email=EMAIL, password=PASSWORD))
    yield server
    server.close()


def _site(
    replica: ReplicaServer, tmp_path: Path, *, email: str = EMAIL, password: str = PASSWORD
) -> ThreeDfpSite:
    return ThreeDfpSite(
        replica.url, SecretStr(email), SecretStr(password), tmp_path / "profile", headless=True
    )


@pytest.fixture
async def browser(replica: ReplicaServer, tmp_path: Path) -> AsyncIterator[ThreeDfpSite]:
    async with _site(replica, tmp_path) as driver:
        yield driver


async def test_login_fills_the_form_and_reaches_my_spools(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    await browser.login()
    assert replica.site.logins == 1
    assert len(replica.site.sessions) == 1


async def test_a_slow_sign_in_waits_for_the_site(
    browser: ThreeDfpSite, replica: ReplicaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The site signs in from its script and then goes to ``/my/dashboard``. A sign-in
    that takes longer than the old fixed pause (6 s at `PACE`, here 0.3 s) still signs in:
    the driver waits for the page to leave ``/login`` before it opens My Spools (#6941)."""
    monkeypatch.setattr(site, "LOGIN_WAIT_MS", 10_000)
    replica.site.scripted_login_delay_s = 1.0
    await browser.login()
    assert replica.site.logins == 1
    assert browser.page.url.endswith("/my/spools")


async def test_a_scripted_sign_in_slower_than_the_wait_is_login_failed(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    """A scripted sign-in that has not set its session when `LOGIN_WAIT_MS` (here 1.5 s)
    ends is judged by My Spools, which sends the browser back to ``/login``: the login is
    ``login_failed``, not ``timeout`` (claude-review on #6947)."""
    replica.site.scripted_login_delay_s = 3.0
    with pytest.raises(SiteError) as caught:
        await browser.login()
    assert caught.value.kind == "login_failed"
    assert replica.site.sessions == set()
    _chain_holds_no_secret(caught.value)


async def test_a_scripted_sign_in_refused_is_login_failed(
    replica: ReplicaServer, tmp_path: Path
) -> None:
    """The script's refusal leaves the page on ``/login``: once the wait ends, the login
    is ``login_failed``."""
    replica.site.scripted_login_delay_s = 0.0
    async with _site(replica, tmp_path, password="wrong-" + PASSWORD) as driver:
        with pytest.raises(SiteError) as caught:
            await driver.login()
    assert caught.value.kind == "login_failed"
    assert replica.site.logins == 0
    _chain_holds_no_secret(caught.value)


def _no_secret(text: str) -> None:
    assert EMAIL not in text
    assert PASSWORD not in text


async def test_a_failed_login_logs_no_secret(
    replica: ReplicaServer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with _site(replica, tmp_path, password="wrong-" + PASSWORD) as driver:
        with pytest.raises(SiteError) as caught:
            await driver.login()
    assert caught.value.kind == "login_failed"
    assert replica.site.logins == 0
    for text in (caplog.text, str(caught.value), "".join(traceback.format_exception(caught.value))):
        _no_secret(text)
        assert "wrong-" not in text


async def test_a_login_form_that_times_out_carries_no_secret(
    replica: ReplicaServer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    replica.site.login_form = "disabled"
    async with _site(replica, tmp_path) as driver:
        with pytest.raises(SiteError) as caught:
            await driver.login()
    assert caught.value.kind == "timeout"
    # Playwright's fill error quotes the value it filled: it must not ride along.
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    for text in (caplog.text, str(caught.value), "".join(traceback.format_exception(caught.value))):
        _no_secret(text)


async def test_a_login_to_a_closed_port_is_browser_not_login_failed(tmp_path: Path) -> None:
    """A refused connection during the login is the browser's failure, retried; never
    ``login_failed``, which ends the worker at start and the run at once (R-F1)."""
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]
    driver = ThreeDfpSite(
        f"http://127.0.0.1:{port}",
        SecretStr(EMAIL),
        SecretStr(PASSWORD),
        tmp_path / "profile",
        headless=True,
    )
    async with driver:
        with pytest.raises(SiteError) as caught:
            await driver.login()
    assert caught.value.kind == "browser"
    _chain_holds_no_secret(caught.value)


async def test_a_navigation_error_during_the_login_is_browser(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    """The login form's post aborted by the network (``net::ERR_FAILED``) is ``browser``;
    the same form with a wrong password stays ``login_failed``
    (`test_a_failed_login_logs_no_secret`)."""

    async def abort_post(route: Any) -> None:
        if route.request.method == "POST":
            await route.abort()
        else:
            await route.continue_()

    await browser.page.route("**/login", abort_post)
    await browser.page.route("**/my/spools", lambda route: route.abort("connectionreset"))
    with pytest.raises(SiteError) as caught:
        await browser.login()
    assert caught.value.kind == "browser"
    _chain_holds_no_secret(caught.value)


async def test_add_clicks_add_to_my_spools_once(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    await browser.login()
    await browser.ensure_in_my_spools(30173)
    await browser.ensure_in_my_spools(30173)
    assert replica.site.adds == [30173]


async def test_an_unknown_id_is_tdfp_not_found(browser: ThreeDfpSite) -> None:
    await browser.login()
    with pytest.raises(SiteError) as caught:
        await browser.ensure_in_my_spools(99999)
    assert caught.value.kind == "tdfp_not_found"


async def test_export_picks_slicer_printer_and_nozzle_and_reads_the_blob(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    await browser.login()
    await browser.ensure_in_my_spools(30173)
    exported = await browser.export(30173, "H2C", "0.4")
    assert exported.name == EXPORT_NAME
    assert exported.text == EXPORT_FILE.read_text(encoding="utf-8")
    assert replica.site.exports == [
        {"id": "30173", "slicer": "Bambu Studio", "printer": "H2C", "nozzle": "0.4 mm"}
    ]


async def test_an_option_the_dialog_lacks_is_reported(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    await browser.login()
    await browser.ensure_in_my_spools(30173)
    with pytest.raises(SiteError) as caught:
        await browser.export(30173, "H2C", "0.2")
    assert caught.value.kind == "option_missing"
    assert "0.2 mm" in str(caught.value)
    assert replica.site.exports == []
    # The page is usable again: the next nozzle exports.
    exported = await browser.export(30173, "H2C", "0.4")
    assert exported.name == EXPORT_NAME


async def test_an_export_over_512_kib_is_refused_in_the_page(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    """A file longer than `MAX_FILE_BYTES` is refused by the page's hook before its text
    crosses into the worker: ``file_too_large``, a plain message, and nothing kept in the
    page for the driver to read (R-F7)."""
    replica.site.export_pad = MAX_FILE_BYTES + 1
    await browser.login()
    await browser.ensure_in_my_spools(30173)
    with pytest.raises(SiteError) as caught:
        await browser.export(30173, "H2C", "0.4")
    assert caught.value.kind == "file_too_large"
    assert str(caught.value) == "the exported file of H2C 0.4 mm is larger than 512 KiB"
    assert await browser.page.evaluate("() => window.__dl.map(d => 'text' in d)") == [False]
    # The page is usable again: the next file, of a normal size, exports.
    replica.site.export_pad = 0
    exported = await browser.export(30173, "H2C", "0.4")
    assert exported.text == EXPORT_FILE.read_text(encoding="utf-8")


async def test_an_export_of_512_kib_exactly_passes(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    replica.site.export_pad = MAX_FILE_BYTES - len(EXPORT_FILE.read_text(encoding="utf-8"))
    await browser.login()
    await browser.ensure_in_my_spools(30173)
    exported = await browser.export(30173, "H2C", "0.4")
    assert len(exported.text) == MAX_FILE_BYTES


@pytest.mark.parametrize(
    ("download", "kind"),
    [
        ("pushed_entry", "file_too_large"),
        ("pushed_text", "browser"),
        ("pushed_long_name", "browser"),
        ("pushed_number_text", "browser"),
    ],
)
async def test_an_entry_the_page_pushes_itself_is_refused_without_its_text(
    browser: ThreeDfpSite,
    replica: ReplicaServer,
    monkeypatch: pytest.MonkeyPatch,
    download: str,
    kind: str,
) -> None:
    """The page's own script can bypass the hook and push to ``window.__dl``: an entry
    over `MAX_FILE_BYTES` with no ``tooLarge`` flag, one that is not an object, one whose
    name is over `MAX_NAME_CHARS`, or one whose text is not a string. The worker refuses all,
    and no `Page.evaluate` brings the text across CDP (#6726)."""
    replica.site.download = download  # type: ignore[assignment]
    replica.site.export_pad = MAX_FILE_BYTES + 1
    await browser.login()
    await browser.ensure_in_my_spools(30173)
    page = browser.page
    evaluate = page.evaluate
    answers: list[Any] = []

    async def spy(expression: str, arg: Any = None) -> Any:
        answer = await evaluate(expression, arg)
        answers.append(answer)
        return answer

    monkeypatch.setattr(page, "evaluate", spy)
    with pytest.raises(SiteError) as caught:
        await browser.export(30173, "H2C", "0.4")
    assert caught.value.kind == kind
    assert answers, "the export read nothing from the page"
    assert max(len(json.dumps(answer)) for answer in answers) < 1024
    # The page's list still holds the oversized text: the driver refused it, not the page.
    if download != "pushed_number_text":
        length = "() => { const e = window.__dl[0]; return (e.text ?? e).length; }"
        assert await evaluate(length) > MAX_FILE_BYTES


async def test_a_checkpoint_that_never_clears_is_bot_check(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    replica.site.checkpoint = "never"
    with pytest.raises(SiteError) as caught:
        await browser.login()
    assert caught.value.kind == "bot_check"


async def test_a_checkpoint_that_clears_is_passed(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    replica.site.checkpoint = "clears"
    await browser.login()
    assert replica.site.logins == 1


async def test_a_redirect_to_login_logs_in_again(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    await browser.login()
    await browser.ensure_in_my_spools(30173)
    replica.site.expire_sessions()
    exported = await browser.export(30173, "H2C", "0.4")
    assert exported.name == EXPORT_NAME
    assert replica.site.logins == 2


async def test_the_user_agent_is_not_headless(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    await browser.login()
    assert replica.site.page_user_agents
    assert set(replica.site.page_user_agents) == {USER_AGENT}
    assert set(replica.site.header_user_agents) == {USER_AGENT}


async def test_a_download_the_hook_misses_writes_nothing(
    replica: ReplicaServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A download the blob hook does not see (a synthetic click on the blob anchor) is
    refused by the browser, not saved to Playwright's temp dir: the hook is the only way an
    export is read (review Important 1)."""
    monkeypatch.setattr(site, "BLOB_WAIT_MS", 2_000, raising=False)
    temp = tmp_path / "tmp"
    temp.mkdir()
    # Playwright's driver keeps accepted downloads under `$TMPDIR/playwright-artifacts-*`.
    monkeypatch.setenv("TMPDIR", str(temp))
    replica.site.download = "unhooked"
    async with _site(replica, tmp_path) as driver:
        await driver.login()
        await driver.ensure_in_my_spools(30173)
        with pytest.raises(SiteError) as caught:
            await driver.export(30173, "H2C", "0.4")
        assert caught.value.kind == "timeout"
        assert replica.site.exports == [
            {"id": "30173", "slicer": "Bambu Studio", "printer": "H2C", "nozzle": "0.4 mm"}
        ]
        # Checked while the browser runs: Playwright removes its artifacts dir on close.
        assert [path for path in temp.rglob("*") if path.is_file()] == []


async def test_a_strict_mode_violation_in_export_is_browser(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    """Two options of one name make Playwright's click raise its strict-mode error: the
    export says ``browser``, names the step, and closes its dialog (review Minor 1)."""
    replica.site.options = [["Bambu Studio"], ["H2C"], ["0.4 mm", "0.4 mm"]]
    await browser.login()
    await browser.ensure_in_my_spools(30173)
    with pytest.raises(SiteError) as caught:
        await browser.export(30173, "H2C", "0.4")
    assert caught.value.kind == "browser"
    assert "exporting H2C 0.4 mm of 30173" in str(caught.value)
    _no_secret(str(caught.value))
    assert await browser.page.locator("[role=dialog]").count() == 0
    assert await browser.page.locator("[role=listbox]").count() == 0


async def test_a_closed_page_in_ensure_is_browser(browser: ThreeDfpSite) -> None:
    await browser.login()
    await browser.page.close()
    with pytest.raises(SiteError) as caught:
        await browser.ensure_in_my_spools(30173)
    assert caught.value.kind == "browser"
    assert "adding 3DFP filament 30173" in str(caught.value)


async def test_a_page_closed_during_the_bot_check_is_browser_at_once(
    browser: ThreeDfpSite, replica: ReplicaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed page is not "still at the checkpoint": it fails at once as ``browser``,
    not after `BOT_CHECK_SECONDS` as ``bot_check`` (review Minor 2)."""
    monkeypatch.setattr(site, "BOT_CHECK_SECONDS", 10.0)
    replica.site.checkpoint = "never"
    page = browser.page

    async def close_soon() -> None:
        await asyncio.sleep(0.5)
        await page.close()

    closer = asyncio.create_task(close_soon())
    started = time.monotonic()
    with pytest.raises(SiteError) as caught:
        await browser.login()
    await closer
    assert caught.value.kind == "browser"
    assert time.monotonic() - started < 5


async def test_an_empty_option_list_is_option_missing(
    browser: ThreeDfpSite, replica: ReplicaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A combobox that opens no option is ``option_missing`` after `OPTION_WAIT_MS`,
    not a step ``timeout`` (review Minor 3)."""
    monkeypatch.setattr(site, "OPTION_WAIT_MS", 300, raising=False)
    replica.site.options = [["Bambu Studio"], ["H2C"], []]
    await browser.login()
    await browser.ensure_in_my_spools(30173)
    with pytest.raises(SiteError) as caught:
        await browser.export(30173, "H2C", "0.4")
    assert caught.value.kind == "option_missing"
    assert "0.4 mm" in str(caught.value)


async def test_a_slash_in_the_export_name_becomes_a_dash(
    browser: ThreeDfpSite, replica: ReplicaServer
) -> None:
    """`3dfp.mjs` replaces every ``/`` in the name with ``-``; the driver returns the same
    name (review Minor 4)."""
    replica.site.export_name = "Inland PLA+ @BBL H2C/H2D 0.4 nozzle.json"
    await browser.login()
    await browser.ensure_in_my_spools(30173)
    exported = await browser.export(30173, "H2C", "0.4")
    assert exported.name == "Inland PLA+ @BBL H2C-H2D 0.4 nozzle.json"


def _chain(error: BaseException) -> list[BaseException]:
    """``error`` and every exception its ``__cause__`` and ``__context__`` reach."""
    seen: list[BaseException] = []
    todo: list[BaseException] = [error]
    while todo:
        current = todo.pop()
        if any(current is known for known in seen):
            continue
        seen.append(current)
        todo.extend(e for e in (current.__cause__, current.__context__) if e is not None)
    return seen


def _chain_holds_no_secret(error: BaseException) -> None:
    for linked in _chain(error):
        _no_secret(str(linked))
        _no_secret(repr(linked))


async def _crash(page: Page) -> None:
    """Crash the page's renderer with Chromium's ``chrome://crash`` (plan build Decision
    49). With the pinned 1.64.1 it crashes only a page that is not on an http(s) origin,
    so the page leaves for ``about:blank`` first, and only one that has not posted the
    login form (after that post the renderer hung instead). The navigation itself fails;
    Playwright delivers the ``crash`` event with the next call on the page, so the helper
    makes one and waits for the event."""
    await page.goto("about:blank")
    async with page.expect_event("crash", timeout=5_000):
        with contextlib.suppress(PlaywrightError):
            await page.goto("chrome://crash")
        await page.title()


async def test_a_failed_login_chain_holds_no_secret(replica: ReplicaServer, tmp_path: Path) -> None:
    async with _site(replica, tmp_path, password="wrong-" + PASSWORD) as driver:
        with pytest.raises(SiteError) as caught:
            await driver.login()
    assert caught.value.kind == "login_failed"
    _chain_holds_no_secret(caught.value)


async def test_a_crash_during_login_is_browser(browser: ThreeDfpSite) -> None:
    """A renderer that crashed is a browser failure, retried; not a refused login, which
    is not."""
    await _crash(browser.page)
    with pytest.raises(SiteError) as caught:
        await browser.login()
    assert caught.value.kind == "browser"
    _chain_holds_no_secret(caught.value)


async def test_a_browser_failure_chain_holds_no_secret(browser: ThreeDfpSite) -> None:
    await _crash(browser.page)
    with pytest.raises(SiteError) as caught:
        await browser.ensure_in_my_spools(30173)
    assert caught.value.kind == "browser"
    _chain_holds_no_secret(caught.value)


async def test_the_crash_event_is_wired(browser: ThreeDfpSite) -> None:
    """The page's ``crash`` event is wired: a wait for Vercel's check on a crashed page
    ends as ``browser`` at once. Without the event's flag the wait's Playwright error
    would escape as it is (measured with 1.64.1: on a crashed page ``title`` answers
    ``""`` and ``wait_for_timeout`` raises "Page crashed", while ``is_closed`` stays
    false)."""
    await _crash(browser.page)
    assert not browser.page.is_closed()
    with pytest.raises(SiteError) as caught:
        await browser._clear_checkpoint()
    assert caught.value.kind == "browser"


async def test_a_list_that_fills_within_the_option_wait_is_used(
    browser: ThreeDfpSite, replica: ReplicaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A combobox whose options appear a moment after it opens, within `OPTION_WAIT_MS`,
    exports; only a list that stays empty is ``option_missing``."""
    monkeypatch.setattr(site, "OPTION_WAIT_MS", 3_000)
    replica.site.option_delay_ms = 800
    await browser.login()
    await browser.ensure_in_my_spools(30173)
    exported = await browser.export(30173, "H2C", "0.4")
    assert exported.name == EXPORT_NAME


def _dumping(script: Path, dumps: Path, role: str, then: str) -> Path:
    """A shell script that writes its environment (NUL-separated) to
    ``dumps/<role>-<pid>``, then execs ``then`` with its arguments."""
    script.write_text(f"#!/bin/sh\n/usr/bin/env -0 > '{dumps}/{role}-'$$\nexec {then} \"$@\"\n")
    script.chmod(0o755)
    return script


@pytest.mark.skipif(sys.platform != "linux", reason="Linux's Chromium and /bin/sh")
async def test_the_browser_gets_no_scadbuddy_environment(
    replica: ReplicaServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chromium is launched with an allowlisted environment (`site.browser_env`): neither
    the browser process nor a renderer it starts holds the 3DFP login or any other
    ``SCADBUDDY_`` variable, which a renderer exploit could read from its own memory
    (Task 6 review, I1).

    ``/proc/<pid>/environ`` cannot show it: Chromium writes its process title over that
    memory (measured with Playwright 1.64.1's Chromium: every browser and renderer read
    empty or as the tail of its command line, with or without the fix). So the browser
    is launched through a script that writes the environment it is given, and its
    renderers through ``--renderer-cmd-prefix`` with another."""
    monkeypatch.setenv("SCADBUDDY_TDFP_EMAIL", EMAIL)
    monkeypatch.setenv("SCADBUDDY_TDFP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SCADBUDDY_X", "another")
    dumps = tmp_path / "environ"
    dumps.mkdir()
    async with async_playwright() as playwright:
        chromium = playwright.chromium.executable_path
    wrapped = _dumping(tmp_path / "chrome", dumps, "browser", f"'{chromium}'")
    renderer = _dumping(tmp_path / "renderer", dumps, "renderer", "")
    launch = BrowserType.launch_persistent_context

    async def launching(self: BrowserType, user_data_dir: str, **kwargs: Any) -> Any:
        kwargs["executable_path"] = str(wrapped)
        kwargs["args"] = [f"--renderer-cmd-prefix={renderer}"]
        return await launch(self, user_data_dir, **kwargs)

    monkeypatch.setattr(BrowserType, "launch_persistent_context", launching)
    async with _site(replica, tmp_path) as driver:
        await driver.login()
    written = {path.name.split("-")[0]: path for path in dumps.iterdir()}
    assert set(written) == {"browser", "renderer"}, sorted(p.name for p in dumps.iterdir())
    for path in dumps.iterdir():
        env = [item.decode() for item in path.read_bytes().split(b"\0") if item]
        assert any(item.startswith("PATH=") for item in env), path.name
        leaked = [item.split("=", 1)[0] for item in env if item.startswith("SCADBUDDY_")]
        assert leaked == [], (path.name, leaked)
        for item in env:
            _no_secret(item)


def test_the_browser_env_keeps_only_the_allowlist() -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/w",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "LC_CTYPE": "C",
        "TZ": "UTC",
        "TMPDIR": "/tmp/w",
        "XDG_RUNTIME_DIR": "/run/w",
        "FONTCONFIG_PATH": "/etc/fonts",
        "DISPLAY": ":99",
        "XAUTHORITY": "/tmp/xvfb-run.x/Xauthority",
        "SCADBUDDY_TDFP_EMAIL": EMAIL,
        "SCADBUDDY_TDFP_PASSWORD": PASSWORD,
        "SCADBUDDY_SERVICE_TOKEN_FILE": "/var/run/secrets/token",
        "SCADBUDDY_X": "x",
        "AWS_SECRET_ACCESS_KEY": "k",
        "LD_PRELOAD": "/evil.so",
    }
    assert site.browser_env(environ) == {
        "PATH": "/usr/bin",
        "HOME": "/home/w",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "LC_CTYPE": "C",
        "TZ": "UTC",
        "TMPDIR": "/tmp/w",
        "XDG_RUNTIME_DIR": "/run/w",
        "FONTCONFIG_PATH": "/etc/fonts",
        "DISPLAY": ":99",
        "XAUTHORITY": "/tmp/xvfb-run.x/Xauthority",
    }


FAKE_EMAIL = "fake-email-6625@example.test"
FAKE_PASSWORD = "fake-pass-6625-XYZ"


def _encodings(secret: str) -> list[bytes]:
    """The forms a file in the profile could hold ``secret`` in: UTF-8 (JSON, SQLite text,
    LevelDB), UTF-16-LE (SQLite's UTF-16 text, Windows-style strings) and the form body's
    URL encoding."""
    encoded = secret.replace("@", "%40")
    return [secret.encode(), secret.encode("utf-16-le"), encoded.encode()]


@pytest.mark.skipif(
    not os.environ.get("DISPLAY") and os.environ.get("SCADBUDDY_TEST_CHROMIUM_REQUIRED") != "1",
    reason="headed Chromium needs a DISPLAY (run under xvfb-run)",
)
async def test_the_profile_keeps_no_login_after_a_login(tmp_path: Path) -> None:
    """R-F12: the persistent profile keeps the site's cookies, never the login itself. A
    login to the replica with a known email and password in headed Chromium (the
    worker's mode, Decision 5), then the browser closed: no file under the profile dir
    holds either one, in any of `_encodings`.

    Headed, because headless saved nothing to find: without the fix, headless Chromium
    (Playwright 1.64.1's) left neither string in the profile, while headed Chromium left
    the email in ``Default/Web Data``, ``Default/Login Data`` and the ``Default/Sessions``
    files. The preferences (`site.write_preferences`) removed the first two; the
    ``autocomplete=off`` the login sets on its fields removed the form state in
    ``Sessions``."""
    server = ReplicaServer(Replica(email=FAKE_EMAIL, password=FAKE_PASSWORD))
    profile = tmp_path / "profile"
    try:
        async with ThreeDfpSite(
            server.url, SecretStr(FAKE_EMAIL), SecretStr(FAKE_PASSWORD), profile, headless=False
        ) as driver:
            await driver.login()
        assert server.site.logins == 1
    finally:
        server.close()
    files = [path for path in profile.rglob("*") if path.is_file() and not path.is_symlink()]
    assert files, "Chromium wrote no profile"
    held = [
        (str(path.relative_to(profile)), secret)
        for path in files
        for secret in (FAKE_EMAIL, FAKE_PASSWORD)
        if any(form in path.read_bytes() for form in _encodings(secret))
    ]
    assert held == []


def test_the_preferences_merge_into_the_profile(tmp_path: Path) -> None:
    """`site.write_preferences` keeps every key Chromium wrote, sets the R-F12 ones, and
    leaves the file 0600 in 0700 dirs."""
    profile = tmp_path / "profile"
    (profile / "Default").mkdir(parents=True, mode=0o755)
    profile.chmod(0o755)
    (profile / "Default" / "Preferences").write_text(
        '{"autofill": {"profile_enabled": true, "last_version_deduped": 131},'
        ' "profile": {"name": "Person 1", "password_manager_enabled": true},'
        ' "browser": {"window_placement": {"top": 1}}}'
    )
    site.write_preferences(profile)
    path = profile / "Default" / "Preferences"
    written = json.loads(path.read_text())
    assert written == {
        "autofill": {
            "enabled": False,
            "profile_enabled": False,
            "credit_card_enabled": False,
            "last_version_deduped": 131,
        },
        "credentials_enable_service": False,
        "credentials_enable_autosignin": False,
        "profile": {"name": "Person 1", "password_manager_enabled": False},
        "browser": {"window_placement": {"top": 1}},
    }
    assert path.stat().st_mode & 0o777 == 0o600
    assert profile.stat().st_mode & 0o777 == 0o700
    assert (profile / "Default").stat().st_mode & 0o777 == 0o700
    assert sorted(p.name for p in (profile / "Default").iterdir()) == ["Preferences"]
