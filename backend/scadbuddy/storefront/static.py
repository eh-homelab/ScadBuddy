"""The storefront page's files (#5282, spec 2026-11-02 §5.1): the second Vite entry,
``storefront.html``, and only the chunks it loads.

The image's bundle holds both entries. The edge serves ``storefront.html`` for ``/`` and
every client route (a path with no file extension), and an ``/assets/<file>`` only when
that file is in the ``storefront.html`` entry's import closure in Vite's
``.vite/manifest.json`` (its ``file``, ``css``, ``assets``, ``imports`` and
``dynamicImports``, followed recursively). Every other path is 404: the back office's
``index.html`` and its own chunks, the manifest, ``public/`` files, and any ``/api/``
path no route took. The closure is read once, when the edge starts.

It never imports database code: the edge's process holds none (`test_edge.py`).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any, Final

from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.types import Receive, Scope, Send

from scadbuddy.core.problems import problem_response

ENTRY: Final = "storefront.html"
MANIFEST: Final = Path(".vite") / "manifest.json"
ASSETS_DIR: Final = "assets"
#: Vite content-hashes every file under ``assets/``.
IMMUTABLE: Final = "public, max-age=31536000, immutable"
#: The page itself is revalidated on every load, so a deploy's new chunks are found.
REVALIDATE: Final = "no-cache"
#: The page's policy. Script, style, fetch and the 3D viewer's preview come from this
#: origin only. A listing's media and a cart's files are presigned GETs on the shop's
#: store (``s3``), whose host the edge does not know, so images and media may come over
#: any https origin. No inline script: ``storefront.html`` has none.
STOREFRONT_CSP: Final = (
    "default-src 'self'; script-src 'self'; connect-src 'self'; "
    "img-src 'self' data: blob: https:; media-src 'self' blob: https:; "
    "font-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "worker-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)
#: Cloudflare Turnstile's origin (#5283). Its CSP page asks for it in ``script-src`` and
#: ``frame-src``, and for ``connect-src 'self'`` only in pre-clearance mode, which the
#: storefront does not use
#: (https://developers.cloudflare.com/turnstile/reference/content-security-policy/, read
#: 2026-11-09). The page is one app and a CSP binds at a full load, so every page names it
#: while a site key is set, and until the edge's first site-key read succeeds
#: (`turnstile.py` `SiteKey.ever_read`), and none does while the check is known off.
TURNSTILE_ORIGIN: Final = "https://challenges.cloudflare.com"
TURNSTILE_CSP: Final = STOREFRONT_CSP.replace(
    "script-src 'self';", f"script-src 'self' {TURNSTILE_ORIGIN}; frame-src {TURNSTILE_ORIGIN};"
)
#: PayPal's SDK origins, live and sandbox, for the ``/pay`` page only (#5283 phase 4): the
#: SDK script loads from them, its buttons are frames of them, and the SDK calls them. Added
#: on top of the shared policy (Turnstile's origins stay while a site key is set), so no
#: other path may reach PayPal
#: (https://developer.paypal.com/sdk/js/configuration/, read 2026-11-10).
PAYPAL_ORIGINS: Final = "https://www.paypal.com https://www.sandbox.paypal.com"
PAY_PATH: Final = "/pay"


def storefront_csp(turnstile: bool, pay: bool = False) -> str:
    """The page's policy: `TURNSTILE_CSP` while the check is on, or until the edge's first
    site-key read succeeds (`SiteKey.ever_read`), else the first-party `STOREFRONT_CSP`;
    with ``pay``, PayPal's origins (`PAYPAL_ORIGINS`) on top, in ``script-src``,
    ``frame-src`` and ``connect-src``."""
    csp = TURNSTILE_CSP if turnstile else STOREFRONT_CSP
    if not pay:
        return csp
    csp = csp.replace("script-src 'self'", f"script-src 'self' {PAYPAL_ORIGINS}", 1)
    csp = csp.replace("connect-src 'self'", f"connect-src 'self' {PAYPAL_ORIGINS}", 1)
    if "frame-src " in csp:
        return csp.replace("frame-src ", f"frame-src {PAYPAL_ORIGINS} ", 1)
    return csp.replace("script-src ", f"frame-src {PAYPAL_ORIGINS}; script-src ", 1)


async def _turnstile_off() -> bool:
    return False


def _closure(manifest: dict[str, Any], key: str) -> set[str]:
    """Every file under ``assets/`` that the manifest entry ``key`` loads, by name."""
    seen: set[str] = set()
    files: set[str] = set()
    pending = [key]
    while pending:
        current = pending.pop()
        if current in seen or current not in manifest:
            continue
        seen.add(current)
        chunk = manifest[current]
        named: Iterable[str] = [
            chunk.get("file", ""),
            *chunk.get("css", []),
            *chunk.get("assets", []),
        ]
        for path in named:
            parts = PurePosixPath(path).parts
            if len(parts) == 2 and parts[0] == ASSETS_DIR:
                files.add(parts[1])
        pending.extend(chunk.get("imports", []))
        pending.extend(chunk.get("dynamicImports", []))
    return files


def _not_found() -> Response:
    return PlainTextResponse("Not Found", 404, headers={"Cache-Control": REVALIDATE})


class StorefrontStatic:
    """An ASGI app for the bundle in ``dist``; mounted at ``/``, after every route.
    Raises FileNotFoundError when ``dist`` has no ``storefront.html`` or manifest."""

    def __init__(
        self, dist: Path, *, turnstile: Callable[[], Awaitable[bool]] = _turnstile_off
    ) -> None:
        self._turnstile = turnstile
        self.page = dist / ENTRY
        if not self.page.is_file():
            raise FileNotFoundError(self.page)
        manifest = json.loads((dist / MANIFEST).read_text("utf-8"))
        self.assets = dist / ASSETS_DIR
        self.allowed = frozenset(_closure(manifest, ENTRY))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close"})
            return
        response = self._response(scope)
        if response is None:
            path: str = scope["path"]
            root: str = scope.get("root_path", "")
            if root and path.startswith(root):
                path = path[len(root) :]
            response = self._page(await self._turnstile(), pay=path.rstrip("/") == PAY_PATH)
        await response(scope, receive, send)

    def _response(self, scope: Scope) -> Response | None:
        """The answer, or None for the page itself (`_page`), whose CSP waits on
        whether the bot check is on."""
        path: str = scope["path"]
        root: str = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :]
        parts = PurePosixPath(path).parts[1:]  # without the leading "/"
        # Before the method check: Starlette gives this mount a request whose path an
        # edge route matches but whose method it does not.
        if parts and parts[0] == "api":
            return problem_response(Request(scope), 404, "No such resource.")
        if scope["method"] not in ("GET", "HEAD"):
            return PlainTextResponse("Method Not Allowed", 405, headers={"Allow": "GET, HEAD"})
        if not parts:
            return None
        if parts[0] == ASSETS_DIR:
            if len(parts) == 2 and parts[1] in self.allowed:
                response = FileResponse(self.assets / parts[1])
                response.headers["Cache-Control"] = IMMUTABLE
                return response
            return _not_found()
        # A client route never has a file extension, a dot file, or `..`.
        if any(part.startswith(".") or "." in part for part in parts):
            return _not_found()
        return None

    def _page(self, turnstile: bool, pay: bool = False) -> Response:
        response = FileResponse(self.page, media_type="text/html")
        response.headers["Cache-Control"] = REVALIDATE
        response.headers["Content-Security-Policy"] = storefront_csp(turnstile, pay)
        return response
