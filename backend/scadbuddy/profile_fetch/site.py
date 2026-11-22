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
The profile keeps the site's cookies, not the login: autofill, form saving and the
password manager are off (`write_preferences`), and the login's fields have autocomplete
off, so no form state is kept either (R-F12, `test_the_profile_keeps_no_login_after_a_login`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import SecretStr

from scadbuddy.profile_fetch.steps import ExportedFile, SiteError
from scadbuddy.profile_fetch.wire import MAX_FILE_BYTES, SLICER

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
#: How long the page may take to leave ``/login`` after "Sign In" before the driver opens My
#: Spools to judge the login. On a sign-in that works the site goes to ``/my/dashboard``
#: (measured on prod, #6941); a fixed 6 s pause read a slow sign-in as a refused one.
LOGIN_WAIT_MS: float = 30_000
#: How long the export's blob may take to reach the hook after "Download" (`3dfp.mjs`'s).
BLOB_WAIT_MS: float = 15_000
#: How long an opened combobox may take to show its first option before the option the
#: step wants counts as missing.
OPTION_WAIT_MS: float = 5_000
#: The title of Vercel's check page (it answers 429; the skill's SKILL.md).
CHECKPOINT_TITLE: Final = "Vercel Security Checkpoint"
#: The longest exported file name, in UTF-16 units, the worker reads from the page.
MAX_NAME_CHARS: Final = 1024
#: What the worker reads of the hook's first entry before its text: the entry's type, the
#: hook's flag, and the name's and the text's lengths (-1 for a value that is not a string).
EXPORT_SHAPE: Final = """() => {
  const e = window.__dl[0];
  const kind = e === null ? 'null' : Array.isArray(e) ? 'array' : typeof e;
  if (kind !== 'object') return { kind };
  const name = e.name;
  const text = e.text;
  return {
    kind,
    tooLarge: e.tooLarge === true,
    name: typeof name === 'string' ? name.length : -1,
    text: typeof text === 'string' ? text.length : -1,
  };
}"""
#: The entry's name and text, each read once and returned only within the caps, so an
#: entry whose getters answer differently on a second read still sends no larger text.
EXPORT_READ: Final = """() => {
  const e = window.__dl[0];
  const name = e.name;
  const text = e.text;
  if (typeof name !== 'string' || typeof text !== 'string') return null;
  if (name.length > __NAME__ || text.length > __MAX__) return null;
  return { name, text };
}""".replace("__NAME__", str(MAX_NAME_CHARS)).replace("__MAX__", str(MAX_FILE_BYTES))

#: `3dfp.mjs`'s hook: Chromium died on the export's blob download intermittently ("Target
#: page, context or browser has been closed"), so the page reads the blob itself. One
#: change from `3dfp.mjs`: a text longer than `MAX_FILE_BYTES` UTF-16 units is marked
#: ``tooLarge`` in the page, a first filter only. The page is untrusted and its own scripts
#: can push to ``window.__dl``, so the cap that holds is the worker's (`_read_export`): it
#: reads an entry's type and lengths first and its text only when they are within the caps
#: (R-F7, #6726). A UTF-8 file is at least as many bytes as its text has units, so no file
#: under the API's byte cap is refused here.
BLOB_HOOK: Final = """() => {
  window.__dl = [];
  const click = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () {
    if (this.download && this.href.startsWith('blob:')) {
      fetch(this.href).then(r => r.text()).then(text => window.__dl.push(
        text.length > __MAX__ ? { name: this.download, tooLarge: true } : { name: this.download, text }));
      return;
    }
    return click.call(this);
  };
}""".replace("__MAX__", str(MAX_FILE_BYTES))  # noqa: E501 (3dfp.mjs's, with the size cap)


#: The variables Chromium is launched with (`browser_env`): the locale, the time zone, the
#: home and temp dirs, fontconfig's, and the X display that ``xvfb-run`` sets for the
#: headed browser (Decision 5). Nothing else: not the 3DFP login, not the service
#: credential's file, no ``SCADBUDDY_`` variable.
BROWSER_ENV: Final = frozenset({"PATH", "HOME", "LANG", "TZ", "TMPDIR", "DISPLAY", "XAUTHORITY"})
BROWSER_ENV_PREFIXES: Final = ("LC_", "XDG_", "FONTCONFIG_")


def browser_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The allowlisted part of ``environ`` that Chromium gets. Playwright's default is the
    driver's whole environment, which the browser and every renderer it forks would hold
    in their memory (Task 6 review, I1)."""
    return {
        name: value
        for name, value in environ.items()
        if name in BROWSER_ENV or name.startswith(BROWSER_ENV_PREFIXES)
    }


#: The Chromium preferences `write_preferences` sets before every launch (R-F12): no
#: autofill, no address or card saving, no password manager and no credentials service,
#: so the profile, which keeps the site's cookies on purpose, never keeps the login.
PREFERENCES: Final[Mapping[str, Mapping[str, bool] | bool]] = {
    "autofill": {"enabled": False, "profile_enabled": False, "credit_card_enabled": False},
    "credentials_enable_service": False,
    "credentials_enable_autosignin": False,
    "profile": {"password_manager_enabled": False},
}


def write_preferences(profile_dir: Path) -> None:
    """Merge `PREFERENCES` into the profile's ``Default/Preferences``, keeping every other
    key Chromium wrote there. The dirs are 0700 and the file 0600."""
    default = profile_dir / "Default"
    default.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile_dir.chmod(0o700)
    default.chmod(0o700)
    path = default / "Preferences"
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        current = {}
    except ValueError:
        # Chromium itself starts over from defaults on a file it cannot parse.
        current = {}
    if not isinstance(current, dict):
        current = {}
    for key, value in PREFERENCES.items():
        if isinstance(value, Mapping):
            section = current.get(key)
            current[key] = {**(section if isinstance(section, dict) else {}), **value}
        else:
            current[key] = value
    staged = default / "Preferences.scadbuddy"
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(current, out)
    staged.chmod(0o600)
    os.replace(staged, path)


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
        write_preferences(self._profile_dir)
        self._playwright = await async_playwright().start()
        env: dict[str, str | float | bool] = dict(browser_env(os.environ))
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
                env=env,
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
        `SiteError` ``login_failed`` only when the site sends the browser back to
        ``/login`` (its own refusal): it waits up to `LOGIN_WAIT_MS` for the page to leave
        ``/login`` after the click, then opens My Spools, which sends a refused login back
        there. A step that times out is ``timeout``; any other
        Playwright error (a refused connection, a failed navigation, a crash) is
        ``browser``: both are retried, so a network blip never reads as a bad password
        (R-F1, which replaces build Decision 48)."""
        page = self.page
        failed: SiteError | None = None
        try:
            await self._goto("/login", settle_ms=5000)
            # Chromium keeps a page's form state in the profile's ``Sessions`` files (for
            # back and forward), but never for a field with autocomplete off (R-F12).
            for field in ("email", "password"):
                await page.locator(f"input[name={field}]").evaluate(
                    "e => e.setAttribute('autocomplete', 'off')"
                )
            await page.fill("input[name=email]", self._email.get_secret_value())
            await page.fill("input[name=password]", self._password.get_secret_value())
            await page.get_by_role("button", name="Sign In", exact=True).click()
            # A timeout means the page is still on the form after the whole wait: with
            # ``wait_until="commit"`` the wait ends as soon as the URL leaves ``/login``.
            # My Spools then tells a refusal (it sends the browser back to ``/login``) from
            # a network failure (it fails).
            with suppress(PlaywrightTimeoutError):
                await page.wait_for_url(
                    lambda url: "/login" not in url, wait_until="commit", timeout=LOGIN_WAIT_MS
                )
            await self._goto("/my/spools", settle_ms=4000)
        except SiteError:
            raise
        except PlaywrightTimeoutError:
            failed = SiteError("timeout", "the 3dfilamentprofiles.com login timed out")
        except PlaywrightError:
            # Not a refusal: the site answered nothing the URL check below could read.
            failed = SiteError("browser", "the browser failed during the login")
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
        `SiteError` ``option_missing`` when the dialog does not offer one of the three,
        and ``file_too_large`` when the file is over `MAX_FILE_BYTES` (`_read_export`)."""
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
            name, text = await self._read_export(printer_model, nozzle)
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
        return ExportedFile(name=name.replace("/", "-"), text=text)

    async def _read_export(self, printer_model: str, nozzle: str) -> tuple[str, str]:
        """The hook's first entry, as its name and text. The page is untrusted and can push
        any entry, so its type and lengths are read first (`EXPORT_SHAPE`), and its text
        only when it is an object whose name and text are strings within `MAX_NAME_CHARS`
        and `MAX_FILE_BYTES` (`EXPORT_READ`): an oversized text never crosses CDP into the
        worker (#6726). `SiteError` ``file_too_large`` for a text over the cap, ``browser``
        for an entry of any other shape."""
        too_large = SiteError(
            "file_too_large",
            f"the exported file of {printer_model} {nozzle} mm is larger than 512 KiB",
        )
        malformed = SiteError(
            "browser", f"the page's export of {printer_model} {nozzle} mm is not a file"
        )
        shape = await self.page.evaluate(EXPORT_SHAPE)
        if not isinstance(shape, dict) or shape.get("kind") != "object":
            raise malformed
        if shape.get("tooLarge") is True:
            raise too_large
        name_length, text_length = shape.get("name"), shape.get("text")
        if not isinstance(name_length, int) or not isinstance(text_length, int):
            raise malformed
        if not 0 <= name_length <= MAX_NAME_CHARS or text_length < 0:
            raise malformed
        if text_length > MAX_FILE_BYTES:
            raise too_large
        download = await self.page.evaluate(EXPORT_READ)
        if not isinstance(download, dict):
            raise malformed
        name, text = download.get("name"), download.get("text")
        if not isinstance(name, str) or not isinstance(text, str):
            raise malformed
        if len(name) > MAX_NAME_CHARS:
            raise malformed
        if len(text) > MAX_FILE_BYTES:
            raise too_large
        return name, text

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
