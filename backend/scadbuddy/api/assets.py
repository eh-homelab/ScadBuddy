"""Files for `// file` parameters (#204): an SVG or PNG attached to one render, and
the sample files a template ships beside its source."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, File, Header, Path, Query, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from scadbuddy.api.deps import (
    IMPORT_CONCURRENCY,
    AppState,
    AssetsDep,
    CatalogueDep,
    FetcherDep,
    HistoryDep,
    ImportPermits,
    ImportsDep,
    PathsDep,
    SettingsStoreDep,
    SlugPath,
)
from scadbuddy.api.models import (
    RESOLVER_RETRY_AFTER,
    fetch_busy,
    require_model_exists,
)
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    Claimed,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.api.versions import CommitQuery, require_history
from scadbuddy.core.events import ModelEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.asset_credits import credit_line, known_licence, with_credit
from scadbuddy.library.asset_fetch import ascii_host, fetch_file, parse_fetch_url
from scadbuddy.library.assets import (
    ASSET_ID_PATTERN,
    MAX_ASSET_BYTES,
    MEDIA_TYPES,
    AssetKind,
    AssetLicence,
    AssetMeta,
    AssetNotFoundError,
    AssetQuotaError,
    AssetRejectedError,
    AssetStore,
    AssetUsage,
    sample_files,
)
from scadbuddy.library.catalogue import (
    ModelNotFoundError,
    SidecarNotFoundError,
    StaleVersionError,
)
from scadbuddy.library.fetch_grants import (
    FETCH_GRANT_HEADER,
    FETCH_GRANT_TTL_SECONDS,
    FetchGrantError,
    fetch_grant_key,
)
from scadbuddy.library.fetch_grants import verify as verify_fetch_grant
from scadbuddy.library.history import GitError, RevisionNotFoundError
from scadbuddy.library.icon_search import COLLECTIONS, MAX_LIMIT, IconSearch, search_icons
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.library.url_import import (
    IMPORT_TIMEOUT,
    ImportRefusedError,
    ResolverBusyError,
    shown_url,
)
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.component import OperationsDep
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.schema import BARE_FILENAME_PATTERN
from scadbuddy.store.content import StoreFullError, template_title

logger = logging.getLogger(__name__)

router = APIRouter(tags=["assets"])

AssetIdPath = Annotated[str, Path(pattern=ASSET_ID_PATTERN)]
#: A sample's bare file name, as `sample_files` lists it. The pattern is a first
#: fence; membership in that list is what actually decides.
SampleNamePath = Annotated[str, Path(pattern=BARE_FILENAME_PATTERN)]

#: What makes a browser that opens an image's URL directly treat it as an inert image,
#: an SVG included: no script, no fetch, no sniffing it into a document. Every route
#: serving an image a user or a template supplied sends these (here, and a model's
#: own images in `api/lsp.py`, #951).
INERT_IMAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
}

#: Served back for the customizer's preview. The SVG is already sanitised; these
#: make a browser that opens the URL directly treat it as an inert image anyway.
CONTENT_HEADERS = {
    **INERT_IMAGE_HEADERS,
    "Cache-Control": "public, max-age=31536000, immutable",
}


#: A sample lives in the template's directory, which an edit can change under the
#: same URL, so it is revalidated rather than kept.
SAMPLE_HEADERS = {**CONTENT_HEADERS, "Cache-Control": "no-cache"}


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
    responses=OPERATION_RESPONSES,
)
async def upload_asset(
    slug: SlugPath,
    response: Response,
    ops: OperationsDep,
    paths: PathsDep,
    file: Annotated[UploadFile, File(description="An SVG or PNG")],
    idempotency_key: IdempotencyKey = None,
) -> AssetMeta | JSONResponse:
    data = await file.read(MAX_ASSET_BYTES + 1)
    if len(data) > MAX_ASSET_BYTES:
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"the file is larger than {MAX_ASSET_BYTES} bytes",
        )
    # By claim: up to MAX_ASSET_BYTES, past a workflow payload's limit (#1054).
    claims = ClaimStore(paths.claims)
    held = await asyncio.to_thread(claims.hold, data)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["asset_upload"],
        subject=slug,
        request={"slug": slug, "file": held.name, "file_name": file.filename},
        idempotency_key=idempotency_key,
        claimed=Claimed(claims, [held]),
    )
    return operation_answer(result, AssetMeta)


async def store_asset(
    slug: str,
    data: bytes,
    filename: str | None,
    *,
    assets: AssetStore,
    state: AppState,
    source_url: str | None = None,
    licence: AssetLicence | None = None,
) -> AssetMeta:
    """An upload's bytes into the store, and mirrored where workers read them: the
    ``asset_upload`` and ``asset_fetch`` operations' runs (#1054)."""
    try:
        # Decoding a PNG and parsing an SVG are CPU work; keep them off the loop.
        meta = await asyncio.to_thread(
            partial(assets.put, data, filename, source_url=source_url, licence=licence)
        )
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


