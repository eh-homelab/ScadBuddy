from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response, WebSocket, status
from fastapi import Path as PathParam

from scadbuddy.api.deps import STATE_ATTR, AppState, CatalogueDep, PathsDep, SlugPath
from scadbuddy.api.libraries import LibraryName
from scadbuddy.api.models import MAX_SOURCE_CHARS, require_model_exists
from scadbuddy.core.fontconfig import env_for
from scadbuddy.core.problems import ApiError
from scadbuddy.library.editor_files import (
    MAX_PATH_LENGTH,
    FilePathError,
    FileTooLargeError,
    NotTextError,
    read_text_file,
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
    # No await between the check and the acquire, so nothing can take the permit
    # in between.
    if state.language_servers.locked():
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
