"""``core/secrets.py``: the agent's sealed format (``agent/src/secrets.ts``), in Python."""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

import pytest

from scadbuddy.core.secrets import (
    Envelope,
    SealError,
    SecretKeyError,
    kek_from_base64,
    load_kek,
    open_secret,
    seal_secret,
)

#: The agent's vector key (agent/test/fixtures/secret-vectors.json): 32 bytes of 0x5a.
KEK_B64 = "WlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlo="
OTHER_B64 = base64.b64encode(b"\x01" * 32).decode()

#: Sealed by agent/src/secrets.ts `sealSecret(kek, PLAINTEXT, AAD, counter)`, where
#: `counter` hands out 0, 1, 2, ... as bytes: the data key, then each seal's IV.
AGENT_VECTOR = Envelope(
    secret_sealed=base64.b64decode(
        "AiAhIiMkJSYnKCkqK3JaZFmcgKiNpJqysgB/9HKwW8sSGfx+dzcKJ621d4bUuyyVsbexUd1Yw1olcbM="
    ),
    dek_sealed=base64.b64decode(
        "AiwtLi8wMTIzNDU2N0a74FVqig/eVLPOgS1WBzZmdZ0BKUM5gwTMoHY8irjmCYNCQl3e8blqnyyzZhQCag=="
    ),
    kek_id="60bf07c488aad18f",
)
PLAINTEXT = "bambuddy-vector-key-0123456789"
AAD = "settings:bambuddy_api_key"


def _counter() -> Callable[[int], bytes]:
    state = {"n": 0}

    def random(size: int) -> bytes:
        out = bytes((state["n"] + i) & 0xFF for i in range(size))
        state["n"] += size
        return out

    return random


def test_a_value_the_agent_sealed_opens() -> None:
    assert open_secret(kek_from_base64(KEK_B64), AGENT_VECTOR, AAD) == PLAINTEXT


def test_sealing_with_the_agents_bytes_writes_the_agents_envelope() -> None:
    sealed = seal_secret(kek_from_base64(KEK_B64), PLAINTEXT, AAD, random=_counter())
    assert sealed == AGENT_VECTOR


def test_a_sealed_value_round_trips_and_differs_each_time() -> None:
    kek = kek_from_base64(KEK_B64)
    first, second = seal_secret(kek, "k", AAD), seal_secret(kek, "k", AAD)
    assert first.secret_sealed != second.secret_sealed
    assert open_secret(kek, first, AAD) == open_secret(kek, second, AAD) == "k"
    assert first.secret_sealed[0] == 0x02


def test_the_context_is_authenticated() -> None:
    """A value copied into another setting's row does not open there."""
    kek = kek_from_base64(KEK_B64)
    sealed = seal_secret(kek, "k", AAD)
    with pytest.raises(SealError):
        open_secret(kek, sealed, "settings:bambuddy_render_api_key")


def test_altered_bytes_fail_authentication() -> None:
    kek = kek_from_base64(KEK_B64)
    sealed = seal_secret(kek, "k", AAD)
    flipped = bytearray(sealed.secret_sealed)
    flipped[-1] ^= 1
    altered = Envelope(bytes(flipped), sealed.dek_sealed, sealed.kek_id)
    with pytest.raises(SealError, match="failed authentication"):
        open_secret(kek, altered, AAD)


def test_another_key_names_both_ids_and_nothing_else() -> None:
    sealed = seal_secret(kek_from_base64(KEK_B64), "the-plaintext", AAD)
    other = kek_from_base64(OTHER_B64)
    with pytest.raises(SealError) as raised:
        open_secret(other, sealed, AAD)
    assert sealed.kek_id in str(raised.value)
    assert other.id in str(raised.value)
    assert "the-plaintext" not in str(raised.value)


def test_no_repr_shows_a_key_or_a_sealed_value() -> None:
    kek = kek_from_base64(KEK_B64)
    sealed = seal_secret(kek, "k", AAD)
    assert "ZZZZ" not in repr(kek) and repr(kek.key) not in repr(kek)
    assert repr(sealed.secret_sealed) not in repr(sealed)
    assert repr(sealed.dek_sealed) not in repr(sealed)


@pytest.mark.parametrize(
    "text",
    ["not base64!", base64.b64encode(b"short").decode(), "a" * 43],
)
def test_a_malformed_key_is_refused(text: str) -> None:
    with pytest.raises(SecretKeyError, match="SCADBUDDY_SECRET_KEY_FILE"):
        kek_from_base64(text)


def test_a_key_file_with_a_trailing_newline_loads(tmp_path: Path) -> None:
    path = tmp_path / "secret.key"
    path.write_text(KEK_B64 + "\n")
    assert load_kek(path).id == "60bf07c488aad18f"


def test_an_unreadable_key_file_names_the_variable_not_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "secret.key"
    with pytest.raises(SecretKeyError) as raised:
        load_kek(missing)
    assert "SCADBUDDY_SECRET_KEY_FILE" in str(raised.value)
    assert str(missing) not in str(raised.value)


def test_a_raw_key_file_never_reaches_the_message(tmp_path: Path) -> None:
    path = tmp_path / "secret.key"
    path.write_bytes(b"\xfe\xffsecretbytes" * 3)
    with pytest.raises(SecretKeyError) as raised:
        load_kek(path)
    assert "secretbytes" not in str(raised.value)
