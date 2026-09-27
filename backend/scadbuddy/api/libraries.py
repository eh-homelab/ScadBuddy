"""Third-party OpenSCAD libraries (#93): the catalogue, and pinning one to a model.

A pin belongs to one model: ``PUT /models/{slug}/libraries/{name}`` clones the
library at a ref and records the commit in that model's ``model.json``, as one
revision of the model. Another model declaring the same library keeps its own pin.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, FastAPI, Path, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.deps import CatalogueDep, InstallsDep, LibrariesDep, SlugPath
from scadbuddy.api.models import require_mine, require_model_exists
from scadbuddy.core.problems import ApiError, problem_response
from scadbuddy.library.catalogue import LibraryNotDeclaredError, ModelNotFoundError, ModelRecord
from scadbuddy.library.history import GitError
from scadbuddy.library.libraries import (
    NAME_PATTERN,
    REF_PATTERN,
    CatalogueLibrary,
    LibraryDeclarationError,
    LibraryError,
    LibraryFetchError,
    LibraryNotFoundError,
    LibraryNotInstalledError,
)

router = APIRouter(tags=["libraries"])

LibraryName = Annotated[
    str, Path(pattern=NAME_PATTERN, description="The directory `use <NAME/...>` names")
]


class LibraryPinRequest(BaseModel):
    # REF_PATTERN refuses `..` with a look-ahead, which pydantic's default engine lacks.
    model_config = ConfigDict(regex_engine="python-re")

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


@router.get(
    "/libraries",
    response_model=list[CatalogueLibrary],
    summary="The library catalogue",
    description="Libraries ScadBuddy knows how to fetch, each with the ref it suggests. "
    "Any other can be pinned to a model by URL.",
)
def list_libraries(libraries: LibrariesDep) -> list[CatalogueLibrary]:
    return libraries.entries()


@router.put(
    "/models/{slug}/libraries/{name}",
    response_model=ModelRecord,
    summary="Pin a library to a model, or re-pin it at another ref",
    description=(
        "Clones the library at `ref` onto the data volume and records the commit that "
        "resolved to in this model's `model.json`, as one revision of the model. The "
        "model renders against that pin from then on; no other model moves."
    ),
)
async def pin_library(
    slug: SlugPath,
    name: LibraryName,
    body: LibraryPinRequest,
    catalogue: CatalogueDep,
    libraries: LibrariesDep,
    installs: InstallsDep,
) -> ModelRecord:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    try:
        # A clone is a network fetch; off the loop, and a bounded number at a time.
        async with installs:
            pin = await asyncio.to_thread(libraries.resolve, name, url=body.url, ref=body.ref)
        return await asyncio.to_thread(catalogue.pin_library, slug, pin)
    except LibraryNotFoundError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"{name!r} is not in the catalogue; give a url to pin it from",
        ) from None
    except LibraryFetchError as error:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, str(error)) from None
    except LibraryError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except ModelNotFoundError:
        # A concurrent delete of the same slug got there first.
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None


@router.delete(
    "/models/{slug}/libraries/{name}",
    response_model=ModelRecord,
    summary="Remove a library from a model",
    description="The checkout stays on the volume: an older revision may still pin it.",
)
def unpin_library(slug: SlugPath, name: LibraryName, catalogue: CatalogueDep) -> ModelRecord:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    try:
        return catalogue.unpin_library(slug, name)
    except LibraryNotDeclaredError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"{slug!r} does not declare a library named {name!r}"
        ) from None
    except ModelNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None


def install_library_handlers(app: FastAPI) -> None:
    """A model declaring a library that is not on the volume is a 409 from every
    route that resolves its source -- schema, render, check -- rather than each one
    catching it, or the 500 an uncaught one would be. So is one whose `libraries`
    entry cannot be read: the model cannot render until it is pinned again."""

    @app.exception_handler(LibraryNotInstalledError)
    async def _not_installed(request: Request, exc: LibraryNotInstalledError) -> JSONResponse:
        return problem_response(request, status.HTTP_409_CONFLICT, str(exc.args[0]))

    @app.exception_handler(LibraryDeclarationError)
    async def _bad_declaration(request: Request, exc: LibraryDeclarationError) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_409_CONFLICT,
            str(exc.args[0]),
            title="Invalid Library Declaration",
        )
