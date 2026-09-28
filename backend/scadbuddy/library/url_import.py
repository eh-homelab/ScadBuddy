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

The fetch is https only -- redirects included -- capped in bytes as they arrive
(uncompressed: a compressed reply is refused, not inflated), and bounded by one
deadline for the whole exchange, so a server that drips its body cannot hold the
request open past it.

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
name that exists from one that does not. After a redirect it names the hop that
failed and the pasted host it came from, in that same one text. What a server
answered -- its status, a web page instead of a file, too large, not text -- is
still reported as it is, because only a vetted public address can have produced it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

import httpcore
import httpx

from scadbuddy.library.scad import NotOpenSCADError, decode_source

IMPORT_TIMEOUT = 30.0

#: A page, not a file. Most often a GitHub `blob/` link pasted instead of its raw
#: one, which would otherwise reach OpenSCAD and fail as a baffling parse error.
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})

#: IPv6 forms whose low 32 bits are an IPv4 address that `is_global` does not look
#: at: NAT64's well-known prefix, and the deprecated IPv4-compatible ``::a.b.c.d``.
EMBEDDED_IPV4_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("::/96"),
)


class ImportRefusedError(Exception):
    """The URL could not be imported; the message says why."""


class ResolverUnavailableError(Exception):
    """The lookup did not finish -- every resolver thread was busy, or it ran past
    `RESOLVE_TIMEOUT` -- so it says nothing about the host either way."""


class ResolverBusyError(ResolverUnavailableError):
    """Every resolver thread on this replica was busy, so the lookup never started.

    Decided before the host is looked up, so unlike a timeout it cannot depend on
    the host: an import may say it (a retryable 503) instead of the refusal. Library
    installs vet their clone URLs on the same threads, so they can cause it too.
    """


class UnreachableError(ImportRefusedError):
    """What :func:`unreachable` raises, so `fetch_model` can name the hop it was on."""


def unreachable(host: str, *, redirected_from: str | None = None) -> UnreachableError:
    """The one answer for every destination that did not answer as a public server.

    ``redirected_from`` is the pasted URL's host when a redirect led to ``host``, so
    the error names the hop that failed rather than the one that was pasted (#178).
    The text is the same whether that hop was refused or did not answer.
    """
    via = f" (redirected from {redirected_from})" if redirected_from not in (None, host) else ""
    return UnreachableError(
        f"could not fetch from {host}{via}: it did not answer, or it is not a public "
        "internet address"
    )


