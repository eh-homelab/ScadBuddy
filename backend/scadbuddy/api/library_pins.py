"""Resolving a library to a pin (#93), shared by ``PUT /models/{slug}/libraries/{name}``
and the create routes that take ``libraries`` (#169).

Its own module because the library routes import the model routes' guards, so the
model routes cannot import the library routes back.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Iterable, Sequence

from fastapi import status

from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import ModelMeta
from scadbuddy.library.libraries import (
    NAME_PATTERN,
    CheckoutGate,
    LibraryError,
    LibraryFetchError,
    LibraryNotFoundError,
    LibraryResolverUnavailableError,
    LibraryStore,
    ModelLibrary,
)


async def resolve_pin(
    name: str,
    *,
    url: str | None,
    ref: str | None,
    libraries: LibraryStore,
    installs: asyncio.Semaphore,
) -> ModelLibrary:
    """Clone ``name`` at ``ref`` and return the pin. The caller holds
    :meth:`CheckoutGate.pinning` until the pin is recorded."""
    try:
        # A clone is a network fetch; off the loop, and a bounded number at a time.
        async with installs:
            return await asyncio.to_thread(libraries.resolve, name, url=url, ref=ref)
    except LibraryNotFoundError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"{name!r} is not in the catalogue; give a url to pin it from",
        ) from None
    except LibraryFetchError as error:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, str(error)) from None
    except LibraryResolverUnavailableError as error:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from None
    except LibraryError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None


def require_library_names(names: Iterable[str]) -> None:
    """A 422 naming each of ``names`` that is not a usable library name.

    The one refusal for a malformed name, whichever body carried it (#437): the
    multipart form's fields are checked here as given, and the JSON body's pattern
    mismatches are routed here from its validation error.
    """
    malformed = [name for name in dict.fromkeys(names) if not re.fullmatch(NAME_PATTERN, name)]
    if malformed:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"not a usable library name: {', '.join(repr(name) for name in malformed)}",
            libraries=malformed,
        )


@contextlib.asynccontextmanager
async def pinned_at_create(
    names: Sequence[str],
    meta: ModelMeta,
    *,
    libraries: LibraryStore,
    installs: asyncio.Semaphore,
    checkouts: CheckoutGate,
) -> AsyncIterator[ModelMeta]:
    """``meta`` with the curated ``names`` pinned at the catalogue's ref, for a create
    to record in its first commit and parse-check against (#169).

    Every name is checked against the catalogue before anything is cloned; one the
    ``meta`` already pins (a dropped ``model.json``) keeps that pin. The gate is held
    until the block exits, so no removal deletes a checkout before the create has
    recorded it.
    """
    wanted = [
        name for name in dict.fromkeys(names) if all(pin.name != name for pin in meta.libraries)
    ]
    require_library_names(wanted)
    if not wanted:
        yield meta
        return
    unknown = [name for name in wanted if name not in libraries.catalogue]
    if unknown:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"not in the library catalogue: {', '.join(unknown)}; "
            "pin another library by URL once the model is created",
            libraries=unknown,
        )
    async with checkouts.pinning():
        pins = [
            await resolve_pin(name, url=None, ref=None, libraries=libraries, installs=installs)
            for name in wanted
        ]
        yield meta.model_copy(update={"libraries": [*meta.libraries, *pins]})
