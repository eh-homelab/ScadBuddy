"""Fetching a file for a `// file` parameter off a URL (#844).

The agent's ``fetch_asset`` tool brings artwork in this way instead of transcribing
paths by hand. The fetch is the URL import's (`url_import.py`): https only on every
hop, public addresses only, pinned to the vetted address, capped in bytes as they
arrive, one deadline for the whole exchange. On top of that, every hop's host must be
on the **asset allowlist** (`asset_fetch_domains` in Settings, which only the user
edits: the agent has no tool for `PUT /settings`, and its headless browser is refused
it). So a redirect off the list is refused rather than followed, and the refusal names
the host that was refused.

What arrives is stored exactly as an upload is (`AssetStore.put`): sniffed rather than
trusted by its name, an SVG stripped of scripts and external references, a PNG
re-encoded. The bytes are data; nothing in them is acted on.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import unquote

import httpx

from scadbuddy.library.url_import import (
    IMPORT_TIMEOUT,
    ImportRefusedError,
    UnreachableError,
    _fetch,
    _vet_hop,
    public_client,
    unreachable,
)

#: Where the allowlist starts: the issue's suggested starters that answer a direct
#: file URL with the file. Measured 2026-10-01 with an httpx user agent:
#: game-icons.net, openmoji.org and raw.githubusercontent.com answer 200 with
#: ``image/svg+xml``; a github.com ``/raw/`` link 302s to raw.githubusercontent.com.
#: Left out: svgrepo.com answers 429 with a "Vercel Security Checkpoint" page,
#: printables.com 403s, and printables.com and thingiverse.com serve meshes, which the
#: asset store does not take yet (#959). Each entry also allows its subdomains.
DEFAULT_ASSET_FETCH_DOMAINS: Final = (
    "game-icons.net",
    "github.com",
    "raw.githubusercontent.com",
    "openmoji.org",
)

#: A host name of at least two labels, each a letter-digit-hyphen label, ending in a
#: top-level label that starts with a letter, so an IPv4 literal is not one. No
#: scheme, port, path or wildcard: an entry already allows its subdomains.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_TOP_LABEL = r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN_RE: Final = re.compile(rf"^(?=.{{1,253}}$)(?:{_LABEL}\.)+{_TOP_LABEL}$")


def normalise_domain(entry: str) -> str:
    """``entry`` lower-cased without a trailing dot, or ``ValueError`` if it is not a
    domain."""
    domain = entry.strip().lower().rstrip(".")
    if not DOMAIN_RE.match(domain):
        raise ValueError(
            f"{entry!r} is not a domain name (like openmoji.org; it also allows its subdomains)"
        )
    return domain


def host_allowed(host: str, domains: tuple[str, ...] | list[str]) -> bool:
    """``host`` is one of ``domains`` or a subdomain of one, compared label-wise."""
    host = host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


class AssetFetchRefusedError(ImportRefusedError):
    """The host is not on the allowlist."""


@dataclass(frozen=True)
class FetchedFile:
    data: bytes
    #: The last path segment, for the asset's display name only.
    filename: str | None
    #: As given, not wherever redirects ended: the one to credit and fetch again.
    source_url: str


def _refuse(host: str, *, redirected_from: str | None = None) -> AssetFetchRefusedError:
    via = f" (redirected from {redirected_from})" if redirected_from not in (None, host) else ""
    return AssetFetchRefusedError(
        f"{host}{via} is not on the asset allowlist, so it was not fetched; the user can "
        "add its domain in Settings"
    )


async def fetch_file(
    pasted: str, *, domains: tuple[str, ...] | list[str], limit: int
) -> FetchedFile:
    """Fetch ``pasted`` from an allowlisted host, reading at most ``limit`` bytes."""
    try:
        url = httpx.URL(pasted.strip())
    except httpx.InvalidURL:
        raise ImportRefusedError(f"{pasted!r} is not a URL") from None
    if url.scheme != "https":
        raise ImportRefusedError(f"only https URLs can be fetched, and {str(url)!r} is not one")
    if not url.host:
        raise ImportRefusedError(f"{pasted!r} names no host")
    if not host_allowed(url.host, domains):
        raise _refuse(url.host)
    hop = url

    async def vet_hop(request: httpx.Request) -> None:
        nonlocal hop
        hop = request.url
        # Before the address is looked up: a host off the list is never resolved.
        if not host_allowed(request.url.host, domains):
            raise _refuse(request.url.host, redirected_from=url.host)
        await _vet_hop(request)

    try:
        async with asyncio.timeout(IMPORT_TIMEOUT), public_client(vet_hop) as client:
            data = await _fetch(url, client, limit=limit)
    except (UnreachableError, TimeoutError, httpx.HTTPError):
        raise unreachable(hop.host, redirected_from=url.host) from None
    name = unquote(url.path.rstrip("/").rsplit("/", 1)[-1]) or None
    return FetchedFile(data=data, filename=name, source_url=str(url))
