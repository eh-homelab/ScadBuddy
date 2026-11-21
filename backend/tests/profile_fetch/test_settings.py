"""The profile-fetch settings (#6625 Task 6, #6780). The worker: no database secret, no 3DFP
login, the browser service's URL and RPC token required. The browser service: the 3DFP login
required and never shown, the RPC token required, and no database secret, proof, Temporal or
API address, or Playwright debug log."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from scadbuddy.profile_fetch import __main__ as entry
from scadbuddy.profile_fetch import browser
from scadbuddy.profile_fetch.__main__ import main
from scadbuddy.profile_fetch.settings import (
    BROWSER_FORBIDDEN_ENV,
    FORBIDDEN_ENV,
    LOGIN_ENV,
    BrowserSettings,
    ProfileFetchRefusal,
    ProfileFetchSettings,
    check_browser_environment,
    check_environment,
    forget_login,
)
from scadbuddy.profile_fetch.wire import DEFAULT_QUEUE

EMAIL = "owner-6625@example.invalid"
PASSWORD = "pw-6625-not-a-real-password"
TOKEN = "k" * 40


@pytest.fixture(autouse=True)
def _dumpable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` would make the test process non-dumpable (`hardening.forbid_dumping`)."""
    for module in (entry, browser):
        monkeypatch.setattr(module, "forbid_dumping", lambda: None)
        # Its handlers would replace the one `caplog` reads.
        monkeypatch.setattr(module, "configure_logging", lambda *args: None)
    monkeypatch.setattr(browser, "SECRETS_DIR", Path("/nonexistent-6780"))


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*BROWSER_FORBIDDEN_ENV, *LOGIN_ENV, "DEBUG"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def worker_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _clear(monkeypatch)
    proof = tmp_path / "token"
    proof.write_text("t")
    rpc = tmp_path / "rpc"
    rpc.write_text(TOKEN + "\n")
    monkeypatch.setenv("SCADBUDDY_API_INTERNAL_URL", "http://scadbuddy:8082")
    monkeypatch.setenv("SCADBUDDY_TEMPORAL_ADDRESS", "temporal:7233")
    monkeypatch.setenv("SCADBUDDY_SERVICE_TOKEN_FILE", str(proof))
    monkeypatch.setenv("SCADBUDDY_PROFILE_FETCH_BROWSER_URL", "http://browser.example:8091/")
    monkeypatch.setenv("SCADBUDDY_PROFILE_FETCH_BROWSER_TOKEN_FILE", str(rpc))
    return proof


@pytest.fixture
def browser_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _clear(monkeypatch)
    rpc = tmp_path / "rpc"
    rpc.write_text(TOKEN)
    monkeypatch.setenv("SCADBUDDY_TDFP_EMAIL", EMAIL)
    monkeypatch.setenv("SCADBUDDY_TDFP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SCADBUDDY_PROFILE_FETCH_BROWSER_TOKEN_FILE", str(rpc))
    monkeypatch.setenv("SCADBUDDY_PROFILE_FETCH_WORK_DIR", str(tmp_path))
    return rpc


# -- the worker ---------------------------------------------------------------------------


def test_the_worker_settings_read_the_queue_and_the_browser(worker_env: Path) -> None:
    settings = ProfileFetchSettings()  # type: ignore[call-arg]
    assert settings.temporal_task_queue_profile_fetch == DEFAULT_QUEUE == "profile-fetch"
    assert settings.profile_fetch_browser_url == "http://browser.example:8091"
    assert settings.browser_token == TOKEN
    assert not hasattr(settings, "tdfp_password")


@pytest.mark.parametrize("name", FORBIDDEN_ENV)
def test_the_worker_refuses_a_database_secret(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "x")
    assert main([]) == 2


