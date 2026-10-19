"""Where the LAN client may connect (security review of #3823 Task 3): a printer is on
the LAN, so only a private address is dialled, checked at every connect."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from scadbuddy.printers import address
from scadbuddy.printers.address import (
    UnsafeAddressError,
    lan_address,
    valid_host,
)


@pytest.mark.parametrize(
    "host",
    ["10.0.0.9", "172.16.4.2", "192.168.1.20", "100.64.0.7", "fd12:3456::9", "printer.lan", "h2c"],
)
def test_a_host_is_an_ip_literal_or_an_rfc_1123_name(host: str) -> None:
    assert valid_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "",
        "http://10.0.0.9",
        "10.0.0.9:8883",
        "-bad.lan",
        "bad-.lan",
        "a..b",
        "a b",
        "x" * 64,
        "ü.lan",
    ],
)
def test_anything_else_is_not_a_host(host: str) -> None:
    assert not valid_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "fe80::1",
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "255.255.255.255",
        "8.8.8.8",
        "2001:4860:4860::8888",
        "::ffff:127.0.0.1",
    ],
)
async def test_refused_addresses(host: str) -> None:
    with pytest.raises(UnsafeAddressError):
        await lan_address(host)


@pytest.mark.parametrize("host", ["10.0.0.9", "192.168.1.20", "100.64.0.7", "fd12::9"])
async def test_a_private_address_is_dialled_as_it_is(host: str) -> None:
    assert await lan_address(host) == host


def _resolving(*ips: str) -> Any:
    async def getaddrinfo(host: str, port: Any, **_: Any) -> list[Any]:
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
            for ip in ips
        ]

    return getaddrinfo


async def test_a_name_resolving_to_loopback_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(address, "_getaddrinfo", _resolving("127.0.0.1"))
    with pytest.raises(UnsafeAddressError):
        await lan_address("printer.lan")


async def test_every_resolved_address_must_be_private(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(address, "_getaddrinfo", _resolving("192.168.1.20", "8.8.8.8"))
    with pytest.raises(UnsafeAddressError):
        await lan_address("printer.lan")


async def test_a_private_name_gives_the_checked_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(address, "_getaddrinfo", _resolving("192.168.1.20", "192.168.1.21"))
    assert await lan_address("printer.lan") == "192.168.1.20"


async def test_a_name_that_does_not_resolve_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fails(host: str, port: Any, **_: Any) -> list[Any]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(address, "_getaddrinfo", fails)
    with pytest.raises(OSError):
        await lan_address("printer.lan")


async def test_a_malformed_host_is_refused() -> None:
    with pytest.raises(UnsafeAddressError):
        await lan_address("http://10.0.0.9")
