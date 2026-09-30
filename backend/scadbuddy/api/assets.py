"""Files for `// file` parameters (#204): an SVG or PNG attached to one render, and
the sample files a template ships beside its source."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, FastAPI, File, Path, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse

from scadbuddy.api.deps import (
    DATABASE_REQUIRED_PROBLEM,
    AssetsDep,
    CatalogueDep,
    FetcherDep,
    HistoryDep,
    PathsDep,
    SlugPath,
    StateDep,
)
from scadbuddy.api.models import require_model_exists
from scadbuddy.api.versions import CommitQuery, require_history
from scadbuddy.core.problems import ApiError, problem_response
from scadbuddy.library.assets import (
    ASSET_ID_PATTERN,
    MAX_ASSET_BYTES,
    MEDIA_TYPES,
    AssetKind,
    AssetMeta,
    AssetNotFoundError,
    AssetQuotaError,
    AssetRejectedError,
    AssetStore,
    AssetStoreUnavailableError,
    AssetUsage,
    sample_files,
)
from scadbuddy.library.history import GitError, RevisionNotFoundError
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.schema import BARE_FILENAME_PATTERN
from scadbuddy.store.content import StoreFullError, template_title

router = APIRouter(tags=["assets"])

AssetIdPath = Annotated[str, Path(pattern=ASSET_ID_PATTERN)]
#: A sample's bare file name, as `sample_files` lists it. The pattern is a first
#: fence; membership in that list is what actually decides.
SampleNamePath = Annotated[str, Path(pattern=BARE_FILENAME_PATTERN)]

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


def install_asset_handlers(app: FastAPI) -> None:
    """The upload store without its database is a 503 naming what is missing, from
    every route that reaches it (an upload, a render or preset naming one, the usage),
    as the analyzer decisions' routes answer -- not the 500 an uncaught one would be.
    The server does not start without a database (#401); this is the answer should
    a store ever be built without one (#591)."""

    @app.exception_handler(AssetStoreUnavailableError)
    async def _unavailable(request: Request, exc: AssetStoreUnavailableError) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            str(exc),
            type_=DATABASE_REQUIRED_PROBLEM,
        )


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
        "256 px on its long side. Anything else is a 422. A file not already stored "
        "that would take the store past SCADBUDDY_ASSET_MAX_COUNT files or "
        "SCADBUDDY_ASSET_MAX_TOTAL_BYTES bytes is a 413 whose problem document carries "
        "the store's `usage`; re-uploading stored content is never refused."
    ),
)
async def upload_asset(
    slug: SlugPath,
    catalogue: CatalogueDep,
    assets: AssetsDep,
    state: StateDep,
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
        meta = await asyncio.to_thread(assets.put, data, file.filename)
    except AssetRejectedError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except AssetQuotaError as error:
        state.metrics.assets_rejected.inc()
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE, str(error), usage=error.usage.model_dump()
        ) from None
    # A failed mirror (Bambuddy unreachable) fails the upload with Bambuddy's problem:
    # a file a worker could not read must not look uploaded.
    if state.store.remote_assets is not None:
        title = await asyncio.to_thread(template_title, state.paths.model_source(slug).parent, slug)
        try:
            await state.store.remote_assets.mirror(assets, meta, slug=slug, title=title)
        except StoreFullError as error:
            raise ApiError(
                status.HTTP_507_INSUFFICIENT_STORAGE,
                f"the blob store has no room for this file: {error}",
            ) from None
    return meta


@router.get(
    "/assets/usage",
    response_model=AssetUsage,
    summary="How much the upload store holds",
    description=(
        "The files stored for `// file` parameters: how many, their total bytes, and "
        "the caps an upload is refused past (0 is no limit). Files that no output, "
        "preset or render job references are removed by a sweep once nothing has "
        "uploaded or used them for SCADBUDDY_ASSET_SWEEP_GRACE."
    ),
)
async def get_asset_usage(assets: AssetsDep) -> AssetUsage:
    return await asyncio.to_thread(assets.usage)


@router.get(
    "/models/{slug}/assets/{asset_id}",
    response_model=AssetMeta,
    summary="An uploaded file's metadata",
)
def get_asset(
    slug: SlugPath, asset_id: AssetIdPath, catalogue: CatalogueDep, assets: AssetsDep
) -> AssetMeta:
    require_model_exists(catalogue, slug)
    return _require_asset(assets, asset_id)


@router.get(
    "/models/{slug}/assets/{asset_id}/content",
    response_class=FileResponse,
    responses={200: {"content": {"image/svg+xml": {}, "image/png": {}}}},
    summary="An uploaded file's bytes",
)
def get_asset_content(
    slug: SlugPath, asset_id: AssetIdPath, catalogue: CatalogueDep, assets: AssetsDep
) -> FileResponse:
    require_model_exists(catalogue, slug)
    meta = _require_asset(assets, asset_id)
    return FileResponse(
        assets.blob_path(meta), media_type=MEDIA_TYPES[meta.kind], headers=CONTENT_HEADERS
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
    fetcher: FetcherDep,
    version: CommitQuery = None,
) -> FileResponse:
    require_model_exists(catalogue, slug)
    requested: str | None = None
    try:
        if version is not None:
            requested = await asyncio.to_thread(require_history(history).resolve, version)
        source = await resolve_source(
            slug, requested, paths=paths, history=history, fetcher=fetcher
        )
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
