from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Response, WebSocket, status
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

from scadbuddy.api.assets import INERT_IMAGE_HEADERS
from scadbuddy.api.deps import (
    STATE_ATTR,
    AppState,
    CatalogueDep,
    HistoryDep,
    PathsDep,
    SlugPath,
    StateDep,
)
from scadbuddy.api.libraries import LibraryName
from scadbuddy.api.models import (
    MAX_SOURCE_CHARS,
    MAX_THUMBNAIL_BYTES,
    THUMBNAIL_CACHE_CONTROL,
    etag_matches,
    require_model_exists,
)
from scadbuddy.api.versions import require_history
from scadbuddy.core.fontconfig import env_for
from scadbuddy.core.paths import model_path
from scadbuddy.core.problems import ApiError
from scadbuddy.library.editor_files import (
    MAX_PATH_LENGTH,
    FilePathError,
    FileTooLargeError,
    NotTextError,
    plain_segments,
    read_file,
    read_text_file,
)
from scadbuddy.library.history import (
    COMMIT_ID_PATTERN,
    BlobTooLargeError,
    GitError,
    ModelHistory,
    RevisionNotFoundError,
)
from scadbuddy.library.libraries import (
    COMMIT_PATTERN,
    CheckoutFetcher,
    LibraryDeclarationError,
    LibraryNotInstalledError,
    declared_libraries,
    model_search_path,
    resolve_search_path,
)
from scadbuddy.library.lsp import serve
from scadbuddy.library.lsp_diagnostics import LspDiagnostic, LspDiagnosticsError, lsp_diagnostics
from scadbuddy.library.slugs import MAX_MODEL_ID_LENGTH, MODEL_ID_PATTERN

logger = logging.getLogger(__name__)

router = APIRouter(tags=["editor"])

FilePath = Annotated[
    str,
    PathParam(
        max_length=MAX_PATH_LENGTH,
        description="A plain relative path: `/`-separated, no `.`/`..` or dot-file segment",
    ),
]

TEXT_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/plain": {"schema": {"type": "string"}}}}
}


def _budget_full(state: AppState) -> bool:
    """Whether every language-server permit is taken. Both callers enter
    ``async with state.language_servers`` straight after this, with no ``await`` in
    between, and an asyncio.Semaphore that is not locked() is acquired without
    yielding, so no other request can take the permit in the gap. Keep it that way:
    an ``await`` between the two would turn a fast 503 into a wait (review of #750).
    """
    return state.language_servers.locked()


async def _libraries(state: AppState, slug: str) -> dict[str, Path]:
    """The model's pinned libraries, each by name with the directory ``use
    <name/...>`` resolves into, fetching a checkout that is gone as a render would.

    A pin that cannot be resolved leaves the editor with no libraries rather than no
    language server: OpenSCAD's own check reports the missing library already.
    """
    fetcher = CheckoutFetcher(state.libraries, state.installs, state.checkouts)
    try:
        search = await resolve_search_path(fetcher, partial(model_search_path, state.paths, slug))
    except (LibraryNotInstalledError, LibraryDeclarationError) as error:
        logger.warning("the editor's language server runs without %r's libraries: %s", slug, error)
        return {}
    # Each entry is `<libraries>/<name>/<commit>`, the parent of the `<name>` directory.
    return {entry.parent.name: entry / entry.parent.name for entry in search}


async def _serve(
    websocket: WebSocket,
    state: AppState,
    root: Path | None,
    libraries: Mapping[str, Path] | None = None,
) -> None:
    """Refusals close the socket before it is accepted, so the editor sees a failed
    connection and carries on without a language server — which is also what it
    does on a machine with none installed."""
    binary = shutil.which(state.config.openscad_lsp)
    if binary is None:
        logger.warning("openscad-lsp is not on PATH; the editor runs without it")
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return
    if _budget_full(state):
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
        return
    async with state.language_servers:
        await websocket.accept()
        env = env_for(state.config.data_dir)
        if libraries:
            # As on a render (`render/runner.py`): exactly the model's own pins.
            env["OPENSCADPATH"] = os.pathsep.join(
                str(directory.parent) for directory in libraries.values()
            )
        if root is not None:
            await serve(websocket, binary, root, env, libraries)
            return
        with tempfile.TemporaryDirectory(prefix="scadbuddy-lsp-") as scratch:
            await serve(websocket, binary, Path(scratch), env)


@router.websocket("/models/{slug}/lsp")
async def model_language_server(websocket: WebSocket, slug: SlugPath) -> None:
    """openscad-lsp for a saved model, rooted in its directory and with its pinned
    libraries on ``OPENSCADPATH``, so the model's ``include``/``use`` of its sibling
    files and of its libraries resolve as they do on render."""
    state: AppState = getattr(websocket.app.state, STATE_ATTR)
    if not state.catalogue.exists(slug):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    libraries = await _libraries(state, slug)
    await _serve(websocket, state, state.paths.model_dir(slug), libraries)


