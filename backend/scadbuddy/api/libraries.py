"""Third-party OpenSCAD libraries (#93): the catalogue, and pinning one into the lockfile.

A model declares which of these it uses with ``PATCH /models/{slug}``'s
``libraries``; that route and every render path live with the models.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from scadbuddy.api.deps import LibrariesDep
from scadbuddy.core.problems import ApiError, problem_response
from scadbuddy.library.history import GitError
from scadbuddy.library.libraries import (
    NAME_PATTERN,
    REF_PATTERN,
    LibraryEntry,
    LibraryError,
    LibraryFetchError,
    LibraryNotFoundError,
    LibraryNotInstalledError,
)

router = APIRouter(tags=["libraries"])


class LibraryAdd(BaseModel):
    name: str = Field(pattern=NAME_PATTERN, description="The directory `use <NAME/...>` names")
    url: str | None = Field(
        default=None,
        max_length=500,
        description="An https git URL; the catalogue's when omitted",
    )
    ref: str | None = Field(
        default=None,
        pattern=REF_PATTERN,
        description="A tag or branch to pin; the catalogue's default when omitted",
    )


@router.get("/libraries", response_model=list[LibraryEntry], summary="Libraries and their pins")
def list_libraries(libraries: LibrariesDep) -> list[LibraryEntry]:
    return libraries.entries()


@router.post(
    "/libraries",
    response_model=LibraryEntry,
    summary="Add a library, or pin it to another ref",
    description=(
        "Clones the library at `ref` onto the data volume and records the commit that "
        "resolved to in `libraries.lock`, as one revision of the models repository. "
        "Models that declare it render against the new pin from then on."
    ),
)
async def add_library(body: LibraryAdd, libraries: LibrariesDep) -> LibraryEntry:
    try:
        # A clone is a network fetch; off the loop.
        await asyncio.to_thread(libraries.install, body.name, url=body.url, ref=body.ref)
    except LibraryNotFoundError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"{body.name!r} is not in the catalogue; give a url to add it",
        ) from None
    except LibraryFetchError as error:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, str(error)) from None
    except LibraryError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    return next(entry for entry in libraries.entries() if entry.name == body.name)


def install_library_handlers(app: FastAPI) -> None:
    """A model declaring a library that is not on the volume is a 409 from every
    route that resolves its source -- schema, render, check -- rather than each one
    catching it, or the 500 an uncaught one would be."""

    @app.exception_handler(LibraryNotInstalledError)
    async def _not_installed(request: Request, exc: LibraryNotInstalledError) -> JSONResponse:
        return problem_response(request, status.HTTP_409_CONFLICT, str(exc.args[0]))