def is_public(address: str) -> bool:
    """Globally routable: not loopback, private, link-local (the cloud metadata
    service), CGNAT, unique-local, reserved, unspecified or multicast -- including
    when an IPv4 address arrives inside an IPv6 one."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
        elif any(ip in prefix for prefix in EMBEDDED_IPV4_PREFIXES):
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


#: `getaddrinfo` blocks a thread and cannot be cancelled, so a slow lookup holds its
#: thread whatever `RESOLVE_TIMEOUT` says. Its own threads, not the loop's default
#: executor, so that can only ever stall other imports -- never the git calls and
#: health checks that share the default one.
#:
#: The threads do come back: glibc's resolver gives up by itself, after resolv.conf's
#: `timeout` (default 5 s) x `attempts` (default 2) per nameserver, for each name it
#: tries from the search list. Under Kubernetes' `ndots:5` and a few search domains
#: that adds up to tens of seconds, not forever -- but long enough that two such
#: lookups could hold both threads for a while. So a lookup that finds both busy is
#: refused at once rather than queued behind them (`_RESOLVER_SLOTS`).
RESOLVER_THREADS = 2
_RESOLVER = ThreadPoolExecutor(max_workers=RESOLVER_THREADS, thread_name_prefix="import-dns")
#: Taken on the loop before a lookup is submitted, given back by the worker thread
#: when `getaddrinfo` actually returns -- not when the awaiting import gives up --
#: so it counts threads that are really busy.
_RESOLVER_SLOTS = threading.BoundedSemaphore(RESOLVER_THREADS)

#: Well inside `IMPORT_TIMEOUT`, so a slow resolver leaves the fetch its time.
RESOLVE_TIMEOUT = 10.0

#: Hops followed after the first request. Raw links redirect once or twice at most.
MAX_REDIRECTS = 5


def _getaddrinfo(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    finally:
        _RESOLVER_SLOTS.release()
    return [str(info[4][0]) for info in infos]


async def resolve_host(host: str, port: int) -> list[str]:
    if not _RESOLVER_SLOTS.acquire(blocking=False):
        raise ResolverBusyError(f"could not resolve {host}: every resolver thread is busy")
    return await asyncio.get_running_loop().run_in_executor(_RESOLVER, _getaddrinfo, host, port)


async def public_addresses(host: str, port: int, *, tell_unavailable: bool = False) -> list[str]:
    """Every address ``host`` resolves to, or :func:`unreachable` unless all are public.

    Shared with the library clones (#93), which vet a user-added git URL the same way.
    They pass ``tell_unavailable``: a lookup that did not finish (the threads busy,
    or a timeout) is then :class:`ResolverUnavailableError` rather than the refusal,
    so an install can say "try again" instead of calling the host private (#205).
    An import keeps the refusal for a timeout, which could depend on the host. Busy
    threads (:class:`ResolverBusyError`) are raised as they are for both: the lookup
    never started, so an import can answer that with its retryable 503 too.
    """
    try:
        addresses = await asyncio.wait_for(resolve_host(host, port), RESOLVE_TIMEOUT)
    except TimeoutError:
        if tell_unavailable:
            raise ResolverUnavailableError(f"could not resolve {host} in time") from None
        raise unreachable(host) from None
    except ResolverBusyError:
        raise
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
        addresses = await public_addresses(host, port)
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
    """httpx's transport over a pool that connects through `PublicOnlyBackend`.

    httpx takes no network backend, but httpcore's pool does, publicly -- so the
    pool is built here and swapped for the one httpx made. The swap is the one
    private step, and it is checked: if httpx stops keeping its pool in `_pool`,
    this raises rather than leave a transport that connects through a backend
    that resolves the name again.
    """
    transport = httpx.AsyncHTTPTransport()
    if not isinstance(getattr(transport, "_pool", None), httpcore.AsyncConnectionPool):
        raise RuntimeError(
            "httpx no longer keeps its connection pool where PublicOnlyBackend is fitted"
        )
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(), network_backend=PublicOnlyBackend()
    )
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
        host = url.host.rstrip(".")  # a fully qualified "makerworld.com." is the same host
        return host == "makerworld.com" or host.endswith(".makerworld.com")

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
    await public_addresses(request.url.host, request.url.port or 443)


async def _fetch(url: httpx.URL, client: httpx.AsyncClient, *, limit: int) -> bytes:
    """Follow redirects by hand, reading only the last response, and that capped.

    Not httpx's `follow_redirects`: it reads every redirect's body in full
    (`await response.aread()` in `_send_handling_redirects`, httpx 0.28), with no
    cap, before it moves on. Here a redirect is closed unread. Each hop still
    passes the request hook, so https and the public-address check apply to it.
    """
    request = client.build_request("GET", url)
    for _ in range(MAX_REDIRECTS + 1):
        response = await client.send(request, stream=True)
        try:
            if response.next_request is None:
                return await _read_capped(response, limit=limit)
            request = response.next_request
        finally:
            await response.aclose()
    raise ImportRefusedError(
        f"{url.host} redirected more than {MAX_REDIRECTS} times, so the file was not fetched"
    )


async def _read_capped(response: httpx.Response, *, limit: int) -> bytes:
    host = response.url.host
    if response.is_error:
        raise ImportRefusedError(f"{host} answered {response.status_code}")
    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind in HTML_TYPES:
        raise ImportRefusedError(
            f"{host} answered with a web page, not a file; link to the raw .scad instead"
        )
    # Asked for `identity`, so an encoding here is the server insisting. It is
    # refused rather than inflated: a few KB of gzip can decode to gigabytes in
    # one step, well before a cap on the decoded bytes gets to look.
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise ImportRefusedError(
            f"{host} sent the file compressed after being asked not to, so it was not read"
        )
    body = bytearray()
    # Raw, so the cap counts what arrives rather than what it decodes to.
    async for chunk in response.aiter_raw():
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
    # The hop in flight, so whatever stops it -- a refused address, no answer, the
    # deadline -- names the host that failed, not only the one that was pasted.
    hop = url

    async def vet_hop(request: httpx.Request) -> None:
        nonlocal hop
        hop = request.url
        await _vet_hop(request)

    try:
        async with (
            asyncio.timeout(IMPORT_TIMEOUT),
            httpx.AsyncClient(
                timeout=IMPORT_TIMEOUT,
                # `_fetch` follows them itself; see why there.
                follow_redirects=False,
                event_hooks={"request": [vet_hop]},
                headers={"Accept-Encoding": "identity"},
                # Its own transport, which also means no proxy from the environment:
                # a proxy would make the connection, and the vetting with it.
                transport=_transport(),
                trust_env=False,
            ) as client,
        ):
            return await resolver.resolve(url, client, limit=limit)
    except (UnreachableError, TimeoutError, httpx.HTTPError):
        raise unreachable(hop.host, redirected_from=url.host) from None
