from __future__ import annotations

from pathlib import Path, PurePosixPath

from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

INDEX_NAME = "index.html"
ASSETS_DIR = "assets"

#: Vite content-hashes every file under ``assets/``, so a URL there never changes meaning.
IMMUTABLE = "public, max-age=31536000, immutable"
#: Everything else, ``index.html`` above all, is revalidated on every load. Without it a
#: browser caches ``index.html`` heuristically and, after a deploy, keeps asking for
#: chunks that no longer exist (#395).
REVALIDATE = "no-cache"


def _is_asset(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and parts[0] == ASSETS_DIR


class SPAStaticFiles(StaticFiles):
    """Serve the built bundle, falling back to ``index.html`` for client-side routes.

    A miss under ``assets/`` is a real 404, never the fallback: a stale page asking for a
    chunk from an older build must fail as a missing script, not receive HTML (#395).
    """

    def __init__(self, directory: Path) -> None:
        super().__init__(directory=directory, html=True)
        self.index = directory / INDEX_NAME

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await self._response_or_fallback(path, scope)
        ok = response.status_code in (200, 304)
        response.headers["Cache-Control"] = IMMUTABLE if ok and _is_asset(path) else REVALIDATE
        return response

    async def _response_or_fallback(self, path: str, scope: Scope) -> Response:
        fallback = self.index.is_file() and not _is_asset(path)
        # A missing file is *raised* as a 404, not returned, so both shapes are handled.
        try:
            response = await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404 or not fallback:
                raise
            return FileResponse(self.index)
        if response.status_code == 404 and fallback:
            return FileResponse(self.index)
        return response