@router.websocket("/lsp")
async def scratch_language_server(websocket: WebSocket) -> None:
    """openscad-lsp for source that is not a model yet, in an empty directory that
    lasts as long as the session."""
    state: AppState = getattr(websocket.app.state, STATE_ATTR)
    await _serve(websocket, state, None)


def _text_file(root: Path, path: str) -> Response:
    try:
        text = read_text_file(root, path, limit=MAX_SOURCE_CHARS)
    except FilePathError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except FileNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no file {path!r}") from None
    except FileTooLargeError:
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"{path!r} is larger than the {MAX_SOURCE_CHARS:,} bytes the editor opens",
        ) from None
    except NotTextError:
        raise ApiError(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"{path!r} is not UTF-8 text"
        ) from None
    return Response(text, media_type="text/plain; charset=utf-8")


@router.get(
    "/models/{slug}/files/{path:path}",
    response_class=Response,
    responses=TEXT_RESPONSE,
    summary="A text file in a model's directory",
    description=(
        "Read-only, for the source editor's go-to-definition into an `include`/`use` "
        "target beside the model (#185). `path` is relative to the model's directory; "
        "an absolute path or one with a `.`, `..` or dot-file segment is a 422, a file "
        "that is missing or resolves outside the directory a 404, one over "
        f"{MAX_SOURCE_CHARS:,} bytes a 413, and one that is not UTF-8 text a 415."
    ),
)
def get_model_file(
    slug: SlugPath, path: FilePath, catalogue: CatalogueDep, paths: PathsDep
) -> Response:
    require_model_exists(catalogue, slug)
    return _text_file(paths.model_dir(slug), path)


#: An image a model's README shows beside it (#951), by extension, as served.
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

#: The largest image served from a model's directory: the cap a model's own
#: thumbnail takes, which is already far above any image a README shows.
MAX_MODEL_IMAGE_BYTES = MAX_THUMBNAIL_BYTES

#: A revision's bytes never change under its id.
REVISION_CACHE_CONTROL = "public, max-age=31536000, immutable"


def _image_type(path: str) -> str:
    media_type = IMAGE_MEDIA_TYPES.get(Path(path).suffix.lower())
    if media_type is None:
        raise ApiError(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"{path!r} is not an image ({', '.join(IMAGE_MEDIA_TYPES)})",
        )
    return media_type


def _too_large(path: str) -> ApiError:
    return ApiError(
        status.HTTP_413_CONTENT_TOO_LARGE,
        f"{path!r} is larger than the {MAX_MODEL_IMAGE_BYTES:,} bytes an image here may be",
    )


def _revision_image(history: ModelHistory, slug: str, path: str, commit: str) -> bytes:
    require_history(history)
    try:
        # Sized from the tree before it is read, as the working tree's file is stat'ed.
        return history.read_blob(commit, f"{model_path(slug)}/{path}", limit=MAX_MODEL_IMAGE_BYTES)
    except RevisionNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no {path!r} at {commit}") from None
    except BlobTooLargeError:
        raise _too_large(path) from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None


