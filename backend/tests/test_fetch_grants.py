"""The one-off host grant the agent mints for an approved fetch (#959)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scadbuddy.core.secrets import Kek
from scadbuddy.library.fetch_grants import FetchGrantError, fetch_grant_key, mint, verify

HOST = "icons.example.com"
URL = "https://icons.example.com/a.svg"
#: Minted by the agent (`agent/test/fetchOnce.test.ts`): KEK 32 bytes of 7, expiry
#: 2000000000, nonce "ab" * 16.
VECTOR = (
    "YXNzZXQtZmV0Y2gtb25jZQppY29ucy5leGFtcGxlLmNvbQplNjgxNTEzNjE2YjkwNTM3YjdmMTEyNzJm"
    "ZjFkMTE2MDE0Zjg4NDFjZDgxMGZlMmM0NjE5OWRjODcxNDQzMDhiCjIwMDAwMDAwMDAKYWJhYmFiYWJh"
    "YmFiYWJhYmFiYWJhYmFiYWJhYmFiYWI.bVgHQHSbT1v8DfE8-6pNyOYiUMsvYOfIoaJFf-RSPv0"
)
BEFORE = datetime.fromtimestamp(1_999_999_000, UTC)
KEY = fetch_grant_key(Kek(id="k", key=bytes([7]) * 32))


def test_the_agents_grant_verifies_and_names_its_nonce() -> None:
    assert verify(KEY, VECTOR, host=HOST, url=URL, now=BEFORE) == "ab" * 16


@pytest.mark.parametrize(
    ("host", "url", "now"),
    [
        ("evil.example", URL, BEFORE),
        (HOST, "https://icons.example.com/b.svg", BEFORE),
        (HOST, URL, datetime.fromtimestamp(2_000_000_001, UTC)),
    ],
)
def test_a_grant_serves_only_its_host_and_url_until_it_expires(
    host: str, url: str, now: datetime
) -> None:
    with pytest.raises(FetchGrantError):
        verify(KEY, VECTOR, host=host, url=url, now=now)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "nodot",
        VECTOR.replace("bVgH", "bVgI"),
        VECTOR + "=",
        mint(fetch_grant_key(Kek(id="o", key=bytes(32))), HOST, URL, now=BEFORE),
    ],
)
def test_anything_else_is_refused(token: str) -> None:
    with pytest.raises(FetchGrantError):
        verify(KEY, token, host=HOST, url=URL, now=BEFORE)


def test_a_minted_grant_round_trips() -> None:
    token = mint(KEY, HOST, URL, now=BEFORE)
    assert len(verify(KEY, token, host=HOST, url=URL, now=BEFORE)) == 32
