"""The 3dfilamentprofiles.com driver (#6625 Task 5): a Python port of the filament-intake
skill's `3dfp.mjs` (`.claude/skills/filament-intake/scripts/3dfp.mjs`), its ``login``,
``add`` and ``export`` steps, with the same selectors, the same in-page blob hook and the
same per-spool page.

Headed Chromium with a normal Chrome user agent is the mode measured to pass Vercel's
check (plan Decision 5; the skill's SKILL.md: headless with the ``HeadlessChrome`` UA gets
403 since 2026-11-01). Chromium's own sandbox stays on (Decision 6). No trace and no HAR
is recorded: a page's requests carry the login form. Downloads are refused
(``accept_downloads=False``): the in-page blob hook is the only way an export is read, so
the browser writes nothing outside the profile dir. The email and the password are only
ever passed to `Page.fill`; no log line, exception text or `evaluate` carries them, and
a failure inside the login is raised as a `SiteError` of a fixed text, its cause dropped.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Literal, Self

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import SecretStr

from scadbuddy.profile_fetch.wire import SLICER

logger = logging.getLogger(__name__)

SITE: Final = "https://3dfilamentprofiles.com"
#: `3dfp.mjs`'s: Vercel's firewall answers 403 to ``HeadlessChrome`` (seen 2026-11-01).
USER_AGENT: Final = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
#: How long Vercel's check may take to clear before the step is ``bot_check`` (Decision 13).
BOT_CHECK_SECONDS: float = 15.0
#: Multiplies `3dfp.mjs`'s settle waits (the site is a client-rendered app); tests shrink it.
PACE: float = 1.0
#: The longest one Playwright step (a click, a fill, the download) may take.
STEP_TIMEOUT_MS: float = 30_000
#: How long the export's blob may take to reach the hook after "Download" (`3dfp.mjs`'s).
BLOB_WAIT_MS: float = 15_000
#: How long an opened combobox may take to show its first option before the option the
#: step wants counts as missing.
OPTION_WAIT_MS: float = 5_000
#: The title of Vercel's check page (it answers 429; the skill's SKILL.md).
CHECKPOINT_TITLE: Final = "Vercel Security Checkpoint"

#: `3dfp.mjs`'s hook: Chromium died on the export's blob download intermittently ("Target
#: page, context or browser has been closed"), so the page reads the blob itself.
BLOB_HOOK: Final = """() => {
  window.__dl = [];
  const click = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () {
    if (this.download && this.href.startsWith('blob:')) {
      fetch(this.href).then(r => r.text()).then(text => window.__dl.push({ name: this.download, text }));
      return;
    }
    return click.call(this);
  };
}"""  # noqa: E501 (copied from 3dfp.mjs as it is)

Kind = Literal[
    "login_failed", "tdfp_not_found", "bot_check", "option_missing", "timeout", "browser"
]

_MESSAGES: Final[dict[str, str]] = {
    "login_failed": "3dfilamentprofiles.com refused the login",
    "tdfp_not_found": "3dfilamentprofiles.com has no such filament",
    "bot_check": "3dfilamentprofiles.com's bot check did not clear",
    "option_missing": "the export dialog lacks an option",
    "timeout": "3dfilamentprofiles.com did not answer in time",
    "browser": "the browser failed on 3dfilamentprofiles.com",
}


class SiteError(Exception):
    """A step on the site that failed. ``kind`` says how; the message never holds the
    email or the password."""

    def __init__(self, kind: Kind, message: str | None = None) -> None:
        super().__init__(message or _MESSAGES[kind])
        self.kind: Kind = kind


@dataclass(frozen=True)
class ExportedFile:
    """One exported preset: the name the site's download gave it, and its text."""

    name: str
    text: str


