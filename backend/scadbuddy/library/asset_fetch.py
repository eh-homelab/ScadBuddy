"""Fetching a file for a `// file` parameter off a URL (#844).

The agent's ``fetch_asset`` tool brings artwork in this way instead of transcribing
paths by hand. The fetch is the URL import's (`url_import.py`): https only on every
hop, public addresses only, pinned to the vetted address, capped in bytes as they
arrive, one deadline for the whole exchange. On top of that, every hop's host must be
on the **asset allowlist** (`asset_fetch_domains` in Settings, which only the user
edits: the agent has no tool for `PUT /settings`). So a redirect off the list is
refused rather than followed, and the refusal names the host that was refused.

One fetch may also let a host off the list through (#959): ``allow_host``, the URL's own
host exactly (not its subdomains or its parent), for that fetch only, redirects within
that host included. The route takes it as ``allow_host`` with a grant (`fetch_grants.py`);
the agent sends it only
from a `fetch_asset` call whose approval said so, and the list itself never changes.

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
#: printables.com 403s; thingiverse.com, a mesh host, was not measured. A person adds
#: a mesh host in Settings. Each entry also allows its subdomains.
#: api.iconify.design (#959) serves the SVGs `search_icons` finds
#: (`library/icon_search.py`, measured there).
DEFAULT_ASSET_FETCH_DOMAINS: Final = (
    "game-icons.net",
    "github.com",
    "raw.githubusercontent.com",
    "openmoji.org",
    "api.iconify.design",
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


def _ascii_host(url: httpx.URL) -> str:
    """The host as it is resolved and connected to: IDNA (punycode) for a non-ASCII
    name. httpx's ``host`` is the Unicode form; ``raw_host`` is what goes on the wire,
    so that is what the allowlist (ASCII entries only, :data:`DOMAIN_RE`) is held to."""
    return url.raw_host.decode("ascii")


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
        # A one-off covers the URL's own host only, never a redirect's.
        + ("" if via else ", or approve fetching from it once (`allow_host`)")
    )


def ascii_host(url: httpx.URL) -> str:
    """The host a fetch of ``url`` is held to: its IDNA form, lower-cased."""
    return _ascii_host(url).lower().rstrip(".")


def parse_fetch_url(pasted: str) -> httpx.URL:
    """``pasted`` as an https URL with a host. Its refusals quote the URL whole, so the
    route makes them before an operation could record them (#1054).

    A backslash, a control character or a user name or password is refused (#959):
    each is where URL parsers disagree on the host, and the host is what the allowlist
    and an approval hold the fetch to, so it must be the one every parser reads."""
    stripped = pasted.strip()
    if _AMBIGUOUS.search(stripped):
        raise ImportRefusedError(
            f"{pasted!r} holds a backslash or a control character, which parsers read "
            "different hosts from; give the plain https URL"
        )
    try:
        url = httpx.URL(stripped)
    except httpx.InvalidURL:
        raise ImportRefusedError(f"{pasted!r} is not a URL") from None
    if url.scheme != "https":
        raise ImportRefusedError(f"only https URLs can be fetched, and {str(url)!r} is not one")
    if not url.host:
        raise ImportRefusedError(f"{pasted!r} names no host")
    if url.userinfo:
        raise ImportRefusedError(
            "the URL names a user or password before its host, which is not fetched; "
            "give the plain https URL"
        )
    return url


#: What makes a URL's host depend on the parser reading it (#959).
_AMBIGUOUS = re.compile(r"[\\\x00-\x1f\x7f]")


async def fetch_file(
    pasted: str,
    *,
    domains: tuple[str, ...] | list[str],
    limit: int,
    allow_host: str | None = None,
) -> FetchedFile:
    """Fetch ``pasted`` from an allowlisted host, or ``allow_host`` (an ASCII host name,
    compared whole), reading at most ``limit`` bytes."""
    url = parse_fetch_url(pasted)

    def allowed(hop: httpx.URL) -> bool:
        host = ascii_host(hop)
        return host == allow_host or host_allowed(host, domains)

    if not allowed(url):
        raise _refuse(url.host)
    hop = url

    async def vet_hop(request: httpx.Request) -> None:
        nonlocal hop
        hop = request.url
        # Before the address is looked up: a host off the list is never resolved.
        if not allowed(request.url):
            raise _refuse(request.url.host, redirected_from=url.host)
        await _vet_hop(request)

    try:
        async with asyncio.timeout(IMPORT_TIMEOUT), public_client(vet_hop) as client:
            data = await _fetch(url, client, limit=limit)
    except (UnreachableError, TimeoutError, httpx.HTTPError):
        raise unreachable(hop.host, redirected_from=url.host) from None
    # From the URL as given, not the last hop: a redirect usually ends on a CDN path
    # whose last segment is a hash. It is a display name only (`display_name`).
    name = unquote(url.path.rstrip("/").rsplit("/", 1)[-1]) or None
    return FetchedFile(data=data, filename=name, source_url=str(url))
