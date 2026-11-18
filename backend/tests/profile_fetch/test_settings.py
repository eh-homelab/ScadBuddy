"""The profile-fetch worker's settings (#6625 Task 6): no database secret, the 3DFP login
required and never shown, and no Playwright debug log."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from scadbuddy.profile_fetch import __main__ as entry
from scadbuddy.profile_fetch.__main__ import main
from scadbuddy.profile_fetch.settings import (
    FORBIDDEN_ENV,
    ProfileFetchRefusal,
    ProfileFetchSettings,
    check_environment,
    forget_login,
)
from scadbuddy.profile_fetch.wire import DEFAULT_QUEUE

EMAIL = "owner-6625@example.invalid"
PASSWORD = "pw-6625-not-a-real-password"


@pytest.fixture(autouse=True)
def _dumpable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` would make the test process non-dumpable (`hardening.forbid_dumping`)."""
    monkeypatch.setattr(entry, "forbid_dumping", lambda: None)
    # Its handlers would replace the one `caplog` reads.
    monkeypatch.setattr(entry, "configure_logging", lambda *args: None)


@pytest.fixture
def worker_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in (*FORBIDDEN_ENV, "DEBUG", "SCADBUDDY_SERVICE_BOOTSTRAP_FILE"):
        monkeypatch.delenv(name, raising=False)
    proof = tmp_path / "token"
    proof.write_text("t")
    monkeypatch.setenv("SCADBUDDY_API_INTERNAL_URL", "http://scadbuddy:8082")
    monkeypatch.setenv("SCADBUDDY_TEMPORAL_ADDRESS", "temporal:7233")
    monkeypatch.setenv("SCADBUDDY_SERVICE_TOKEN_FILE", str(proof))
    monkeypatch.setenv("SCADBUDDY_TDFP_EMAIL", EMAIL)
    monkeypatch.setenv("SCADBUDDY_TDFP_PASSWORD", PASSWORD)
    return proof


def test_the_settings_read_the_queue_and_the_login(worker_env: Path) -> None:
    settings = ProfileFetchSettings()  # type: ignore[call-arg]
    assert settings.temporal_task_queue_profile_fetch == DEFAULT_QUEUE == "profile-fetch"
    assert settings.tdfp_email.get_secret_value() == EMAIL
    assert settings.tdfp_password.get_secret_value() == PASSWORD


@pytest.mark.parametrize("name", FORBIDDEN_ENV)
def test_refuses_to_start_with_a_database_secret(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "x")
    assert main([]) == 2


@pytest.mark.parametrize("missing", ["SCADBUDDY_TDFP_EMAIL", "SCADBUDDY_TDFP_PASSWORD"])
def test_refuses_to_start_without_the_3dfp_login(
    worker_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    missing: str,
) -> None:
    monkeypatch.delenv(missing)
    with pytest.raises(ValidationError, match=missing.removeprefix("SCADBUDDY_").lower()):
        ProfileFetchSettings()  # type: ignore[call-arg]
    caplog.set_level(logging.ERROR)
    assert main([]) == 2
    assert missing.removeprefix("SCADBUDDY_").lower() in caplog.text
    assert EMAIL not in caplog.text and PASSWORD not in caplog.text


def test_a_blank_login_refuses_the_start(worker_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_TDFP_PASSWORD", "  ")
    with pytest.raises(ValidationError, match="tdfp_password"):
        ProfileFetchSettings()  # type: ignore[call-arg]


def test_a_bad_setting_logs_no_secret(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A refused setting is logged by its name and why, never with the input values, so
    a bad URL beside the login does not print the login."""
    monkeypatch.setenv("SCADBUDDY_API_INTERNAL_URL", "not a url")
    caplog.set_level(logging.ERROR)
    assert main([]) == 2
    assert "api_internal_url" in caplog.text
    assert EMAIL not in caplog.text and PASSWORD not in caplog.text


def test_settings_repr_holds_no_secret(worker_env: Path) -> None:
    settings = ProfileFetchSettings()  # type: ignore[call-arg]
    for text in (
        repr(settings),
        str(settings),
        settings.model_dump_json(),
        str(settings.model_dump()),
    ):
        assert EMAIL not in text
        assert PASSWORD not in text


def test_the_settings_need_one_proof(worker_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_SERVICE_BOOTSTRAP_FILE", str(worker_env))
    with pytest.raises(ValidationError, match="exactly one"):
        ProfileFetchSettings()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "debug", ["pw:api", "pw:protocol", "pw*", "express:*,pw:browser", "app pw:api", "*", "p*"]
)
def test_a_playwright_debug_log_refuses_the_start(debug: str) -> None:
    """Playwright's debug log writes each call's arguments, a filled password among
    them, to stderr."""
    with pytest.raises(ProfileFetchRefusal, match="DEBUG"):
        check_environment({"DEBUG": debug})


@pytest.mark.parametrize("debug", ["", "express:*", "-pw:*", "app,other", "pwa"])
def test_another_debug_log_is_allowed(debug: str) -> None:
    check_environment({"DEBUG": debug})


def test_main_refuses_a_playwright_debug_log(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEBUG", "pw:api")
    assert main([]) == 2


def test_forget_login_removes_every_spelling_of_the_login() -> None:
    """pydantic-settings reads names without regard to case, so the login may come in any
    spelling; `forget_login` removes each one, and nothing else."""
    environ = {
        "SCADBUDDY_TDFP_EMAIL": EMAIL,
        "scadbuddy_tdfp_email": EMAIL,
        "Scadbuddy_Tdfp_Password": PASSWORD,
        "scadbuddy_tdfp_password": PASSWORD,
        "SCADBUDDY_API_INTERNAL_URL": "http://scadbuddy:8082",
        "SCADBUDDY_TDFP_EMAIL_X": "kept",
    }
    forget_login(environ)
    assert environ == {
        "SCADBUDDY_API_INTERNAL_URL": "http://scadbuddy:8082",
        "SCADBUDDY_TDFP_EMAIL_X": "kept",
    }


def test_the_settings_read_a_lower_case_login(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case `forget_login` covers: a lower-case name is read as the login."""
    monkeypatch.delenv("SCADBUDDY_TDFP_EMAIL")
    monkeypatch.setenv("scadbuddy_tdfp_email", EMAIL)
    assert ProfileFetchSettings().tdfp_email.get_secret_value() == EMAIL  # type: ignore[call-arg]
