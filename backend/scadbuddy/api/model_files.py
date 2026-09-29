"""The other source files of a multi-file model (#252).

OpenSCAD resolves ``include <x.scad>`` and ``use <x.scad>`` beside the file doing it
(https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Include_Statement), and every
render already runs in the model's directory with those files present: the parse
check copies them (``library/scad.py`` ``SIDECARS``), the render cache key hashes
them (``render/provenance.py`` ``source_version``), and an older revision is
exported whole. What was missing was a way to read and write them. These routes do,
for bare ``.scad`` names at the top of the directory; reading one is the editor's
``GET /models/{slug}/files/{path}`` (``api/lsp.py``, #707), which already serves any
text file in the model's directory. ``model.scad`` is written only through
``PUT /models/{slug}/source``, which parse-checks it. A sibling is not parse-checked
on its own: it is often a library of modules with no top-level geometry, and what
matters is whether the model that includes it still renders, which
``POST /models/check`` with the model's ``slug`` answers.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, Field

from scadbuddy.api.deps import CatalogueDep, EventsDep, SlugPath
from scadbuddy.api.models import (
    MAX_SOURCE_CHARS,
    announce_source_change,
    require_mine,
    require_model_exists,
)
from scadbuddy.core.paths import SOURCE_NAME
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import (
    ModelNotFoundError,
    ModelRecord,
    SidecarNotFoundError,
    TooManySourceFilesError,
)
from scadbuddy.library.history import MAX_SUBJECT, GitError

router = APIRouter(tags=["models"])

#: A bare ``.scad`` file name: no directory, no leading dot.
SOURCE_FILE_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}\.scad$"
#: How many ``.scad`` files a model may hold, its own included. Every one is hashed on
#: every render (``source_version``) and copied into every parse check.
MAX_SOURCE_FILES = 50

FileNamePath = Annotated[str, Path(pattern=SOURCE_FILE_PATTERN)]


class SourceFile(BaseModel):
    name: str
    size: int = Field(description="Bytes")
    main: bool = Field(description="True for model.scad, the file every render opens")


class SourceFileUpdate(BaseModel):
    # MAX_SOURCE_CHARS is also the agent's bound, in agent/src/tools/sourceFiles.ts
    # (`content`'s Zod max); change both together (PR #752 review).
    content: str = Field(max_length=MAX_SOURCE_CHARS, description="The file's OpenSCAD text")
    message: str | None = Field(
        default=None,
        max_length=MAX_SUBJECT,
        description="What the revision is called in the history; a default when omitted",
    )


def _require_sibling(name: str) -> None:
    if name == SOURCE_NAME:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{SOURCE_NAME} is the model's own source: write it with PUT /models/{{slug}}/source, "
            "which parse-checks it",
        )


@router.get(
    "/models/{slug}/files",
    response_model=list[SourceFile],
    summary="A model's .scad files",
    description="Every `.scad` file at the top of the model's directory, `model.scad` first.",
)
def list_source_files(slug: SlugPath, catalogue: CatalogueDep) -> list[SourceFile]:
    try:
        files = catalogue.source_files(slug)
    except ModelNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    listed = [
        SourceFile(name=path.name, size=path.stat().st_size, main=path.name == SOURCE_NAME)
        for path in files
    ]
    return sorted(listed, key=lambda f: (not f.main, f.name))


@router.put(
    "/models/{slug}/files/{name}",
    response_model=ModelRecord,
    summary="Write one of a model's other .scad files as one revision",
    description=(
        "Creates or replaces a `.scad` file beside `model.scad`, which it can then "
        "`include` or `use`, as one revision named by `message`. `model.scad` itself is "
        "a 409: write it with `PUT /models/{slug}/source`. Not parse-checked on its own; "
        f"check the model with `POST /models/check` and its `slug`. At most "
        f"{MAX_SOURCE_FILES} `.scad` files per model (#252)."
    ),
)
async def put_source_file(
    slug: SlugPath,
    name: FileNamePath,
    body: SourceFileUpdate,
    catalogue: CatalogueDep,
    events: EventsDep,
) -> ModelRecord:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    _require_sibling(name)
    if "\x00" in body.content:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "the file contains a NUL byte, so it is binary, not OpenSCAD text",
        )
    try:
        record = await asyncio.to_thread(
            catalogue.write_file,
            slug,
            name,
            body.content,
            message=body.message,
            max_files=MAX_SOURCE_FILES,
        )
    except ModelNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    except TooManySourceFilesError as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{slug!r} already has {error.count} .scad files, the most a model may hold",
        ) from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    announce_source_change(events, slug)
    return record


@router.delete(
    "/models/{slug}/files/{name}",
    response_model=ModelRecord,
    summary="Remove one of a model's other .scad files as one revision",
    description="`model.scad` itself is a 409; a file that is not there is a 404 (#252).",
)
async def delete_source_file(
    slug: SlugPath,
    name: FileNamePath,
    catalogue: CatalogueDep,
    events: EventsDep,
) -> ModelRecord:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    _require_sibling(name)
    try:
        record = await asyncio.to_thread(catalogue.write_file, slug, name, None)
    except ModelNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    except SidecarNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} has no file {name!r}") from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    announce_source_change(events, slug)
    return record
