"""Pulling a model's source off a URL, for `POST /models/import` (#153).

A URL is handed to the first resolver that claims it; the direct one claims
everything, so it goes last. A resolver only fetches and names the source: the
slug, the parse check and the schema are the create path's, exactly as for an
upload.

**MakerWorld is refused, not resolved.** Its model metadata is public
(``api.bambulab.com/v1/design-service/design/<id>`` answers without a login), but
every route that serves the files -- ``design/<id>/model`` and the instance 3MF --
answers ``403 {"error": "Please log in to download models."}``, and the model
pages themselves sit behind a Cloudflare challenge. Measured 2026-09-26. Until
there is a signed-in resolver (#174) the refusal says how to get the file in by hand.

The fetch is https only -- redirects included -- capped in bytes, and bounded by
one deadline for the whole exchange, so a server that drips its body cannot hold
the request open past it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import httpx

from scadbuddy.library.scad import NotOpenSCADError, decode_source

IMPORT_TIMEOUT = 30.0

#: A page, not a file. Most often a GitHub `blob/` link pasted instead of its raw
#: one, which would otherwise reach OpenSCAD and fail as a baffling parse error.
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})


class ImportRefusedError(Exception):
    """The URL is not one that can be imported; the message says why."""


class SourceUnreachableError(Exception):
    """The URL could not be fetched: no answer, an error status, or out of time."""

    def __init__(self, message: str, *, timed_out: bool = False) -> None:
        super().__init__(message)
        self.timed_out = timed_out


@dataclass(frozen=True)
class ImportedModel:
    name: str
    source: str
    #: The URL as it was pasted, not wherever redirects ended: that is the one to
    #: link back to and to pull again.
    origin_url: str


class Resolver(Protocol):
    def handles(self, url: httpx.URL) -> bool: ...

    async def resolve(
        self, url: httpx.URL, client: httpx.AsyncClient, *, limit: int
    ) -> ImportedModel: ...


class MakerWorldResolver:
    def handles(self, url: httpx.URL) -> bool:
        return url.host == "makerworld.com" or url.host.endswith(".makerworld.com")

    async def resolve(
        self, url: httpx.URL, client: httpx.AsyncClient, *, limit: int
    ) -> ImportedModel:
        raise ImportRefusedError(
            "MakerWorld only serves a model's files to a signed-in account, so ScadBuddy "
            "cannot fetch them. Download the .scad from the model page and use Upload, "
            "or paste a link to the raw file instead."
        )


class DirectResolver:
    """Any URL that answers with the source itself: GitHub raw, a gist's raw, a file
    on a web server."""

    def handles(self, url: httpx.URL) -> bool:
        return True

    async def resolve(
        self, url: httpx.URL, client: httpx.AsyncClient, *, limit: int
    ) -> ImportedModel:
        raw = await _fetch(url, client, limit=limit)
        try:
            source = decode_source(raw)
        except NotOpenSCADError:
            raise ImportRefusedError(
                f"{url.host} did not answer with UTF-8 text, so it is not OpenSCAD source"
            ) from None
        return ImportedModel(name=_name_from(url), source=source, origin_url=str(url))


RESOLVERS: tuple[Resolver, ...] = (MakerWorldResolver(), DirectResolver())


def _name_from(url: httpx.URL) -> str:
    """The file's name without `.scad`, or the host when the path has none."""
    stem = url.path.rstrip("/").rsplit("/", 1)[-1]
    if stem.lower().endswith(".scad"):
        stem = stem[: -len(".scad")]
    return stem.strip() or url.host


def _require_https(url: httpx.URL) -> None:
    if url.scheme != "https":
        raise ImportRefusedError(f"only https URLs can be imported, and {str(url)!r} is not one")


async def _refuse_downgrades(request: httpx.Request) -> None:
    # A request hook runs for every hop, so this is also what stops a redirect
    # leaving https.
    _require_https(request.url)


async def _fetch(url: httpx.URL, client: httpx.AsyncClient, *, limit: int) -> bytes:
    async with client.stream("GET", url) as response:
        if response.is_error:
            raise SourceUnreachableError(f"{url.host} answered {response.status_code}")
        kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind in HTML_TYPES:
            raise ImportRefusedError(
                f"{url.host} answered with a web page, not a file; link to the raw .scad instead"
            )
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise ImportRefusedError(
                    f"the file is larger than {limit} bytes, which is the most an import reads"
                )
        return bytes(body)


async def fetch_model(pasted: str, *, limit: int) -> ImportedModel:
    """Resolve and fetch the model at `pasted`, reading at most `limit` bytes of it."""
    try:
        url = httpx.URL(pasted.strip())
    except httpx.InvalidURL:
        raise ImportRefusedError(f"{pasted!r} is not a URL") from None
    _require_https(url)
    if not url.host:
        raise ImportRefusedError(f"{pasted!r} names no host")
    resolver = next(candidate for candidate in RESOLVERS if candidate.handles(url))
    try:
        async with (
            asyncio.timeout(IMPORT_TIMEOUT),
            httpx.AsyncClient(
                timeout=IMPORT_TIMEOUT,
                follow_redirects=True,
                event_hooks={"request": [_refuse_downgrades]},
            ) as client,
        ):
            return await resolver.resolve(url, client, limit=limit)
    except (TimeoutError, httpx.TimeoutException):
        raise SourceUnreachableError(
            f"{url.host} did not answer within {IMPORT_TIMEOUT:.0f} seconds", timed_out=True
        ) from None
    except httpx.HTTPError as error:
        raise SourceUnreachableError(
            f"could not reach {url.host}: {type(error).__name__}"
        ) from None
