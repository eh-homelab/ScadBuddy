"""The transfer token (plan 2026-10-16-native-slicer-rust-worker Task 10): an HMAC over
method, key and expiry, under a key derived from the KEK."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from scadbuddy.core.secrets import kek_from_base64
from scadbuddy.slicing.transfer_token import (
    TokenError,
    TransferToken,
    sign,
    transfer_key,
    verify,
)

AT = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
SECRET = transfer_key(kek_from_base64(base64.b64encode(bytes(32)).decode()))
TOKEN = TransferToken(method="GET", blob_key="slice-abc", expires_at=AT)
BEFORE = AT - timedelta(minutes=1)


def test_token_round_trips() -> None:
    assert verify(SECRET, sign(SECRET, TOKEN), method="GET", blob_key="slice-abc", now=BEFORE) == (
        TOKEN
    )


def test_the_key_is_32_bytes_and_not_the_kek() -> None:
    assert len(SECRET) == 32
    assert bytes(32) != SECRET


def test_the_format_is_fixed() -> None:
    body, mac = sign(SECRET, TOKEN).split(".")
    assert base64.urlsafe_b64decode(body) == f"GET\nslice-abc\n{int(AT.timestamp())}".encode()
    assert len(base64.urlsafe_b64decode(mac)) == 32


@pytest.mark.parametrize(
    ("method", "blob_key", "now"),
    [
        ("PUT", "slice-abc", BEFORE),
        ("GET", "slice-other", BEFORE),
        ("GET", "slice-abc", AT + timedelta(seconds=1)),
        ("GET", "slice-abc", AT),
    ],
)
def test_token_refuses_other_method_key_or_expiry(
    method: str, blob_key: str, now: datetime
) -> None:
    with pytest.raises(TokenError):
        verify(SECRET, sign(SECRET, TOKEN), method=method, blob_key=blob_key, now=now)


def test_tampered_mac_is_refused() -> None:
    body, mac = sign(SECRET, TOKEN).split(".")
    flipped = mac[:3] + ("A" if mac[3] != "A" else "B") + mac[4:]
    with pytest.raises(TokenError):
        verify(SECRET, f"{body}.{flipped}", method="GET", blob_key="slice-abc", now=BEFORE)


def test_a_tampered_body_is_refused() -> None:
    _, mac = sign(SECRET, TOKEN).split(".")
    later = base64.urlsafe_b64encode(f"GET\nslice-abc\n{int(AT.timestamp()) + 3600}".encode())
    with pytest.raises(TokenError):
        verify(
            SECRET,
            f"{later.decode()}.{mac}",
            method="GET",
            blob_key="slice-abc",
            now=AT + timedelta(minutes=1),
        )


def test_another_kek_is_refused() -> None:
    other = transfer_key(kek_from_base64(base64.b64encode(bytes([1]) * 32).decode()))
    with pytest.raises(TokenError):
        verify(SECRET, sign(other, TOKEN), method="GET", blob_key="slice-abc", now=BEFORE)


@pytest.mark.parametrize("value", ["", ".", "abc", "a.b.c", "!!!.???", "Zm9v.Zm9v"])
def test_malformed_values_are_refused(value: str) -> None:
    with pytest.raises(TokenError):
        verify(SECRET, value, method="GET", blob_key="slice-abc", now=BEFORE)
