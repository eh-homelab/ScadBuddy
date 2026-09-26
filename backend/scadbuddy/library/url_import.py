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

**Public addresses only.** The pod sits inside the cluster, next to Bambuddy and
whatever else the namespace can reach, and the API has no authentication; an
unrestricted fetch would let any browser that reaches ScadBuddy read or map those
services through it. So a host is refused unless *every* address it resolves to
is globally routable (`is_public`). That is checked in two places, for two reasons:

* in a request hook, on every hop, so the refusal comes before any request is made
  and a redirect into the cluster is never followed;
* in the network backend at connect time, which then connects to the address it
  just vetted instead of letting the socket layer resolve the name again. This is
  the check that holds against DNS rebinding -- a name that answered public to the
  hook and private a moment later. TLS still verifies against the host name, which
  httpcore takes from the URL, not from the address it connected to.

A refused address, a name that does not resolve, a refused connection and a
timeout all read the same (`unreachable`), so the endpoint cannot tell an internal
name that exists from one that does not. What a server answered -- its status, a
web page instead of a file, too large, not text -- is still reported as it is,
because only a vetted public address can have produced it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

import httpcore
import httpx

from scadbuddy.library.scad import NotOpenSCADError, decode_source

IMPORT_TIMEOUT = 30.0

#: A page, not a file. Most often a GitHub `blob/` link pasted instead of its raw
#: one, which would otherwise reach OpenSCAD and fail as a baffling parse error.
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})

#: NAT64's well-known prefix embeds an IPv4 address that `is_global` does not look at.
NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class ImportRefusedError(Exception):
    """The URL could not be imported; the message says why."""


def unreachable(host: str) -> ImportRefusedError:
    """The one answer for every destination that did not answer as a public server."""
    return ImportRefusedError(
        f"could not fetch from {host}: it did not answer, or it is not a public internet address"
    )


def is_public(address: str) -> bool:
    """Globally routable: not loopback, private, link-local (the cloud metadata
    service), CGNAT, unique-local, reserved, unspecified or multicast -- including
    when an IPv4 address arrives inside an IPv6 one."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in NAT64_PREFIX:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


async def resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def _public_addresses(host: str, port: int) -> list[str]:
    try:
        addresses = await resolve_host(host, port)
    except OSError:
        raise unreachable(host) from None
    if not addresses or not all(is_public(address) for address in addresses):
        raise unreachable(host)
    return addresses


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Connects only to a vetted public address, and to exactly that address."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await _public_addresses(host, port)
        return await self._inner.connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _transport() -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport()
    # httpx takes no network backend, so it is set on the pool it built. A test
    # drives a rebinding name through this, so an httpx upgrade that moves the
    # attribute fails that test rather than silently dropping the check.
    transport._pool._network_backend = PublicOnlyBackend()
    return transport


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


async def _vet_hop(request: httpx.Request) -> None:
    # A request hook runs for every hop, so this is also what stops a redirect
    # leaving https or turning back into the cluster.
    _require_https(request.url)
    await _public_addresses(request.url.host, request.url.port or 443)


async def _fetch(url: httpx.URL, client: httpx.AsyncClient, *, limit: int) -> bytes:
    async with client.stream("GET", url) as response:
        if response.is_error:
            raise ImportRefusedError(f"{url.host} answered {response.status_code}")
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
                event_hooks={"request": [_vet_hop]},
                # Its own transport, which also means no proxy from the environment:
                # a proxy would make the connection, and the vetting with it.
                transport=_transport(),
                trust_env=False,
            ) as client,
        ):
            return await resolver.resolve(url, client, limit=limit)
    except (TimeoutError, httpx.HTTPError):
        raise unreachable(url.host) from None
