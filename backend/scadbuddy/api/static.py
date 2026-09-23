from __future__ import annotations

from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

INDEX_NAME = "index.html"


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
            return FileResponse(self.index)
        if response.status_code == 404 and self.index.is_file():
            return FileResponse(self.index)
        return response
