"""The login cookie and ``return_to`` (#4975 phase 2, spec 2026-11-01 §5.1 step 1), and the
sign-in rule (D3)."""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import UTC, datetime, timedelta

import pytest

from scadbuddy.auth.config import LoginConfig
from scadbuddy.auth.login_state import (
    LOGIN_LIFETIME,
    new_state,
    open_state,
    safe_return_to,
    seal_state,
)
from scadbuddy.core.secrets import Kek

KEK = Kek(id="k", key=os.urandom(32))


@pytest.mark.parametrize("path", ["/", "/models", "/models/x?tab=a#b", "/a%20b"])
def test_a_same_origin_path_is_kept(path: str) -> None:
    assert safe_return_to(path) == path


@pytest.mark.parametrize(
    "value",
    [
        "",
        "models",
        "//evil.test",
        "/\\evil.test",
        "/%2F%2Fevil.test",
        "/%252F%252Fevil.test",
        "/%5Cevil.test",
        "https://evil.test/",
        "javascript:alert(1)",
        "/x\r\nSet-Cookie: a=b",
    ],
)
def test_anything_else_is_home(value: str) -> None:
    assert safe_return_to(value) == "/"


def test_the_state_round_trips_and_carries_the_challenge() -> None:
    state = new_state("/models", popup=True)
    opened = open_state(KEK, seal_state(KEK, state))
    assert opened == state
    digest = hashlib.sha256(state.verifier.encode()).digest()
    assert state.challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert 43 <= len(state.verifier) <= 128


def test_the_state_expires() -> None:
    at = datetime(2026, 11, 1, tzinfo=UTC)
    sealed = seal_state(KEK, new_state("/", popup=False, now=at))
    assert open_state(KEK, sealed, now=at + LOGIN_LIFETIME - timedelta(seconds=1)) is not None
    assert open_state(KEK, sealed, now=at + LOGIN_LIFETIME) is None


@pytest.mark.parametrize("cookie", ["", "not base64!", "AAAA"])
def test_a_bad_cookie_opens_to_nothing(cookie: str) -> None:
    assert open_state(KEK, cookie) is None


def test_another_keys_cookie_opens_to_nothing() -> None:
    other = Kek(id="o", key=os.urandom(32))
    assert open_state(other, seal_state(KEK, new_state("/", popup=False))) is None


def test_the_rule() -> None:
    by_subject = LoginConfig(allowed_subjects=[" alice ", ""])
    assert by_subject.allowed_subjects == ["alice"]
    assert by_subject.allows({"sub": "alice"}) and not by_subject.allows({"sub": "bob"})
    by_group = LoginConfig(group_claim="groups", group_value="sb")
    assert by_group.allows({"sub": "x", "groups": ["a", "sb"]})
    assert by_group.allows({"sub": "x", "groups": "sb"})
    assert not by_group.allows({"sub": "x", "groups": ["a"]})
    assert not by_group.allows({"sub": "x"})
    assert not LoginConfig().has_rule()
    assert not LoginConfig(group_claim="groups").has_rule()
