"""Searching open icon catalogues for artwork to fetch (#959, #844's `search_icons`).

One search, through Iconify's public API, over the catalogues #844 named: Material
Design Icons, Tabler, game-icons and OpenMoji. Each icon comes back with its
catalogue's licence and an SVG URL on `api.iconify.design`, which is on the default
asset allowlist, so `fetch_asset` can bring it in after its approval.

Measured 2026-11-03 with curl: ``GET https://api.iconify.design/search?query=unicorn
&limit=32&prefixes=mdi,tabler,game-icons,openmoji`` answers 200 JSON, ``{"icons":
["mdi:unicorn", ...], "total", "limit", "start", "collections": {prefix: {"name",
"author": {"name", "url"}, "license": {"title", "spdx", "url"}, ...}}}``; a
``limit`` under 32 still answers 32 icons, and an empty ``query`` answers 400.
``GET https://api.iconify.design/game-icons/unicorn.svg`` answers ``image/svg+xml``.
The API is documented at https://iconify.design/docs/api/search.html.

The request is the URL import's (`url_import.py`): https only, public addresses only,
capped in bytes and in time. It goes to Iconify's host and nowhere else: a redirect is
refused rather than followed. What comes back is data: only names of Iconify's own
shape are kept, and only those are put in a URL.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Final

import httpx
from pydantic import BaseModel

from scadbuddy.library.asset_fetch import host_allowed
from scadbuddy.library.url_import import (
    IMPORT_TIMEOUT,
    ImportRefusedError,
    UnreachableError,
    _fetch,
    _vet_hop,
    public_client,
    unreachable,
)

ICONIFY_HOST: Final = "api.iconify.design"
SEARCH_URL: Final = f"https://{ICONIFY_HOST}/search"
#: The catalogues searched, by Iconify prefix (#844's list).
COLLECTIONS: Final = ("mdi", "tabler", "game-icons", "openmoji")
#: Iconify's own floor for ``limit`` (measured above); fewer are cut here.
ICONIFY_MIN_LIMIT: Final = 32
MAX_LIMIT: Final = 64
#: A 64-icon answer measured about 4 KB; this leaves room and still bounds a bad one.
MAX_ANSWER_BYTES: Final = 256 * 1024
#: An Iconify icon name: lower-case letters and digits in hyphen-separated runs
#: (https://iconify.design/docs/icons/icon-basics.html#naming).
_NAME_RE: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class IconSearchError(ImportRefusedError):
    """The catalogue could not be searched."""


class IconLicence(BaseModel):
    #: The SPDX identifier, when the catalogue gives one.
    spdx: str | None = None
    title: str | None = None
    url: str | None = None


class Icon(BaseModel):
    #: ``<collection>:<name>``, Iconify's id.
    id: str
    name: str
    collection: str
    collection_name: str | None = None
    author: str | None = None
    licence: IconLicence
    #: The SVG, for `fetch_asset`.
    url: str
    #: Whether :attr:`url`'s host is on the asset allowlist, so `fetch_asset` takes it.
    allowlisted: bool


class IconSearch(BaseModel):
    icons: list[Icon]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _icons(answer: Any, *, limit: int, domains: tuple[str, ...] | list[str]) -> list[Icon]:
    if not isinstance(answer, dict):
        raise IconSearchError(f"{ICONIFY_HOST} answered something that is not a search result")
    collections = answer.get("collections")
    collections = collections if isinstance(collections, dict) else {}
    allowed = host_allowed(ICONIFY_HOST, domains)
    icons: list[Icon] = []
    for entry in answer.get("icons") or []:
        if len(icons) == limit:
            break
        prefix, _, name = entry.partition(":") if isinstance(entry, str) else ("", "", "")
        if prefix not in COLLECTIONS or not _NAME_RE.match(name):
            continue
        info = collections.get(prefix)
        info = info if isinstance(info, dict) else {}
        licence = info.get("license")
        licence = licence if isinstance(licence, dict) else {}
        author = info.get("author")
        icons.append(
            Icon(
                id=f"{prefix}:{name}",
                name=name,
                collection=prefix,
                collection_name=_text(info.get("name")),
                author=_text(author.get("name")) if isinstance(author, dict) else None,
                licence=IconLicence(
                    spdx=_text(licence.get("spdx")),
                    title=_text(licence.get("title")),
                    url=_text(licence.get("url")),
                ),
                url=f"https://{ICONIFY_HOST}/{prefix}/{name}.svg",
                allowlisted=allowed,
            )
        )
    return icons


async def search_icons(
    query: str, *, limit: int, domains: tuple[str, ...] | list[str]
) -> IconSearch:
    """At most ``limit`` icons matching ``query``, each held to ``domains``."""
    url = httpx.URL(
        SEARCH_URL,
        params={
            "query": query,
            "limit": max(limit, ICONIFY_MIN_LIMIT),
            "prefixes": ",".join(COLLECTIONS),
        },
    )

    async def vet_hop(request: httpx.Request) -> None:
        if request.url.raw_host.decode("ascii") != ICONIFY_HOST:
            raise IconSearchError(f"{ICONIFY_HOST} redirected elsewhere, which is not followed")
        await _vet_hop(request)

    try:
        async with asyncio.timeout(IMPORT_TIMEOUT), public_client(vet_hop) as client:
            data = await _fetch(url, client, limit=MAX_ANSWER_BYTES)
    except (UnreachableError, TimeoutError, httpx.HTTPError):
        raise unreachable(ICONIFY_HOST) from None
    try:
        answer = json.loads(data)
    except ValueError:
        raise IconSearchError(f"{ICONIFY_HOST} answered something that is not JSON") from None
    return IconSearch(icons=_icons(answer, limit=limit, domains=domains))