class AssetFetch(BaseModel):
    url: str = Field(max_length=2048, description="An https URL to an SVG or PNG")
    allow_host: str | None = Field(
        default=None,
        max_length=253,
        description=(
            "Let this host through for this one fetch although it is not on the asset "
            "allowlist (#959): it must be the URL's own host exactly (its IDNA form, "
            "lower case), not a subdomain, and covers redirects within it. The allowlist "
            "is left as it was. Refused without the `X-ScadBuddy-Fetch-Grant` the agent "
            "mints for a `fetch_asset` call a person approved, which serves one fetch"
        ),
    )
    licence: AssetLicence | None = Field(
        default=None,
        description=(
            "The licence the file is under, as the source states it (`search_icons` "
            "answers it per icon); kept on the asset as given, never checked"
        ),
    )


class FetchedAsset(AssetMeta):
    #: The URL fetched, to credit: its scheme, host, port and path, never its query,
    #: userinfo or fragment, which may carry a token (#1054, as `ModelRecord.origin_url`).
    #: Not capped: it is the parsed form, which percent-encodes what the request's 2048
    #: characters allowed, and can be longer.
    source_url: str
    #: Whether the model's README credits this source (#959): a line was added to its
    #: Credits section, or one was already there.
    credited: bool = False


@router.post(
    "/models/{slug}/assets/fetch",
    response_model=FetchedAsset,
    status_code=status.HTTP_201_CREATED,
    summary="Fetch a file for a file parameter from a URL",
    description=(
        "Fetches an SVG or PNG on the server and stores it exactly as an upload: the "
        "answer's `id` is the value a `// file` parameter takes. The URL's host, and "
        "every redirect's, must be on the asset allowlist (`asset_fetch_domains` in "
        "Settings, each domain with its subdomains), or be the body's `allow_host`: one "
        "fetch from that host alone, which needs the single-use grant a person's approval "
        "of this fetch minted in the `X-ScadBuddy-Fetch-Grant` header, bound to that host "
        "and URL. The allowlist is left as it is. Otherwise as the URL import: "
        f"https only, public internet addresses only, at most {MAX_ASSET_BYTES} bytes, "
        f"within {IMPORT_TIMEOUT:.0f} seconds; a URL with a backslash, a control "
        "character or a user name or password is refused. Every refusal of the URL or "
        "its content is a 422, as is an `allow_host` that is not the URL's host; a "
        "missing, forged, expired, other-host, other-URL or spent grant is a 403, and a "
        "one-off with no secret key configured to check it is a 503. A full store is "
        "the upload's 413. The asset keeps the URL and its licence: the body's `licence`, "
        "else the one known for its catalogue (Material Design Icons, Tabler, game-icons, "
        "OpenMoji). The model's README gets a line in its Credits section naming the "
        "file, the source and the licence, once per source URL, as a revision of its own."
    ),
    responses={
        **OPERATION_RESPONSES,
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "an `allow_host` fetch whose `X-ScadBuddy-Fetch-Grant` is missing, forged, "
                "expired, for another host or URL, or already spent"
            )
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": (
                "the replica's fetch budget (shared with imports) or its resolver threads "
                "are all in use; retry after `Retry-After` seconds. Also an `allow_host` "
                "fetch on a server with no secret key to check its grant"
            )
        },
    },
)
async def fetch_asset(
    slug: SlugPath,
    body: AssetFetch,
    response: Response,
    ops: OperationsDep,
    imports: ImportsDep,
    paths: PathsDep,
    assets: AssetsDep,
    settings: SettingsStoreDep,
    idempotency_key: IdempotencyKey = None,
    fetch_grant: Annotated[str | None, Header(alias=FETCH_GRANT_HEADER)] = None,
) -> FetchedAsset | JSONResponse:
    pasted = body.url.strip()
    try:
        # Refusals that quote the URL whole: here, so no operation records them.
        parsed = parse_fetch_url(pasted)
    except ImportRefusedError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    if body.allow_host is not None:
        await _spend_grant(
            body.allow_host, parsed, pasted, fetch_grant, idempotency_key, assets, settings
        )
    # The URL by claim, so its query, which may carry a token, is in neither the
    # operation's record nor its history (as an import's, review 3c 1.5).
    claims = ClaimStore(paths.claims)
    url = await asyncio.to_thread(claims.hold, pasted.encode())

    async def permit() -> None:
        # Before the operation starts, so a full budget answers with its Retry-After
        # header and records nothing; the run asks again.
        _require_fetch_permit(imports)

    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["asset_fetch"],
        # The host, never the URL: the subject is a search attribute.
        subject=urlsplit(pasted).hostname or "url",
        request={
            "slug": slug,
            "url": url.name,
            "shown": shown_url(pasted),
            "licence": None if body.licence is None else body.licence.model_dump(),
            "allow_host": body.allow_host,
        },
        idempotency_key=idempotency_key,
        claimed=Claimed(claims, [url]),
        before_start=permit,
    )
    return operation_answer(result, FetchedAsset)


