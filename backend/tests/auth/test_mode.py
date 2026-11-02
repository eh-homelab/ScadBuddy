"""``SCADBUDDY_AUTH_MODE`` (#4975, spec 2026-11-01 §9.1): `off` by default, and the
modes no gate implements yet stop the start."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.core.settings import BOOTSTRAP_FIELDS, Settings
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS


def _settings() -> Settings:
    return Settings(database_url=UNUSED_DATABASE_URL, temporal_address=UNUSED_TEMPORAL_ADDRESS)


def test_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCADBUDDY_AUTH_MODE", raising=False)
    assert _settings().auth_mode == "off"


def test_off_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_AUTH_MODE", "off")
    assert _settings().auth_mode == "off"


def test_observe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_AUTH_MODE", "observe")
    assert _settings().auth_mode == "observe"


def test_enforce_stops_the_start_until_it_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_AUTH_MODE", "enforce")
    with pytest.raises(ValidationError, match="SCADBUDDY_AUTH_MODE=enforce is not available"):
        _settings()


def test_an_unknown_mode_stops_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_AUTH_MODE", "on")
    with pytest.raises(ValidationError):
        _settings()


def test_settings_cannot_hold_it() -> None:
    assert "auth_mode" in BOOTSTRAP_FIELDS
