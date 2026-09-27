"""Uploads for `// file` parameters (#204): an SVG or PNG attached to one render."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, File, Path, UploadFile, status
from fastapi.responses import FileResponse

from scadbuddy.api.deps import CatalogueDep, PathsDep, SlugPath
from scadbuddy.api.models import require_model_exists
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.assets import (
    ASSET_ID_PATTERN,
    MAX_ASSET_BYTES,
    MEDIA_TYPES,
    AssetMeta,
    AssetNotFoundError,
    AssetRejectedError,
    AssetStore,
)

router = APIRouter(tags=["assets"])

AssetIdPath = Annotated[str, Path(pattern=ASSET_ID_PATTERN)]

#: Served back for the customizer's preview. The SVG is already sanitised; these
#: make a browser that opens the URL directly treat it as an inert image anyway.
CONTENT_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "public, max-age=31536000, immutable",
}


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