async def _spend_grant(
    allow_host: str,
    parsed: httpx.URL,
    pasted: str,
    grant: str | None,
    idempotency_key: str | None,
    assets: AssetStore,
    settings: SettingsStore,
) -> None:
    """A one-off host's checks (#959): the host is the one this route fetches from, by
    its own parse, and the grant is a person's approval of this very fetch, spent now."""
    host = ascii_host(parsed)
    if allow_host != host:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"allow_host is {allow_host!r}, but the URL's host is {host!r}; nothing was fetched",
        )
    if grant is None:
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "a host off the asset allowlist is fetched only with the grant of a person's "
            "approval (the assistant's fetch_asset sends one); add the domain in Settings instead",
        )
    kek = settings.kek
    if kek is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "a one-off host needs the secret key (SCADBUDDY_SECRET_KEY_FILE) to check its grant",
        )
    now = datetime.now(UTC)
    try:
        nonce = verify_fetch_grant(fetch_grant_key(kek), grant, host=host, url=pasted, now=now)
    except FetchGrantError as error:
        raise ApiError(status.HTTP_403_FORBIDDEN, str(error)) from None
    expires = now + timedelta(seconds=FETCH_GRANT_TTL_SECONDS)
    if not await asyncio.to_thread(assets.spend_fetch_grant, nonce, idempotency_key, expires):
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "this fetch grant was already used; each approval serves one fetch",
        )


def _require_fetch_permit(imports: ImportPermits) -> None:
    # As the import: no await between the check and the hold.
    if imports.full():
        raise fetch_busy(
            f"{IMPORT_CONCURRENCY} fetches are already running on this replica",
            imports.retry_after(),
        )