@router.get(
    "/models/{slug}/images/{path:path}",
    response_class=Response,
    responses={
        200: {
            "content": {media_type: {} for media_type in sorted(set(IMAGE_MEDIA_TYPES.values()))}
        },
        304: {"description": "The copy named by `If-None-Match` is still current"},
        503: {"description": "`commit` was given but model history is unavailable"},
    },
    summary="An image file in a model's directory",
    description=(
        "Read-only, for a model README's relative image (`![](thumbnail.png)`, #951). "
        "The same path rules as `GET /models/{slug}/files/{path}`: an absolute path or "
        "one with a `.`, `..` or dot-file segment is a 422, a file that is missing or "
        "resolves outside the directory a 404. Only "
        f"{', '.join(IMAGE_MEDIA_TYPES)} are served, each with its image type (else a "
        f"415), and none over {MAX_MODEL_IMAGE_BYTES:,} bytes (a 413). With `commit`, "
        "the file as it was at that revision (404 when it did not exist then, 503 when "
        "there is no model history), cached as immutable; without, the working tree's, "
        "with a strong `ETag` and `Cache-Control: no-cache`."
    ),
)
def get_model_image(
    slug: SlugPath,
    path: FilePath,
    catalogue: CatalogueDep,
    paths: PathsDep,
    history: HistoryDep,
    commit: Annotated[
        str | None,
        Query(pattern=COMMIT_ID_PATTERN, description="The revision to read the image at"),
    ] = None,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    require_model_exists(catalogue, slug)
    try:
        plain = "/".join(plain_segments(path))
    except FilePathError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    media_type = _image_type(plain)
    if commit is not None:
        data = _revision_image(history, slug, plain, commit)
        cache_control = REVISION_CACHE_CONTROL
    else:
        try:
            data = read_file(paths.model_dir(slug), plain, limit=MAX_MODEL_IMAGE_BYTES)
        except FileNotFoundError:
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no file {path!r}") from None
        except FileTooLargeError:
            raise _too_large(path) from None
        cache_control = THUMBNAIL_CACHE_CONTROL
    etag = f'"{hashlib.sha256(data).hexdigest()}"'
    headers = {**INERT_IMAGE_HEADERS, "ETag": etag, "Cache-Control": cache_control}
    if etag_matches(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(data, media_type=media_type, headers=headers)


@router.get(
    "/models/{slug}/libraries/{name}/files/{path:path}",
    response_class=Response,
    responses=TEXT_RESPONSE,
    summary="A text file in a library the model pins",
    description=(
        "Read-only, for the source editor's go-to-definition into a library on the "
        "model's `OPENSCADPATH` (#185). `path` is relative to the library's directory "
        "(`BOSL2/std.scad` is `std.scad` under `BOSL2`) in the checkout the model's pin "
        "names. `commit` is the one the editor's URI for the file carries "
        "(`file:///libraries/<name>@<commit>/...`): a 409 when the pin has moved on since. "
        "A 404 when the model does not pin `name` or its checkout is not on the volume; "
        "otherwise the same refusals as a file in the model's directory."
    ),
)
def get_library_file(
    slug: SlugPath,
    name: LibraryName,
    path: FilePath,
    catalogue: CatalogueDep,
    paths: PathsDep,
    commit: Annotated[
        str | None,
        Query(
            pattern=COMMIT_PATTERN,
            description="The commit the editor's URI names; a 409 when the pin has moved",
        ),
    ] = None,
) -> Response:
    require_model_exists(catalogue, slug)
    pin = next((p for p in declared_libraries(paths.model_dir(slug)) if p.name == name), None)
    if pin is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} does not pin a library {name!r}")
    if commit is not None and commit != pin.commit:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{slug!r} pins {name!r} at {pin.commit[:7]} now, not {commit[:7]}; "
            "reopen the editor to follow the new pin",
        )
    return _text_file(paths.libraries / name / pin.commit / name, path)


#: What a 503 from a busy language-server budget says to wait.
LSP_RETRY_AFTER = 2


class LspDiagnosticsRequest(BaseModel):
    source: str = Field(max_length=MAX_SOURCE_CHARS, description="The OpenSCAD source")
    slug: str | None = Field(
        default=None,
        pattern=MODEL_ID_PATTERN,
        max_length=MAX_MODEL_ID_LENGTH,
        description="Open the source in this model's directory, so its includes resolve",
    )


class LspDiagnostics(BaseModel):
    available: bool = Field(description="False when no openscad-lsp is installed to ask")
    diagnostics: list[LspDiagnostic] = Field(default_factory=list)


@router.post(
    "/lsp/diagnostics",
    response_model=LspDiagnostics,
    summary="openscad-lsp's diagnostics for a source",
    description=(
        "Runs the editor's language server once on `source` and returns what it "
        "publishes: tree-sitter parse errors with line and column ranges, and a missing "
        "file for a leading `include`. Saves nothing and runs no OpenSCAD; "
        "`POST /models/check` is OpenSCAD's own check. Shares the editor's "
        "`SCADBUDDY_LSP_SESSIONS` budget: 503 with Retry-After when it is full (#252)."
    ),
)
async def post_lsp_diagnostics(body: LspDiagnosticsRequest, state: StateDep) -> LspDiagnostics:
    root: Path | None = None
    libraries: dict[str, Path] = {}
    if body.slug is not None:
        if not state.catalogue.exists(body.slug):
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {body.slug!r}")
        root = state.paths.model_dir(body.slug)
        # The editor's bridge (`_serve`, #707) resolves includes through the model's
        # pins too, so the two report the same missing files.
        libraries = await _libraries(state, body.slug)
    binary = shutil.which(state.config.openscad_lsp)
    if binary is None:
        return LspDiagnostics(available=False)
    if _budget_full(state):
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "every language server is in use; try again shortly",
            headers={"Retry-After": str(LSP_RETRY_AFTER)},
        )
    async with state.language_servers:
        env = env_for(state.config.data_dir)
        if libraries:
            env["OPENSCADPATH"] = os.pathsep.join(
                str(directory.parent) for directory in libraries.values()
            )
        try:
            if root is not None:
                found = await lsp_diagnostics(binary, root, body.source, env=env)
            else:
                with tempfile.TemporaryDirectory(prefix="scadbuddy-lsp-") as scratch:
                    found = await lsp_diagnostics(binary, Path(scratch), body.source, env=env)
        except LspDiagnosticsError as error:
            raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from None
    return LspDiagnostics(available=True, diagnostics=found)
