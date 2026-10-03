# backend/tests/test_proxies.py
"""core/proxies.py: the agent's forwarded-client rules, ported (spec 2026-10-01 §5.2).

The cases mirror ``agent/test/origins.test.ts`` ("trusted proxies") and
``agent/test/mcpAuthMode.test.ts`` (the client a proxy names), so the backend and the
agent agree about who a client is."""

from __future__ import annotations

import ipaddress

import pytest
from pydantic import ValidationError

from scadbuddy.core.proxies import (
    ProxyConfigError,
    client_address,
    forwarded_client,
    in_networks,
    last_value,
    parse_cidr_list,
)
from scadbuddy.core.settings import BOOTSTRAP_FIELDS, ENV_SEEDED, Settings
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS

INGRESS = "10.42.0.0/16"


def test_addresses_and_ranges_of_both_families_parse() -> None:
    networks = parse_cidr_list("10.42.0.0/16, 192.168.1.10 ,fd00::/8")
    assert in_networks(networks, "10.42.9.9")
    assert not in_networks(networks, "10.43.0.1")
    assert in_networks(networks, "192.168.1.10")
    assert not in_networks(networks, "192.168.1.11")
    assert in_networks(networks, "fd12::1")


def test_host_bits_under_the_prefix_are_ignored() -> None:
    assert parse_cidr_list("10.42.9.9/16") == (ipaddress.ip_network("10.42.0.0/16"),)


def test_empty_trusts_no_one() -> None:
    assert parse_cidr_list("") == ()
    assert parse_cidr_list(None) == ()
    assert parse_cidr_list(" , ") == ()


@pytest.mark.parametrize(
    "entry",
    ["10.0.0.0/33", "fd00::/129", "not-an-ip", "10.0.0.0/8/1", "10.0.0.0/abc", "10.0.0.0/", "/8"],
)
def test_a_malformed_entry_is_refused_by_name(entry: str) -> None:
    with pytest.raises(ProxyConfigError, match=f'SCADBUDDY_TRUSTED_PROXIES: "{entry}"'):
        parse_cidr_list(f"10.42.0.0/16, {entry}")


def test_an_ipv4_mapped_peer_is_matched_as_ipv4() -> None:
    assert in_networks(parse_cidr_list(INGRESS), "::ffff:10.42.0.5")
    assert in_networks(parse_cidr_list(INGRESS), "::FFFF:10.42.0.5")


def test_a_peer_that_is_not_an_address_is_never_trusted() -> None:
    networks = parse_cidr_list(INGRESS)
    assert not in_networks(networks, None)
    assert not in_networks(networks, "testclient")
    assert not in_networks(networks, "")


def test_the_last_value_is_taken_trimmed_and_empty_is_none() -> None:
    assert last_value("198.51.100.1, 203.0.113.9") == "203.0.113.9"
    assert last_value(" 203.0.113.9 ") == "203.0.113.9"
    assert last_value("203.0.113.9, ") is None
    assert last_value("") is None
    assert last_value(None) is None


def test_a_trusted_peer_names_the_client_with_its_last_value() -> None:
    trusted = parse_cidr_list(INGRESS)
    assert forwarded_client("10.42.0.5", "198.51.100.1, 203.0.113.9", trusted) == "203.0.113.9"
    assert forwarded_client("::ffff:10.42.0.5", "203.0.113.9", trusted) == "203.0.113.9"


def test_an_untrusted_peer_is_not_believed() -> None:
    trusted = parse_cidr_list(INGRESS)
    assert forwarded_client("10.43.0.5", "203.0.113.9", trusted) is None
    # From loopback no proxy is involved unless it is listed.
    assert forwarded_client("127.0.0.1", "203.0.113.9", trusted) is None
    assert forwarded_client("10.42.0.5", "203.0.113.9", ()) is None


def test_the_client_is_the_peer_unless_a_trusted_proxy_names_one() -> None:
    trusted = parse_cidr_list(INGRESS)
    assert client_address("10.42.0.5", "198.51.100.1, 203.0.113.9", trusted) == "203.0.113.9"
    assert client_address("10.43.0.5", "203.0.113.9", trusted) == "10.43.0.5"
    assert client_address("10.42.0.5", "203.0.113.9, ", trusted) == "10.42.0.5"
    assert client_address("10.42.0.5", None, trusted) == "10.42.0.5"
    assert client_address(None, None, trusted) is None


def _settings(trusted_proxies: str = "") -> Settings:
    return Settings(
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        trusted_proxies=trusted_proxies,
    )


def test_the_setting_is_a_bootstrap_field_parsed_once_valid() -> None:
    assert "trusted_proxies" in BOOTSTRAP_FIELDS
    assert "trusted_proxies" not in ENV_SEEDED
    assert _settings().trusted_proxy_networks == ()
    assert _settings(trusted_proxies="10.42.0.0/16").trusted_proxy_networks == (
        ipaddress.ip_network("10.42.0.0/16"),
    )


def test_a_malformed_setting_stops_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_TRUSTED_PROXIES", "10.0.0.0/33")
    with pytest.raises(ValidationError, match="SCADBUDDY_TRUSTED_PROXIES"):
        Settings(database_url=UNUSED_DATABASE_URL, temporal_address=UNUSED_TEMPORAL_ADDRESS)
