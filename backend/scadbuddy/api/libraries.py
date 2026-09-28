"""Third-party OpenSCAD libraries (#93): the catalogue, and pinning one to a model.

A pin belongs to one model: ``PUT /models/{slug}/libraries/{name}`` clones the
library at a ref and records the commit in that model's ``model.json``, as one
revision of the model. Another model declaring the same library keeps its own pin.
``PATCH`` re-pins from the upstream the model already pins (#253).

The checkouts themselves are a cache on the volume: ``GET /libraries/installed``
lists them and ``DELETE /libraries/{name}`` removes them, refused while any model
still pins one. A pinned checkout missing from the volume is cloned again at its
commit when a render or create needs it (``CheckoutFetcher``), and the boot sweep
removes the ones no revision of any model pins (``sweep_library_checkouts``).
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Annotated

from fastapi import APIRouter, FastAPI, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.deps import (
    CatalogueDep,
    CheckoutsDep,
    EventsDep,
    InstallsDep,
    LibrariesDep,
    PathsDep,
    SlugPath,
)
from scadbuddy.api.library_pins import resolve_pin
from scadbuddy.api.models import require_mine, require_model_exists
from scadbuddy.core.events import EventBus, LibraryChanged, LibraryRemoved, ModelEvent, emit
from scadbuddy.core.problems import ApiError, problem_response
from scadbuddy.library.catalogue import (
    Catalogue,
    LibraryNotDeclaredError,
    LibraryPinChangedError,
    ModelNotFoundError,
    ModelRecord,
)
from scadbuddy.library.history import GitError
from scadbuddy.library.libraries import (
    COMMIT_PATTERN,
    NAME_PATTERN,
    REF_PATTERN,
    CatalogueLibrary,
    CheckoutGate,
    LibraryCheckoutNotFoundError,
    LibraryDeclarationError,
    LibraryError,
    LibraryNotInstalledError,
    LibraryStore,
    ModelLibrary,
    declared_libraries,
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


class LibraryRepinRequest(BaseModel):
    model_config = ConfigDict(regex_engine="python-re")

    ref: str | None = Field(
        default=None,
        pattern=REF_PATTERN,
        description="The tag or branch to pin; the ref already pinned when omitted, "
        "which moves a branch pin to where that branch is now",
    )


class InstalledLibrary(BaseModel):
    """One checkout on the volume, and the models whose live pins read it."""

    name: str
    commit: str
    used_by: list[str] = Field(default_factory=list)


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
    checkouts: CheckoutsDep,
    events: EventsDep,
) -> ModelRecord:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    return await _pin(
        slug,
        name,
        url=body.url,
        ref=body.ref,
        catalogue=catalogue,
        libraries=libraries,
        installs=installs,
        checkouts=checkouts,
        events=events,
    )


async def _pin(
    slug: str,
    name: str,
    *,
    url: str | None,
    ref: str | None,
    catalogue: Catalogue,
    libraries: LibraryStore,
    installs: asyncio.Semaphore,
    checkouts: CheckoutGate,
    events: EventBus,
    replacing: ModelLibrary | None = None,
) -> ModelRecord:
    """Clone ``name`` and record the pin in ``slug``, with the same checks and status
    codes for a first pin and a re-pin. ``replacing`` is the entry a re-pin read:
    the record is refused, a 409, if it changed while the clone ran."""
    try:
        # Held from the clone to the record, so no removal lands in between.
        async with checkouts.pinning():
            pin = await resolve_pin(name, url=url, ref=ref, libraries=libraries, installs=installs)
            record = await asyncio.to_thread(
                partial(catalogue.pin_library, slug, pin, replacing=replacing)
            )
    except LibraryPinChangedError:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{slug!r}'s {name!r} was changed or removed while this re-pin ran; "
            "nothing was recorded",
        ) from None
    except ModelNotFoundError:
        # A concurrent delete of the same slug got there first.
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    _library_changed(events, slug, name)
    return record


@router.patch(
    "/models/{slug}/libraries/{name}",
    response_model=ModelRecord,
    summary="Re-pin a model's library from the upstream it already pins",
    description=(
        "Clones the library again from the URL this model's pin records -- a fork stays "
        "a fork -- at `ref`, or at the ref already pinned when `ref` is omitted (so a "
        "branch pin moves to the branch's current commit), and records the commit as one "
        "revision of the model. The same checks and errors as pinning it in the first "
        "place; a 404 when the model does not declare the library, and a 409 when its "
        "entry is changed or removed by another request while the clone runs."
    ),
)
async def repin_library(
    slug: SlugPath,
    name: LibraryName,
    body: LibraryRepinRequest,
    catalogue: CatalogueDep,
    libraries: LibrariesDep,
    installs: InstallsDep,
    checkouts: CheckoutsDep,
    paths: PathsDep,
    events: EventsDep,
) -> ModelRecord:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    # A malformed declaration is the 409 every other reader of it gives
    # (install_library_handlers); PUT is the way to replace one.
    declared = await asyncio.to_thread(declared_libraries, paths.model_dir(slug))
    current = next((entry for entry in declared if entry.name == name), None)
    if current is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"{slug!r} does not declare a library named {name!r}"
        )
    return await _pin(
        slug,
        name,
        url=current.url,
        ref=body.ref or current.ref,
        catalogue=catalogue,
        libraries=libraries,
        installs=installs,
        checkouts=checkouts,
        events=events,
        replacing=current,
    )


@router.delete(
    "/models/{slug}/libraries/{name}",
    response_model=ModelRecord,
    summary="Remove a library from a model",
    description="The checkout stays on the volume: an older revision may still pin it.",
)
def unpin_library(
    slug: SlugPath, name: LibraryName, catalogue: CatalogueDep, events: EventsDep
) -> ModelRecord:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    try:
        record = catalogue.unpin_library(slug, name)
    except LibraryNotDeclaredError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"{slug!r} does not declare a library named {name!r}"
        ) from None
    except ModelNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    _library_changed(events, slug, name)
    return record


def _library_changed(events: EventBus, slug: str, name: str) -> None:
    emit(events, LibraryChanged(slug=slug, name=name))
    emit(events, ModelEvent(kind="model.updated", slug=slug))


@router.get(
    "/libraries/installed",
    response_model=list[InstalledLibrary],
    summary="Library checkouts on the volume",
    description="Every library checkout on the data volume, with the models whose live "
    "pins read it. One no model uses can be removed.",
)
async def list_installed_libraries(
    catalogue: CatalogueDep, libraries: LibrariesDep
) -> list[InstalledLibrary]:
    def collect() -> list[InstalledLibrary]:
        return [
            InstalledLibrary(
                name=name, commit=commit, used_by=catalogue.library_users(name, commit)
            )
            for name, commit in libraries.installed()
        ]

    # Walks the volume and reads every model.json; off the loop.
    return await asyncio.to_thread(collect)


@router.delete(
    "/libraries/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove a library's checkouts from the volume",
    description=(
        "Deletes the checkout at `commit`, or every checkout of the library. Refused "
        "with a 409 naming the models while any model's live pin still reads one, and "
        "with a 409 naming the jobs while a running render reads one. "
        "Older revisions are not counted: rendering one that pinned a removed checkout "
        "clones it again at that commit, and is a 409 only when that fails."
    ),
)
async def remove_library(
    name: LibraryName,
    catalogue: CatalogueDep,
    libraries: LibrariesDep,
    checkouts: CheckoutsDep,
    events: EventsDep,
    commit: Annotated[
        str | None,
        Query(pattern=COMMIT_PATTERN, description="Only this checkout; every one when omitted"),
    ] = None,
) -> Response:
    what = name if commit is None else f"{name} at {commit[:7]}"
    directory = libraries.paths.libraries / name
    if commit is not None:
        directory /= commit
    # Alone: no pin can find this checkout and record it while it goes, and no
    # render can take a lease on it.
    async with checkouts.removing():
        jobs = checkouts.leased(directory)
        if jobs:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"{what} is being read by render job {', '.join(jobs)}; "
                "try again once it has finished",
                jobs=jobs,
            )
        users = await asyncio.to_thread(catalogue.library_users, name, commit)
        if users:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"{what} is still pinned by {', '.join(users)}; remove it from "
                f"{'that model' if len(users) == 1 else 'those models'} first",
                models=users,
            )
        try:
            removed = await asyncio.to_thread(libraries.remove, name, commit)
        except LibraryCheckoutNotFoundError:
            raise ApiError(
                status.HTTP_404_NOT_FOUND, f"no checkout of {what} is on this volume"
            ) from None
        except LibraryError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    # No model changes -- a removal is refused while one pins it -- so no
    # `model.updated`: only the checkouts on the volume moved.
    emit(events, LibraryRemoved(name=name, commits=removed))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
