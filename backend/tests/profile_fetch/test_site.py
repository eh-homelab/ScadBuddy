"""#6625 Task 5: the 3DFP driver, `ThreeDfpSite`, against the local replica in `site/`
(plan Decision 17). The real site is never reached: every test passes the replica's
127.0.0.1 URL as ``base_url``."""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from scadbuddy.profile_fetch import site
from scadbuddy.profile_fetch.site import USER_AGENT, SiteError, ThreeDfpSite
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
