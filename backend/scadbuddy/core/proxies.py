# backend/scadbuddy/core/proxies.py
"""Who a request is from, behind a proxy the deployment trusts (spec 2026-10-01 §5.2).

A port of the agent's rules in ``agent/src/http/origins.ts`` (``parseCidrList``,
``plainAddress``, ``inBlockList``, ``lastValue``, ``forwardedClient``), with the same
cases in ``tests/test_proxies.py``, so the two services cannot disagree about who a
client is. ``X-Forwarded-For`` is believed only from a peer in
``SCADBUDDY_TRUSTED_PROXIES``, and only its LAST value is taken: the one the nearest
proxy, the trusted peer, appended. Earlier values may have come from the client. One
trusted hop is all the cluster has (Envoy Gateway in front of the pod), so there is no
right-to-left walk over several.

One deliberate difference: the agent reads ``10.0.0.0/`` as ``/0`` (``Number('')`` is
0), which trusts every peer. Here it is refused at start like any other malformed entry.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from typing import Final

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

TRUSTED_PROXIES_VAR: Final = "SCADBUDDY_TRUSTED_PROXIES"
_MAPPED: Final = re.compile(r"^::ffff:(\d+\.\d+\.\d+\.\d+)$", re.IGNORECASE)
#: ``::ffff:0:0/96``, the IPv4-mapped range: an entry inside it names an IPv4 network.
_MAPPED_PREFIX: Final = 96
_PREFIX: Final = re.compile(r"^[0-9]+$")


class ProxyConfigError(ValueError):
    pass


def _network(entry: str) -> Network | None:
    address, *prefixes = entry.split("/")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    prefix = prefixes[0] if prefixes else str(parsed.max_prefixlen)
    if len(prefixes) > 1 or not _PREFIX.match(prefix) or int(prefix) > parsed.max_prefixlen:
        return None
    network = ipaddress.ip_network(f"{parsed}/{prefix}", strict=False)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        # Peers are unwrapped from ``::ffff:a.b.c.d`` before matching (`plain_address`),
        # so a mapped entry is an IPv4 network. Wider than the mapped range (/96) it
        # would name IPv6 space the entry's author did not mean: refused.
        if int(prefix) < _MAPPED_PREFIX:
            return None
        return ipaddress.ip_network(
            f"{parsed.ipv4_mapped}/{int(prefix) - _MAPPED_PREFIX}", strict=False
        )
    return network


def parse_cidr_list(raw: str | None, name: str = TRUSTED_PROXIES_VAR) -> tuple[Network, ...]:
    """``10.0.0.0/8, fd00::/8, 192.168.1.10``: a bare address is a /32 or /128, and host
    bits under the prefix are ignored, as Node's ``BlockList.addSubnet`` ignores them."""
    networks: list[Network] = []
    for entry in (part.strip() for part in (raw or "").split(",")):
        if not entry:
            continue
        network = _network(entry)
        if network is None:
            raise ProxyConfigError(f'{name}: "{entry}" is not an IP address or CIDR range')
        networks.append(network)
    return tuple(networks)


def plain_address(address: str) -> str:
    """Unwraps an IPv4-mapped IPv6 peer (``::ffff:10.0.0.1``), which a dual-stack
    socket reports."""
    mapped = _MAPPED.match(address)
    return mapped.group(1) if mapped else address


def in_networks(networks: Sequence[Network], address: str | None) -> bool:
    if address is None:
        return False
    try:
        parsed = ipaddress.ip_address(plain_address(address))
    except ValueError:
        return False
    # An address of the other family is in no network: `in` answers False, not an error.
    return any(parsed in network for network in networks)


def last_value(value: str | None) -> str | None:
    """The last comma-separated value of a header, trimmed: the one the nearest proxy
    added. Empty is none."""
    if value is None:
        return None
    return value.split(",")[-1].strip() or None


def forwarded_client(
    peer: str | None, forwarded_for: str | None, trusted: Sequence[Network]
) -> str | None:
    """The client a trusted proxy names; None from any other peer, whose header is not
    believed."""
    return last_value(forwarded_for) if in_networks(trusted, peer) else None


def client_address(
    peer: str | None, forwarded_for: str | None, trusted: Sequence[Network]
) -> str | None:
    """Who the request is from: the client a trusted proxy names, else the peer itself.
    A missing or empty last value from a trusted proxy falls back to the peer."""
    return forwarded_client(peer, forwarded_for, trusted) or peer


__all__ = [
    "TRUSTED_PROXIES_VAR",
    "Network",
    "ProxyConfigError",
    "client_address",
    "forwarded_client",
    "in_networks",
    "last_value",
    "parse_cidr_list",
    "plain_address",
]
