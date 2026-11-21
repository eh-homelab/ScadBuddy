"""The steps of 3dfilamentprofiles.com, apart from the browser that runs them (#6780).

`site.ThreeDfpSite` runs them in the browser service; `remote.RemoteSite` asks the service
for them from the worker. This module imports no browser driver, so the worker, which holds
the ``profile-fetch`` CRUD credential, never loads Playwright (claude-review on #6808).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Literal, Protocol

Kind = Literal[
    "login_failed",
    "tdfp_not_found",
    "bot_check",
    "option_missing",
    "file_too_large",
    "timeout",
    "browser",
]

_MESSAGES: Final[dict[str, str]] = {
    "login_failed": "3dfilamentprofiles.com refused the login",
    "tdfp_not_found": "3dfilamentprofiles.com has no such filament",
    "bot_check": "3dfilamentprofiles.com's bot check did not clear",
    "option_missing": "the export dialog lacks an option",
    "file_too_large": "the exported file is larger than 512 KiB",
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


class Site(Protocol):
    """`ThreeDfpSite`'s surface: an async context manager that signs in and exports. The
    worker's `remote.RemoteSite` has it too, over the browser service's RPC (#6780)."""

    async def __aenter__(self) -> Any: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def login(self) -> None: ...

    async def ensure_in_my_spools(self, tdfp_id: int) -> None: ...

    async def export(self, tdfp_id: int, printer_model: str, nozzle: str) -> ExportedFile: ...


class SiteHolder:
    """One signed-in browser for the process's life (one export at a time, Decision 16).
    `get` opens it and signs in when there is none; `drop` closes it, so the next `get`
    starts afresh (after a crash or a timeout, `activities.FRESH_BROWSER_KINDS`). The
    browser service holds one over `ThreeDfpSite`, and the worker one over
    `remote.RemoteSite` (#6780)."""

    def __init__(self, open_site: Callable[[], Site]) -> None:
        self._open = open_site
        self._site: Site | None = None

    async def get(self) -> Site:
        if self._site is None:
            site = self._open()
            await site.__aenter__()
            try:
                await site.login()
            except BaseException:
                with suppress(Exception):
                    await site.__aexit__(None, None, None)
                raise
            self._site = site
        return self._site

    async def drop(self) -> None:
        site, self._site = self._site, None
        if site is not None:
            with suppress(Exception):
                await site.__aexit__(None, None, None)

    async def aclose(self) -> None:
        await self.drop()
