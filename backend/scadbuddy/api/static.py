from __future__ import annotations

from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

INDEX_NAME = "index.html"

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


class SPAStaticFiles(StaticFiles):
    """Serve the built bundle, falling back to ``index.html`` for client-side routes."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory=directory, html=True)
        self.index = directory / INDEX_NAME

    async def get_response(self, path: str, scope: Scope) -> Response:
        # A missing file is *raised* as a 404, not returned, so both shapes are handled.
        try:
            response = await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404 or not self.index.is_file():
                raise
            response = FileResponse(self.index)
        if response.status_code == 404 and self.index.is_file():
            response = FileResponse(self.index)
        response.headers["Content-Security-Policy"] = PAGE_CSP
        return response
