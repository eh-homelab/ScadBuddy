"""Files for `// file` parameters (#204): an SVG or PNG attached to one render, and
the sample files a template ships beside its source."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, File, Path, UploadFile, status
from fastapi.responses import FileResponse

from scadbuddy.api.deps import CatalogueDep, HistoryDep, PathsDep, SlugPath
from scadbuddy.api.models import require_model_exists
from scadbuddy.api.versions import CommitQuery, require_history
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.assets import (
    ASSET_ID_PATTERN,
    MAX_ASSET_BYTES,
    MEDIA_TYPES,
    AssetKind,
    AssetMeta,
    AssetNotFoundError,
    AssetRejectedError,
    AssetStore,
    sample_files,
)
from scadbuddy.library.history import GitError, RevisionNotFoundError
from scadbuddy.render.jobs import resolve_source

router = APIRouter(tags=["assets"])

AssetIdPath = Annotated[str, Path(pattern=ASSET_ID_PATTERN)]
#: A sample's bare file name, as `sample_files` lists it. The pattern is a first
#: fence; membership in that list is what actually decides.
SampleNamePath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$")]

#: Served back for the customizer's preview. The SVG is already sanitised; these
#: make a browser that opens the URL directly treat it as an inert image anyway.
CONTENT_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "public, max-age=31536000, immutable",
}


#: A sample lives in the template's directory, which an edit can change under the
#: same URL, so it is revalidated rather than kept.
SAMPLE_HEADERS = {**CONTENT_HEADERS, "Cache-Control": "no-cache"}


def _store(paths: DataPaths) -> AssetStore:
    return AssetStore(paths.assets)


def _require_asset(store: AssetStore, asset_id: str) -> AssetMeta:
    try:
        return store.get(asset_id)
    except AssetNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no uploaded file {asset_id!r}") from None


@router.post(
    "/models/{slug}/assets",
    response_model=AssetMeta,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file for a file parameter",
    description=(
        "Stores an SVG or PNG for a `// file` parameter and answers its id, the "
        "SHA-256 of the stored bytes, which is the value the render takes. The "
        "content is sniffed, not trusted by its name: an SVG is stripped of scripts "
        "and external references, a PNG is re-encoded and downscaled to at most "
        "256 px on its long side. Anything else is a 422."
    ),
)
async def upload_asset(
    slug: SlugPath,
    catalogue: CatalogueDep,
    paths: PathsDep,
    file: Annotated[UploadFile, File(description="An SVG or PNG")],
) -> AssetMeta:
    require_model_exists(catalogue, slug)
    data = await file.read(MAX_ASSET_BYTES + 1)
    if len(data) > MAX_ASSET_BYTES:
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"the file is larger than {MAX_ASSET_BYTES} bytes",
        )
    try:
        # Decoding a PNG and parsing an SVG are CPU work; keep them off the loop.
        return await asyncio.to_thread(_store(paths).put, data, file.filename)
    except AssetRejectedError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None


@router.get(
    "/models/{slug}/assets/{asset_id}",
    response_model=AssetMeta,
    summary="An uploaded file's metadata",
)
def get_asset(
    slug: SlugPath, asset_id: AssetIdPath, catalogue: CatalogueDep, paths: PathsDep
) -> AssetMeta:
    require_model_exists(catalogue, slug)
    return _require_asset(_store(paths), asset_id)


@router.get(
    "/models/{slug}/assets/{asset_id}/content",
    response_class=FileResponse,
    responses={200: {"content": {"image/svg+xml": {}, "image/png": {}}}},
    summary="An uploaded file's bytes",
)
def get_asset_content(
    slug: SlugPath, asset_id: AssetIdPath, catalogue: CatalogueDep, paths: PathsDep
) -> FileResponse:
    require_model_exists(catalogue, slug)
    store = _store(paths)
    meta = _require_asset(store, asset_id)
    return FileResponse(
        store.blob_path(meta), media_type=MEDIA_TYPES[meta.kind], headers=CONTENT_HEADERS
    )


@router.get(
    "/models/{slug}/samples/{name}",
    response_class=FileResponse,
    responses={200: {"content": {"image/svg+xml": {}, "image/png": {}}}},
    summary="A sample file the template ships",
    description=(
        "Serves one of the files a `file` parameter's `samples` lists: an SVG or PNG "
        "directly in the template's directory, by its bare name. `version` reads the "
        "template as it was at that revision. Any other name is a 404."
    ),
)
async def get_sample_content(
    slug: SlugPath,
    name: SampleNamePath,
    catalogue: CatalogueDep,
    history: HistoryDep,
    paths: PathsDep,
    version: CommitQuery = None,
) -> FileResponse:
    require_model_exists(catalogue, slug)
    requested: str | None = None
    try:
        if version is not None:
            requested = await asyncio.to_thread(require_history(history).resolve, version)
        source = await resolve_source(slug, requested, paths=paths, history=history)
    except RevisionNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no revision {version!r}") from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    directory = source.scad.parent
    # Only a listed sample: a bare name, a regular file (never a symlink), of a kind
    # a file parameter takes. Nothing else in the directory is reachable here.
    if name not in await asyncio.to_thread(sample_files, directory):
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} ships no sample {name!r}")
    kind: AssetKind = "svg" if name.lower().endswith(".svg") else "png"
    return FileResponse(directory / name, media_type=MEDIA_TYPES[kind], headers=SAMPLE_HEADERS)