class ThreeDfpSite:
    """One browser on 3dfilamentprofiles.com (or a replica at ``base_url``), with its
    profile, which keeps the site's cookies, in ``profile_dir``. Use it as an async
    context manager."""

    def __init__(
        self,
        base_url: str,
        email: SecretStr,
        password: SecretStr,
        profile_dir: Path,
        headless: bool = False,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._email = email
        self._password = password
        self._profile_dir = profile_dir
        self._headless = headless
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._crashed = False

    async def __aenter__(self) -> Self:
        self._profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._playwright = await async_playwright().start()
        try:
            # No record_har_path and no tracing: both would keep the login form's body.
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self._profile_dir),
                headless=self._headless,
                user_agent=USER_AGENT,
                chromium_sandbox=True,
                # The blob hook reads the export in the page; any other download is refused
                # rather than saved to Playwright's temp dir, outside `profile_dir`.
                accept_downloads=False,
            )
            self._context.set_default_timeout(STEP_TIMEOUT_MS)
            await self._context.add_init_script(script=f"({BLOB_HOOK})()")
            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
            self._page.on("crash", self._on_crash)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._page = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("ThreeDfpSite is used outside its `async with`")
        return self._page

    async def login(self) -> None:
        """`3dfp.mjs login`: fill the form, sign in, and check that My Spools opens.
        `SiteError` ``login_failed`` when the site sends the browser back to ``/login``."""
        page = self.page
        failed: SiteError | None = None
        try:
            await self._goto("/login", settle_ms=5000)
            await page.fill("input[name=email]", self._email.get_secret_value())
            await page.fill("input[name=password]", self._password.get_secret_value())
            await page.get_by_role("button", name="Sign In", exact=True).click()
            await page.wait_for_load_state()
            await self._pause(6000)
            await self._goto("/my/spools", settle_ms=4000)
        except SiteError:
            raise
        except PlaywrightTimeoutError:
            failed = SiteError("timeout", "the 3dfilamentprofiles.com login timed out")
        except PlaywrightError:
            if self._page_gone():
                # A crashed renderer or a closed page is the browser's failure, retried;
                # not a refused login, which is not.
                failed = SiteError("browser", "the browser failed during the login")
            else:
                failed = SiteError("login_failed", "the 3dfilamentprofiles.com login failed")
        if failed is not None:
            # Raised outside the handler, so neither its cause nor its context is
            # Playwright's error: a fill's call log quotes the value it filled (measured
            # with Playwright 1.64.1: `- fill("<value>")`).
            raise failed
        if "/login" in page.url:
            raise SiteError("login_failed")
        logger.info("signed in to 3dfilamentprofiles.com")

    async def ensure_in_my_spools(self, tdfp_id: int) -> None:
        """`3dfp.mjs add`: on the filament's page, click "Add to My Spools" when it is
        there; a filament already in My Spools has none."""
        page = self.page
        try:
            await self._open(f"/filament/details/{tdfp_id}", tdfp_id, settle_ms=4000)
            button = page.get_by_role("button", name="Add to My Spools").first
            if await button.count():
                await button.click()
                await self._pause(2500)
                logger.info("added 3DFP filament %d to My Spools", tdfp_id)
        except PlaywrightTimeoutError:
            raise SiteError("timeout", f"adding 3DFP filament {tdfp_id} timed out") from None
        except PlaywrightError:
            raise SiteError(
                "browser", f"adding 3DFP filament {tdfp_id} failed in the browser"
            ) from None

    async def export(self, tdfp_id: int, printer_model: str, nozzle: str) -> ExportedFile:
        """`3dfp.mjs export` for one printer model and nozzle (``"0.4"``): on the
        spool's own page, open "Export slicer profile", pick `SLICER`, the model and
        ``"<nozzle> mm"``, acknowledge, download, and read the blob the hook kept.
        `SiteError` ``option_missing`` when the dialog does not offer one of the three."""
        page = self.page
        try:
            # The per-spool page, not the My Spools list: a list row that groups several
            # spools of one filament hides the export button behind "Expand the group".
            await self._open(f"/my/spools/details/{tdfp_id}", tdfp_id, settle_ms=5000)
            button = page.get_by_role("button", name="Export slicer profile").first
            if not await button.count():
                raise SiteError("tdfp_not_found", f"3DFP filament {tdfp_id} is not in My Spools")
            await button.click()
            await self._pause(1500)
            dialog = page.locator("[role=dialog]").last
            for index, label in enumerate((SLICER, printer_model, f"{nozzle} mm")):
                await dialog.get_by_role("combobox").nth(index).click()
                await self._pause(400)
                try:
                    await page.get_by_role("option").first.wait_for(timeout=OPTION_WAIT_MS)
                except PlaywrightTimeoutError:
                    raise SiteError(
                        "option_missing", f"the export dialog offers no {label!r} option"
                    ) from None
                option = page.get_by_role("option", name=label, exact=True)
                if not await option.count():
                    raise SiteError(
                        "option_missing", f"the export dialog offers no {label!r} option"
                    )
                await option.click()
                await self._pause(500)
            ack = dialog.get_by_role("checkbox").last
            if await ack.get_attribute("aria-checked") == "false":
                await ack.click()
            await page.evaluate("() => { window.__dl = []; }")
            await dialog.get_by_role("button", name=re.compile(r"^Download")).click()
            await page.wait_for_function("() => window.__dl.length > 0", timeout=BLOB_WAIT_MS)
            download = await page.evaluate("() => window.__dl[0]")
            await page.keyboard.press("Escape")
            await self._pause(600)
        except SiteError:
            await self._dismiss()
            raise
        except PlaywrightTimeoutError:
            await self._dismiss()
            raise SiteError(
                "timeout", f"exporting {printer_model} {nozzle} mm of {tdfp_id} timed out"
            ) from None
        except PlaywrightError:
            await self._dismiss()
            raise SiteError(
                "browser",
                f"exporting {printer_model} {nozzle} mm of {tdfp_id} failed in the browser",
            ) from None
        logger.info("exported 3DFP filament %d for %s %s mm", tdfp_id, printer_model, nozzle)
        # As `3dfp.mjs` names its file: the site's name with every "/" made "-".
        name = str(download["name"]).replace("/", "-")
        return ExportedFile(name=name, text=str(download["text"]))

    # -- steps ----------------------------------------------------------------------------

    async def _pause(self, ms: float) -> None:
        """`3dfp.mjs`'s ``waitForTimeout``, scaled by `PACE`."""
        if ms * PACE > 0:
            await self.page.wait_for_timeout(ms * PACE)

    async def _goto(self, path: str, *, settle_ms: float) -> int | None:
        """Open ``path`` and wait out Vercel's check; the answer's HTTP status."""
        status = await self._load(path)
        if status == 429 or await self._at_checkpoint():
            await self._clear_checkpoint()
            # Once more with the check's cookie, for the page's own status.
            status = await self._load(path)
            if status == 429 or await self._at_checkpoint():
                raise SiteError("bot_check")
        await self._pause(settle_ms)
        return status

    async def _load(self, path: str) -> int | None:
        response = await self.page.goto(self._base + path, wait_until="domcontentloaded")
        return None if response is None else response.status

    async def _open(self, path: str, tdfp_id: int, *, settle_ms: float) -> None:
        """`_goto`, signing in again when the site sends the browser to ``/login``; a
        404 is `SiteError` ``tdfp_not_found``."""
        status = await self._goto(path, settle_ms=settle_ms)
        if "/login" in self.page.url:
            logger.info("3dfilamentprofiles.com asked for the login again")
            await self.login()
            status = await self._goto(path, settle_ms=settle_ms)
            if "/login" in self.page.url:
                raise SiteError("login_failed")
        if status == 404:
            raise SiteError("tdfp_not_found", f"3dfilamentprofiles.com has no filament {tdfp_id}")

    def _on_crash(self, page: Page) -> None:
        self._crashed = True

    def _page_gone(self) -> bool:
        """The page is closed or its renderer crashed."""
        return self.page.is_closed() or self._crashed

    def _check_page(self) -> None:
        """`SiteError` ``browser`` when the page is closed or its renderer crashed."""
        if self._page_gone():
            raise SiteError("browser", "the browser page closed or crashed")

    async def _at_checkpoint(self) -> bool:
        try:
            return CHECKPOINT_TITLE in await self.page.title()
        except PlaywrightError:
            self._check_page()
            # The check's own reload destroyed the page's context: not settled yet.
            return True

    async def _clear_checkpoint(self) -> None:
        """Wait up to `BOT_CHECK_SECONDS` for the check to reload into the page."""
        logger.info("waiting for 3dfilamentprofiles.com's bot check")
        deadline = time.monotonic() + BOT_CHECK_SECONDS
        while time.monotonic() < deadline:
            try:
                await self.page.wait_for_timeout(250)
            except PlaywrightError:
                self._check_page()
                raise
            if not await self._at_checkpoint():
                return
        raise SiteError("bot_check")

    async def _dismiss(self) -> None:
        """Close an open option list and the dialog, as `3dfp.mjs` does after a failure."""
        for _ in range(2):
            try:
                await self.page.keyboard.press("Escape")
            except PlaywrightError:
                return
