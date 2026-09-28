"""A template's own UI files (spec 2026-09-27 §4.1, §9): served from its `ui/`, live or
at a pinned revision, so a module's relative imports stay inside one revision."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, Header, Path, status
from fastapi.responses import Response

from scadbuddy.api.deps import (
    CatalogueDep,
    CommitPath,
    FetcherDep,
    HistoryDep,
    PathsDep,
    SlugPath,
)
from scadbuddy.api.jobs import _resolve_version
from scadbuddy.api.models import _etag_matches, require_model_exists
from scadbuddy.core.problems import ApiError
from scadbuddy.render.jobs import resolve_source

router = APIRouter(tags=["models"])

UI_DIR = "ui"
#: What a template UI may ship. Anything else, HTML included, is not served: a
#: document from the app's origin would run with the whole app's reach.
UI_MEDIA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
}
#: `sandbox` makes a file opened directly (an SVG, say) an inert document; it does
#: not apply to the module the page imports, which runs under the page's own CSP.
UI_FILE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cross-Origin-Resource-Policy": "same-origin",
}
LIVE_CACHE_CONTROL = "no-cache"
#: A commit's files never change, and the URL names the commit.
PINNED_CACHE_CONTROL = "public, max-age=31536000, immutable"

#: Not a pattern: a path that fails one would be a 422, and every path that is not a
#: servable file under ui/ is the same 404, whatever shape it has.
UiPath = Annotated[
    str, Path(max_length=300, description="A file under the template's ui/ directory")
]


def _ui_file(directory: FsPath, path: str) -> FsPath:
    base = (directory / UI_DIR).resolve()
    target = (base / path).resolve()
    if (
        any(not part or part.startswith(".") for part in path.split("/"))
        # Resolved, so a symlink out of ui/ is caught as well as a `..`.
        or not target.is_relative_to(base)
        or target.suffix.lower() not in UI_MEDIA_TYPES
        or not target.is_file()
    ):
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no ui file {path!r}")
    return target


def _serve(file: FsPath, *, pinned: bool, if_none_match: str | None) -> Response:
    body = file.read_bytes()
    etag = f'"{hashlib.sha256(body).hexdigest()}"'
    headers = {
        **UI_FILE_HEADERS,
        "ETag": etag,
        "Cache-Control": PINNED_CACHE_CONTROL if pinned else LIVE_CACHE_CONTROL,
    }
    if _etag_matches(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(body, media_type=UI_MEDIA_TYPES[file.suffix.lower()], headers=headers)


@router.get(
    "/models/{slug}/ui/{path:path}",
    response_class=Response,
    responses={200: {"content": {"text/javascript": {}}}, 304: {"description": "Unchanged"}},
    summary="Template UI file",
)
async def get_ui_file(
    slug: SlugPath,
    path: UiPath,
    catalogue: CatalogueDep,
    paths: PathsDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    require_model_exists(catalogue, slug)
    # The live directory itself: no revision to resolve, no library checkout to fetch.
    file = await asyncio.to_thread(_ui_file, paths.model_dir(slug), path)
    return await asyncio.to_thread(_serve, file, pinned=False, if_none_match=if_none_match)


@router.get(
    "/models/{slug}/versions/{commit}/ui/{path:path}",
    response_class=Response,
    responses={200: {"content": {"text/javascript": {}}}, 304: {"description": "Unchanged"}},
    summary="Template UI file at a revision",
)
async def get_ui_file_at(
    slug: SlugPath,
    commit: CommitPath,
    path: UiPath,
    catalogue: CatalogueDep,
    history: HistoryDep,
    paths: PathsDep,
    fetcher: FetcherDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    require_model_exists(catalogue, slug)
    requested = await _resolve_version(history, slug, commit)
    source = await resolve_source(slug, requested, paths=paths, history=history, fetcher=fetcher)
    file = await asyncio.to_thread(_ui_file, source.scad.parent, path)
    # `resolve_source` answers the current revision with the live directory, which an
    # uncommitted edit can change; only an export is immutable.
    exported = source.scad.parent != paths.model_dir(slug)
    return await asyncio.to_thread(_serve, file, pinned=exported, if_none_match=if_none_match)