async def fetch_run(
    slug: str,
    url: str,
    state: AppState,
    *,
    licence: AssetLicence | None = None,
    allow_host: str | None = None,
) -> FetchedAsset:
    """The ``asset_fetch`` operation's run (#1054): the fetch, then the store."""
    # Off the loop: `load` is blocking psycopg I/O.
    domains = (await asyncio.to_thread(state.settings_store.load)).allowed_asset_domains()
    imports = state.imports
    _require_fetch_permit(imports)
    with imports.hold():
        try:
            fetched = await fetch_file(
                url, domains=domains, limit=MAX_ASSET_BYTES, allow_host=allow_host
            )
        except ImportRefusedError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        except ResolverBusyError:
            raise fetch_busy(
                "every resolver thread on this replica is busy", RESOLVER_RETRY_AFTER
            ) from None
    # Never the URL as given: its query may carry a token, and the row and this answer
    # are kept.
    source_url = shown_url(fetched.source_url)
    # The body's licence, else the one its catalogue is known by: stored, credited and
    # answered alike.
    licence = licence or known_licence(source_url)
    meta = await store_asset(
        slug,
        fetched.data,
        fetched.filename,
        assets=state.assets,
        state=state,
        source_url=source_url,
        licence=licence,
    )
    # This fetch's URL and licence, even when the content was stored from elsewhere
    # first: they are what the README credits.
    credited = await _credit(slug, meta.name, source_url, licence, state)
    return FetchedAsset(
        **{**meta.model_dump(), "source_url": source_url, "licence": licence, "credited": credited}
    )


#: How often a credit is tried again when the model moved under it.
CREDIT_ATTEMPTS = 3


async def _credit(
    slug: str, name: str, source_url: str, licence: AssetLicence | None, state: AppState
) -> bool:
    """Add ``source_url``'s credit to the model's README, once (#959). The README is read
    and written at one revision, so an edit landing in between is never overwritten:
    the credit is made again on top of it. False when the README cannot be written
    (no model history): the file is stored all the same, and the answer says so."""
    line = credit_line(name, source_url, licence)
    catalogue = state.catalogue

    def attempt() -> bool:
        version = catalogue.record(slug).version
        try:
            readme = catalogue.read_readme(slug)
        except SidecarNotFoundError:
            readme = ""
        text = with_credit(readme, source_url, line)
        if text is None:
            return False
        catalogue.write_readme(
            slug, text, message=f"Credit {name} in the README", expected_version=version
        )
        return True

    for _ in range(CREDIT_ATTEMPTS):
        try:
            written = await asyncio.to_thread(attempt)
        except StaleVersionError:
            continue
        except ModelNotFoundError:
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
        except GitError as error:
            logger.warning("asset credit for %s not written: %s", slug, type(error).__name__)
            return False
        if written:
            emit(state.events, ModelEvent(kind="model.updated", slug=slug))
        return True
    raise ApiError(
        status.HTTP_409_CONFLICT,
        f"the file is stored, but {slug} kept changing while its README was credited; "
        "fetch it again to credit it",
    )


@router.get(
    "/icons/search",
    response_model=IconSearch,
    summary="Search open icon catalogues",
    description=(
        "Searches " + ", ".join(COLLECTIONS) + " (Iconify prefixes) through "
        "api.iconify.design, on the server. Each icon has its catalogue's licence and an "
        "SVG `url` for `POST /models/{slug}/assets/fetch`, with `allowlisted` saying "
        "whether the asset allowlist lets that fetch through. The request is the URL "
        f"import's: https, public addresses, no redirect, within {IMPORT_TIMEOUT:.0f} "
        "seconds. A catalogue that fails to answer is a 502."
    ),
    responses={
        status.HTTP_502_BAD_GATEWAY: {"description": "the catalogue could not be searched"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": (
                "the replica's fetch budget (shared with imports) or its resolver threads "
                "are all in use; retry after `Retry-After` seconds"
            )
        },
    },
)
async def search_icon_catalogues(
    imports: ImportsDep,
    settings: SettingsStoreDep,
    q: Annotated[str, Query(min_length=1, max_length=100, pattern=r"\S")],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
) -> IconSearch:
    domains = (await asyncio.to_thread(settings.load)).allowed_asset_domains()
    _require_fetch_permit(imports)
    with imports.hold():
        try:
            return await search_icons(q.strip(), limit=limit, domains=domains)
        except ImportRefusedError as error:
            raise ApiError(status.HTTP_502_BAD_GATEWAY, str(error)) from None
        except ResolverBusyError:
            raise fetch_busy(
                "every resolver thread on this replica is busy", RESOLVER_RETRY_AFTER
            ) from None


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
