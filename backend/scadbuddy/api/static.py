from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import NoReturn

from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Match, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

INDEX_NAME = "index.html"
ASSETS_DIR = "assets"
#: Every API route lives under ``/api/``. One that reached this mount matched no route,
#: so it is a 404 problem document, never the SPA's HTML or a 405 (#365).
API_DIR = "api"

#: Vite content-hashes every file under ``assets/``, so a URL there never changes meaning.
#: Nothing else may land there: ``frontend/public/assets/<name>`` would be copied to the
#: same path unhashed and cached for a year. Put un-hashed files at the root of ``public/``.
IMMUTABLE = "public, max-age=31536000, immutable"
#: Everything else, ``index.html`` above all, is revalidated on every load. Without it a
#: browser caches ``index.html`` heuristically and, after a deploy, keeps asking for
#: chunks that no longer exist (#395).
REVALIDATE = "no-cache"


def _is_asset(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and parts[0] == ASSETS_DIR


def _is_api(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and parts[0] == API_DIR


#: The app document's policy (spec 2026-09-27 §9). A template UI runs unsandboxed in
#: this page. The policy stops it loading script from anywhere but this origin, and stops
#: fetch/XHR/WebSocket and image, media and font beacons to other hosts. Google Fonts is
#: the one exception, for style and font files only (the font picker's previews,
#: `frontend/src/lib/fonts.ts` `googleFontsCssUrl`). The policy does NOT stop same-origin
#: abuse: a module can call every /api/v1 route with the user's session. Nor does it stop
#: exfiltration by top-level navigation, window.open or WebRTC. No frame-ancestors:
#: Bambuddy's origin, which frames this page, is not known here.
PAGE_CSP = (
    "default-src 'self'; script-src 'self'; connect-src 'self'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "worker-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'"
)


def _missing_asset() -> Response:
    """Returned, not raised: the app's problem handler would answer it without
    get_response's Cache-Control. Plain text and no CSP, never a 404.html: not a document,
    so nothing runs in it."""
    return PlainTextResponse("Not Found", 404)


def _refuse_api(path: str, scope: Scope) -> NoReturn:
    """An ``/api/`` request no route took. The mount at ``/`` matches every path, so the
    router hands it a request for a real route with the wrong method too: that one is a
    405 naming the methods the route takes, everything else a 404."""
    probe: Scope = {"type": "http", "path": f"/{path}", "root_path": "", "method": scope["method"]}
    allowed: set[str] = set()
    for route in scope["app"].routes:
        if isinstance(route, Route) and route.matches(probe)[0] is Match.PARTIAL:
            allowed |= route.methods or set()
    if allowed:
        raise HTTPException(405, headers={"Allow": ", ".join(sorted(allowed))})
    raise HTTPException(404, f"no API route matches {scope['method']} /{path}")


class SPAStaticFiles(StaticFiles):
    """Serve the built bundle, falling back to ``index.html`` for client-side routes.

    A miss under ``assets/`` is a real 404, never the fallback: a stale page asking for a
    chunk from an older build must fail as a missing script, not receive HTML (#395).
    """

    def __init__(self, directory: Path) -> None:
        super().__init__(directory=directory, html=True)
        self.index = directory / INDEX_NAME

    async def get_response(self, path: str, scope: Scope) -> Response:
        if _is_api(path):
            _refuse_api(path, scope)
        response = await self._response_or_fallback(path, scope)
        ok = response.status_code in (200, 304)
        response.headers["Cache-Control"] = IMMUTABLE if ok and _is_asset(path) else REVALIDATE
        return response

    async def _response_or_fallback(self, path: str, scope: Scope) -> Response:
        asset = _is_asset(path)
        fallback = self.index.is_file() and not asset
        # A missing file is usually *raised* as a 404, but returned when a 404.html exists,
        # so both shapes are handled.
        try:
            response = await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404 or not (asset or fallback):
                raise
            if asset:
                return _missing_asset()
            response = FileResponse(self.index)
        if response.status_code == 404:
            if asset:
                return _missing_asset()
            if fallback:
                response = FileResponse(self.index)
        response.headers["Content-Security-Policy"] = PAGE_CSP
        return response
