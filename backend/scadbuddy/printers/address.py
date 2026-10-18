"""Where the LAN client may connect (#3823; the security review of plan Task 3).

A Bambu printer in LAN mode is on the local network, so ScadBuddy dials only a
private address: RFC 1918 (10/8, 172.16/12, 192.168/16), CGNAT 100.64/10 (RFC 6598)
and IPv6 unique local fc00::/7 (RFC 4193). Loopback, link-local (169.254/16, which
holds cloud metadata services, and fe80::/10), unspecified, multicast and broadcast
are refused even where the ``ipaddress`` module calls them private. A host is an IP
literal or an RFC 1123 name; a name is resolved and every address it resolves to must
pass, and the caller connects to the address checked here, never to the name, so a
second resolution cannot answer differently (DNS rebinding). `lan_address` is the one
check, run at every connect: the link's probe, the watch, the upload and the print.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Any

#: RFC 1123 §2.1: labels of letters, digits and hyphens, neither starting nor ending
#: with a hyphen, at most 63 characters each and 253 in all.
_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")

_ALLOWED = tuple(
    ipaddress.ip_network(net)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")
)
_BROADCAST = ipaddress.ip_address("255.255.255.255")

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class UnsafeAddressError(ValueError):
    """The host is not a LAN printer's: never dialled. The message names no address."""

    def __init__(self) -> None:
        super().__init__(
            "the printer's address must be an IP address or host name on the local network"
            " (a private address: 10/8, 172.16/12, 192.168/16, 100.64/10 or fc00::/7)"
        )


def _ip(host: str) -> Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def valid_host(host: str) -> bool:
    """An IP literal or an RFC 1123 host name; nothing with a scheme, port or path."""
    if _ip(host) is not None:
        return True
    if not host or len(host) > 253 or not host.isascii():
        return False
    return all(_LABEL.match(label) for label in host.removesuffix(".").split("."))


def allowed(ip: Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip == _BROADCAST
    ):
        return False
    return any(ip in net for net in _ALLOWED if net.version == ip.version)


async def _getaddrinfo(host: str, port: Any, **kwargs: Any) -> list[Any]:
    return await asyncio.get_running_loop().getaddrinfo(host, port, **kwargs)


async def lan_address(host: str) -> str:
    """The address to dial for ``host``: ``host`` itself when it is an allowed IP
    literal, else the first address the name resolves to once every one is allowed.
    Raises `UnsafeAddressError`, or ``OSError`` when the name does not resolve."""
    if not valid_host(host):
        raise UnsafeAddressError()
    literal = _ip(host)
    if literal is not None:
        if not allowed(literal):
            raise UnsafeAddressError()
        return host
    infos = await _getaddrinfo(host, None, type=socket.SOCK_STREAM)
    ips = [str(info[4][0]).split("%", 1)[0] for info in infos]
    parsed = [_ip(ip) for ip in ips]
    if not parsed or any(ip is None or not allowed(ip) for ip in parsed):
        raise UnsafeAddressError()
    return ips[0]
