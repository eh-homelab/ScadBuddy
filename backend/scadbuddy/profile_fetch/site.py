"""The 3dfilamentprofiles.com driver (#6625 Task 5, #7025): a Python port of the filament-intake
skill's `3dfp.mjs` (`.claude/skills/filament-intake/scripts/3dfp.mjs`), its ``login``,
``add``, ``export`` and ``search`` steps, with the same selectors, the same in-page blob hook
and the same per-spool page; and the add form (/filament/add) as the script posted on #7025
filled it (`ThreeDfpSite.create`).

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
from urllib.parse import quote_plus

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import SecretStr

from scadbuddy.profile_fetch.duplicates import likely_duplicates
from scadbuddy.profile_fetch.steps import ExportedFile, SiteError
from scadbuddy.profile_fetch.wire import (
    MAX_CANDIDATES,
    MAX_DUPLICATE_TEXT,
    MAX_FILE_BYTES,
    MAX_HEXES,
    MAX_TEXT,
    SLICER,
    Candidate,
    CreateAnswer,
    CreateAsk,
    LikelyDuplicate,
    SearchAnswer,
    SearchAsk,
    ranked,
    search_query,
    weight_from_name,
)

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
#: How long `ensure_in_my_spools` waits for the My Spools page's "Export slicer profile"
#: before it counts the filament as having no record (the old fixed settle, #7228).
RECORD_WAIT_MS: float = 5_000
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


#: #7025: how long the add form may take to say `DEFAULTS_FOUND` once the type is chosen.
DEFAULTS_WAIT_MS: float = 5_000
#: How long "Save Filament" may take to open the new filament's page.
SAVE_WAIT_MS: float = 20_000
#: What the add form shows when the brand, material and type have site defaults (measured
#: by the script on #7025, 2026-11-22).
DEFAULTS_FOUND: Final = "Defaults found"
#: The heading of the add form's duplicate list (the same script).
AVOID_DUPLICATES: Final = "Avoid creating duplicates"
#: The add form's save button (the same script).
SAVE_FILAMENT: Final = "Save Filament"
SAVED_URL: Final = re.compile(r"/filament/details/(\d+)")
#: The label of the add form's field for each identifier key but ``mpn`` (R-7025-6): the
#: fields were not measured, so a key goes only to the one field whose label names it.
IDENTIFIER_LABELS: Final[Mapping[str, re.Pattern[str]]] = {
    "gtin": re.compile(r"\b(GTIN|UPC|EAN|barcode)\b", re.IGNORECASE),
    "asin": re.compile(r"\bASIN\b", re.IGNORECASE),
    "manufacturer_url": re.compile(
        r"\b(manufacturer|mfr|product)\b.*\b(url|link|site|page)\b", re.IGNORECASE
    ),
    "empty_spool_weight_g": re.compile(r"\bempty spool\b|\bspool weight\b", re.IGNORECASE),
    "td": re.compile(r"^\s*TD\b|transmission distance", re.IGNORECASE),
}
#: The results table's rows (`3dfp.mjs search`'s page, its layout measured 2026-11-22: a
#: header row of Brand, Material, Type, Color, RGB… and one row per filament whose Color
#: cell links ``/filament/details/<id>``). Cells are found by their header, since the page's
#: "Columns" control can hide some.
SEARCH_READ: Final = """() => {
  const table = document.querySelector('main table') || document.querySelector('table');
  if (!table) return [];
  const heads = [...table.querySelectorAll('th')].map(th => th.innerText.trim().toLowerCase());
  const at = name => heads.indexOf(name);
  const rows = [];
  for (const tr of table.querySelectorAll('tr')) {
    const link = tr.querySelector('a[href^="/filament/details/"]');
    if (!link) continue;
    const cells = tr.querySelectorAll('td');
    const cell = name => (at(name) >= 0 && cells[at(name)]) ? cells[at(name)] : null;
    const text = name => cell(name) ? cell(name).innerText.trim() : null;
    const rgb = cell('rgb');
    rows.push({
      id: link.getAttribute('href').split('/').pop(),
      brand: text('brand'), material: text('material'), type: text('type'),
      name: link.innerText.trim(),
      hexes: rgb ? (rgb.innerHTML.match(/#[0-9a-fA-F]{6}\\b/g) || []) : [],
    });
    if (rows.length >= __MAX__) break;
  }
  return rows;
}""".replace("__MAX__", str(MAX_CANDIDATES))
#: The add form's duplicate list: the text after `AVOID_DUPLICATES` in ``main`` (as the
#: script on #7025 read it), and a card per ``/filament/details/<id>`` link after it, read
#: as a search page's card is: its breadcrumb (``/filaments/<brand>/<material>/<type>``),
#: its name and its colours. The text ends at the `SAVE_FILAMENT` button when it is within
#: it, so the form's own labels after the list are not read as the list (R-7025-13).
DUPLICATES_READ: Final = (
    """() => {
  const main = document.querySelector('main') || document.body;
  const all = main.innerText || '';
  const at = all.indexOf('__HEAD__');
  if (at < 0) return { text: '', cards: [] };
  let text = all.slice(at, at + __TEXT__);
  const save = text.indexOf('__SAVE__');
  if (save > 0) text = text.slice(0, save);
  const head = [...main.querySelectorAll('*')].find(
    e => e.children.length === 0 && e.textContent.includes('__HEAD__'));
  const cards = [];
  const seen = new Set();
  const idOf = a => a.getAttribute('href').split('/').pop();
  for (const link of main.querySelectorAll('a[href^="/filament/details/"]')) {
    if (head && !(head.compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
    const id = idOf(link);
    if (seen.has(id)) continue;
    seen.add(id);
    const ids = e => new Set([...e.querySelectorAll('a[href^="/filament/details/"]')].map(idOf));
    let card = link;
    const up = () => card.parentElement && card.parentElement !== main;
    while (up() && ids(card.parentElement).size === 1) card = card.parentElement;
    const crumbs = {};
    for (const a of card.querySelectorAll('a[href^="/filaments/"]')) {
      const depth = a.getAttribute('href').split('?')[0].split('/').filter(Boolean).length;
      crumbs[depth] = a.innerText.trim();
    }
    const hexes = [];
    for (const e of card.querySelectorAll('[style], span')) {
      const own = e.children.length ? '' : e.textContent;
      const source = (e.getAttribute('style') || '') + ' ' + own;
      hexes.push(...(source.match(/#[0-9a-fA-F]{6}\\b/g) || []));
    }
    cards.push({ id, brand: crumbs[2] ?? null, material: crumbs[3] ?? null, type: crumbs[4] ?? null,
      name: link.innerText.trim(), hexes });
    if (cards.length >= __MAX__) break;
  }
  return { text, cards };
}""".replace("__HEAD__", AVOID_DUPLICATES)
    .replace("__TEXT__", str(MAX_DUPLICATE_TEXT))
    .replace("__MAX__", str(MAX_CANDIDATES))
    .replace("__SAVE__", SAVE_FILAMENT)
)


def _text(value: object) -> str | None:
    return value.strip()[:MAX_TEXT] if isinstance(value, str) and value.strip() else None


def _candidates(rows: object) -> list[Candidate]:
    """The page's rows as candidates. The page is untrusted: a row of any other shape, or
    with an id that is not a number, is dropped; texts are cut to `MAX_TEXT`, and each
    row keeps its first `MAX_HEXES` distinct hexes."""
    found: list[Candidate] = []
    seen: set[int] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        raw_id, name = row.get("id"), _text(row.get("name"))
        if not isinstance(raw_id, str) or not raw_id.isascii() or not raw_id.isdigit():
            continue
        if name is None or int(raw_id) in seen or int(raw_id) == 0:
            continue
        hexes: list[str] = []
        raw_hexes = row.get("hexes")
        for value in raw_hexes if isinstance(raw_hexes, list) else []:
            if isinstance(value, str) and HEX.fullmatch(value) and value.upper() not in hexes:
                hexes.append(value.upper())
        seen.add(int(raw_id))
        found.append(
            Candidate(
                tdfp_id=int(raw_id),
                brand=_text(row.get("brand")),
                material=_text(row.get("material")),
                material_type=_text(row.get("type")),
                name=name,
                hexes=hexes[:MAX_HEXES],
                nominal_weight_g=weight_from_name(name),
            )
        )
        if len(found) >= MAX_CANDIDATES:
            break
    return found


def _duplicate_list(listed: object) -> tuple[str, list[Candidate]]:
    """`DUPLICATES_READ`'s answer as its text (cut to `MAX_DUPLICATE_TEXT`) and cards."""
    if not isinstance(listed, dict):
        return "", []
    text = listed.get("text")
    return (
        text[:MAX_DUPLICATE_TEXT] if isinstance(text, str) else "",
        _candidates(listed.get("cards")),
    )


HEX: Final = re.compile(r"#[0-9A-Fa-f]{6}")


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
        """`3dfp.mjs add`, once per filament (#7228): when the filament's My Spools page
        offers "Export slicer profile", the owner has a record of it and nothing is added.
        Only otherwise does it click "Add to My Spools" on the filament's page. 3DFP shows that
        button every time, and each click adds one more 1000 g record (measured on
        2026-11-23: one record per fetch)."""
        page = self.page
        try:
            status = await self._signed_in(f"/my/spools/details/{tdfp_id}", settle_ms=0)
            if status != 404 and await self._offers_export():
                logger.info("3DFP filament %d is already in My Spools", tdfp_id)
                return
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

    async def _offers_export(self) -> bool:
        """Whether the open My Spools page shows "Export slicer profile" within
        `RECORD_WAIT_MS`: at once for a filament with a record, after the whole wait for
        one without (a page with no record has no other mark to wait for)."""
        button = self.page.get_by_role("button", name="Export slicer profile").first
        try:
            await button.wait_for(state="attached", timeout=RECORD_WAIT_MS)
        except PlaywrightTimeoutError:
            return False
        return True

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

    async def search(self, ask: SearchAsk) -> SearchAnswer:
        """`3dfp.mjs search`: ``/filaments?q=`` (`wire.search_query`) and the rows of its
        results table, read by their header (`SEARCH_READ`), ranked (`wire.ranked`)."""
        query = search_query(ask)
        try:
            await self._signed_in(f"/filaments?q={quote_plus(query)}", settle_ms=6000)
            rows = await self.page.evaluate(SEARCH_READ)
        except PlaywrightTimeoutError:
            raise SiteError("timeout", "the 3dfilamentprofiles.com search timed out") from None
        except PlaywrightError:
            raise SiteError("browser", "the 3dfilamentprofiles.com search failed") from None
        found = _candidates(rows)
        logger.info("3DFP search found %d filament(s)", len(found))
        return SearchAnswer(query=query, candidates=ranked(ask, found))

    async def create(self, ask: CreateAsk) -> CreateAnswer:
        """Add a filament on /filament/add, as the script on #7025 did (R-7025-4): the brand,
        material and type through each combobox's native ``<select>``, the brand typed into
        its combobox when the select lacks it; `SiteError` ``no_defaults`` when the page does
        not say "Defaults found" for them. Then "Next", the colour name, the hexes (a row
        added per hex), the nominal weight and the identifiers (`_identifiers`). The site's
        duplicate list (`DUPLICATES_READ`) is judged by `duplicates.likely_duplicates`: a
        likely one answers ``duplicate`` and saves nothing. Else "Save Filament", which
        lands on ``/filament/details/<id>``; any failure after that click is
        ``not_created``, never a retried kind, since the filament may exist."""
        page = self.page
        try:
            await self._signed_in("/filament/add", settle_ms=7000)
            await self._choose("brand", ask.brand, typed=True)
            await self._choose("material", ask.material)
            await self._choose("material_type", ask.material_type)
            if not await self._defaults_found():
                raise SiteError(
                    "no_defaults",
                    f"3dfilamentprofiles.com has no defaults for {ask.brand} {ask.material}"
                    f" {ask.material_type}",
                )
            await page.get_by_role("button", name="Next", exact=True).click()
            await self._pause(2500)
            await page.fill("#color", ask.colour)
            for index, hex_colour in enumerate(ask.hexes):
                if index:
                    add_row = page.locator(f"#rgbText-{index - 1}").locator(
                        "xpath=following::button[1]"
                    )
                    await add_row.click()
                    await self._pause(500)
                await page.fill(f"#rgbText-{index}", hex_colour)
            if ask.nominal_weight_g is not None:
                await page.fill("#nominal_weight", str(ask.nominal_weight_g))
            sent, not_sent = await self._identifiers(ask)
            await self._pause(1500)
            listed = await page.evaluate(DUPLICATES_READ)
        except SiteError:
            raise
        except PlaywrightTimeoutError:
            raise SiteError(
                "timeout", "filling 3dfilamentprofiles.com's add form timed out"
            ) from None
        except PlaywrightError:
            raise SiteError("browser", "filling 3dfilamentprofiles.com's add form failed") from None
        text, cards = _duplicate_list(listed)
        likely = [
            LikelyDuplicate(tdfp_id=None if found is None else found.tdfp_id, reason=reason)
            for found, reason in likely_duplicates(ask, cards, text)
        ]
        answer = CreateAnswer(
            status="duplicate" if likely else "created",
            duplicates=cards,
            duplicate_text=text,
            likely=likely,
            sent=sent,
            not_sent=not_sent,
        )
        if likely:
            logger.info("3DFP create stopped on %d likely duplicate(s)", len(likely))
            return answer
        try:
            await page.get_by_role("button", name=SAVE_FILAMENT, exact=True).click()
            await page.wait_for_url(SAVED_URL, wait_until="commit", timeout=SAVE_WAIT_MS)
            found_id = SAVED_URL.search(page.url)
        except PlaywrightError:
            raise SiteError("not_created") from None
        if found_id is None:
            raise SiteError("not_created")
        answer.tdfp_id = int(found_id.group(1))
        logger.info("created 3DFP filament %d", answer.tdfp_id)
        return answer

    async def _choose(self, field: str, label: str, *, typed: bool = False) -> None:
        """Pick ``label`` in the add form's combobox ``#field`` through the native
        ``<select>`` after it (typing into the combobox picked the wrong entry, #7025), its
        option matched exactly, else by case; with ``typed`` (the brand, which the script
        typed), type it into the combobox and click the exact option when the select
        lacks it. `SiteError` ``option_missing`` when the form offers no such entry."""
        page = self.page
        select = page.locator(f"#{field}").locator("xpath=following::select[1]")
        if await select.count():
            offered = [text.strip() for text in await select.locator("option").all_inner_texts()]
            match = next((o for o in offered if o == label), None) or next(
                (o for o in offered if o.casefold() == label.casefold()), None
            )
            if match is not None:
                await select.select_option(label=match, force=True)
                await self._pause(1500)
                return
        if typed:
            await page.click(f"#{field}")
            await self._pause(800)
            await page.keyboard.type(label, delay=30)
            await self._pause(1500)
            option = page.get_by_role("option", name=label, exact=True)
            if await option.count():
                await option.first.click()
                await self._pause(1500)
                return
            await page.keyboard.press("Escape")
        what = field.replace("_", " ")
        raise SiteError("option_missing", f"3dfilamentprofiles.com offers no {what} {label!r}")

    async def _defaults_found(self) -> bool:
        """The page says "Defaults found" within `DEFAULTS_WAIT_MS` (R-7025-4)."""
        try:
            await (
                self.page.locator("main")
                .get_by_text(DEFAULTS_FOUND)
                .first.wait_for(timeout=DEFAULTS_WAIT_MS)
            )
        except PlaywrightTimeoutError:
            return False
        return True

    async def _identifiers(self, ask: CreateAsk) -> tuple[list[str], list[str]]:
        """Fill each identifier the ask has where the form has a field for it: ``mpn`` in
        ``#sku-0`` (measured on #7025), the rest in the one field whose label
        `IDENTIFIER_LABELS` matches (R-7025-6). The keys sent, and those with no field."""
        page = self.page
        sent: list[str] = []
        not_sent: list[str] = []
        for key, value in ask.identifiers().items():
            if key == "mpn":
                field = page.locator("#sku-0")
            else:
                field = page.get_by_label(IDENTIFIER_LABELS[key])
            if await field.count() == 1 and await field.is_editable():
                await field.fill(value)
                sent.append(key)
            else:
                not_sent.append(key)
        return sent, not_sent

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

    async def _signed_in(self, path: str, *, settle_ms: float) -> int | None:
        """`_goto`, signing in again when the site sends the browser to ``/login``; the
        page's HTTP status."""
        status = await self._goto(path, settle_ms=settle_ms)
        if "/login" in self.page.url:
            logger.info("3dfilamentprofiles.com asked for the login again")
            await self.login()
            status = await self._goto(path, settle_ms=settle_ms)
            if "/login" in self.page.url:
                raise SiteError("login_failed")
        return status

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
        """`_signed_in`; a 404 is `SiteError` ``tdfp_not_found``."""
        status = await self._signed_in(path, settle_ms=settle_ms)
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
