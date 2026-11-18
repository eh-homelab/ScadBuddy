"""#6625 Task 5: the 3DFP driver, `ThreeDfpSite`, against the local replica in `site/`
(plan Decision 17). The real site is never reached: every test passes the replica's
127.0.0.1 URL as ``base_url``."""

from __future__ import annotations

import logging
import traceback
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from scadbuddy.profile_fetch import site
from scadbuddy.profile_fetch.site import USER_AGENT, SiteError, ThreeDfpSite
from tests.profile_fetch.site.server import EXPORT_FILE, EXPORT_NAME, Replica, ReplicaServer

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