@pytest.mark.parametrize(
    "name", ["SCADBUDDY_TDFP_EMAIL", "SCADBUDDY_TDFP_PASSWORD", "scadbuddy_tdfp_password"]
)
def test_the_worker_refuses_the_3dfp_login(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The login lives in the browser service only (#6780), in any spelling
    pydantic-settings would read."""
    monkeypatch.setenv(name, PASSWORD)
    with pytest.raises(ProfileFetchRefusal, match="browser service"):
        check_environment({name: PASSWORD})
    assert main([]) == 2


@pytest.mark.parametrize(
    "missing", ["SCADBUDDY_PROFILE_FETCH_BROWSER_URL", "SCADBUDDY_PROFILE_FETCH_BROWSER_TOKEN_FILE"]
)
def test_the_worker_needs_the_browser_service(
    worker_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    missing: str,
) -> None:
    monkeypatch.delenv(missing)
    caplog.set_level(logging.ERROR)
    assert main([]) == 2
    assert missing.removeprefix("SCADBUDDY_").lower() in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "https://browser.example:8091",
        "http://user@browser.example:8091",
        "http://browser.example:8091/path",
        "http://browser.example:8091?x=1",
        "not a url",
    ],
)
def test_the_browser_url_must_be_a_plain_http_url(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("SCADBUDDY_PROFILE_FETCH_BROWSER_URL", url)
    with pytest.raises(ValidationError, match="plain http"):
        ProfileFetchSettings()  # type: ignore[call-arg]


@pytest.mark.parametrize("token", ["short", "x" * 1025, "has space " * 5])
def test_a_bad_rpc_token_refuses_the_start(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str
) -> None:
    rpc = tmp_path / "bad"
    rpc.write_text(token)
    monkeypatch.setenv("SCADBUDDY_PROFILE_FETCH_BROWSER_TOKEN_FILE", str(rpc))
    with pytest.raises(ValidationError, match="SCADBUDDY_PROFILE_FETCH_BROWSER_TOKEN_FILE"):
        ProfileFetchSettings()  # type: ignore[call-arg]


def test_the_worker_settings_need_one_proof(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCADBUDDY_SERVICE_BOOTSTRAP_FILE", str(worker_env))
    with pytest.raises(ValidationError, match="exactly one"):
        ProfileFetchSettings()  # type: ignore[call-arg]


def test_a_bad_worker_setting_logs_its_name(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SCADBUDDY_API_INTERNAL_URL", "not a url")
    caplog.set_level(logging.ERROR)
    assert main([]) == 2
    assert "api_internal_url" in caplog.text
    assert TOKEN not in caplog.text


# -- the browser service ------------------------------------------------------------------


def test_the_browser_settings_read_the_login_and_the_token(browser_env: Path) -> None:
    settings = BrowserSettings()  # type: ignore[call-arg]
    assert settings.tdfp_email.get_secret_value() == EMAIL
    assert settings.tdfp_password.get_secret_value() == PASSWORD
    assert settings.browser_token == TOKEN
    assert settings.profile_fetch_browser_port == 8091


@pytest.mark.parametrize("name", BROWSER_FORBIDDEN_ENV)
def test_the_browser_service_refuses_a_secret_a_proof_or_a_route_out(
    browser_env: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """No database secret, no proof for a CRUD credential, and no Temporal or API
    address: it reaches neither (#6780)."""
    monkeypatch.setenv(name, "x")
    with pytest.raises(ProfileFetchRefusal):
        check_browser_environment({name: "x"})
    assert browser.main([]) == 2


def test_the_browser_service_refuses_a_mounted_secret(
    browser_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secrets = tmp_path / "secrets" / "kubernetes.io" / "serviceaccount"
    secrets.mkdir(parents=True)
    (secrets / "token").write_text("sa")
    monkeypatch.setattr(browser, "SECRETS_DIR", tmp_path / "secrets")
    assert browser.main([]) == 2


@pytest.mark.parametrize("missing", ["SCADBUDDY_TDFP_EMAIL", "SCADBUDDY_TDFP_PASSWORD"])
def test_the_browser_service_needs_the_3dfp_login(
    browser_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    missing: str,
) -> None:
    monkeypatch.delenv(missing)
    with pytest.raises(ValidationError, match=missing.removeprefix("SCADBUDDY_").lower()):
        BrowserSettings()  # type: ignore[call-arg]
    caplog.set_level(logging.ERROR)
    assert browser.main([]) == 2
    assert missing.removeprefix("SCADBUDDY_").lower() in caplog.text
    assert EMAIL not in caplog.text and PASSWORD not in caplog.text


def test_the_browser_service_needs_the_rpc_token(
    browser_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("SCADBUDDY_PROFILE_FETCH_BROWSER_TOKEN_FILE")
    caplog.set_level(logging.ERROR)
    assert browser.main([]) == 2
    assert "profile_fetch_browser_token_file" in caplog.text
    assert EMAIL not in caplog.text and PASSWORD not in caplog.text


def test_a_blank_login_refuses_the_start(
    browser_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCADBUDDY_TDFP_PASSWORD", "  ")
    with pytest.raises(ValidationError, match="tdfp_password"):
        BrowserSettings()  # type: ignore[call-arg]


def test_browser_settings_repr_holds_no_secret(browser_env: Path) -> None:
    settings = BrowserSettings()  # type: ignore[call-arg]
    for text in (
        repr(settings),
        str(settings),
        settings.model_dump_json(),
        str(settings.model_dump()),
    ):
        assert EMAIL not in text
        assert PASSWORD not in text
        assert TOKEN not in text


@pytest.mark.parametrize(
    "debug", ["pw:api", "pw:protocol", "pw*", "express:*,pw:browser", "app pw:api", "*", "p*"]
)
def test_a_playwright_debug_log_refuses_the_start(debug: str) -> None:
    """Playwright's debug log writes each call's arguments, a filled password among
    them, to stderr."""
    with pytest.raises(ProfileFetchRefusal, match="DEBUG"):
        check_browser_environment({"DEBUG": debug})


@pytest.mark.parametrize("debug", ["", "express:*", "-pw:*", "app,other", "pwa"])
def test_another_debug_log_is_allowed(debug: str) -> None:
    check_browser_environment({"DEBUG": debug})


def test_the_browser_main_refuses_a_playwright_debug_log(
    browser_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEBUG", "pw:api")
    assert browser.main([]) == 2


def test_forget_login_removes_every_spelling_of_the_login() -> None:
    """pydantic-settings reads names without regard to case, so the login may come in any
    spelling; `forget_login` removes each one, and nothing else."""
    environ = {
        "SCADBUDDY_TDFP_EMAIL": EMAIL,
        "scadbuddy_tdfp_email": EMAIL,
        "Scadbuddy_Tdfp_Password": PASSWORD,
        "scadbuddy_tdfp_password": PASSWORD,
        "SCADBUDDY_PROFILE_FETCH_WORK_DIR": "/tmp",
        "SCADBUDDY_TDFP_EMAIL_X": "kept",
    }
    forget_login(environ)
    assert environ == {
        "SCADBUDDY_PROFILE_FETCH_WORK_DIR": "/tmp",
        "SCADBUDDY_TDFP_EMAIL_X": "kept",
    }


def test_the_browser_settings_read_a_lower_case_login(
    browser_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case `forget_login` covers: a lower-case name is read as the login."""
    monkeypatch.delenv("SCADBUDDY_TDFP_EMAIL")
    monkeypatch.setenv("scadbuddy_tdfp_email", EMAIL)
    assert BrowserSettings().tdfp_email.get_secret_value() == EMAIL  # type: ignore[call-arg]
