"""The profile-fetch processes' settings (#6625 Task 6, #6780). Neither has a database, key,
store or Bambuddy field.

`ProfileFetchSettings` is the worker's: it reaches the CRUD service, Temporal and the browser
service only, and refuses to start with the 3DFP login (`check_environment`).
`BrowserSettings` is the browser service's: it holds the 3DFP login (two `SecretStr`s, so no
repr, log line or dump of the settings shows it) and the RPC token, and refuses to start with
a database secret, the worker's proof, a Temporal or API address, or a Playwright debug log
(`check_browser_environment`)."""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from pydantic import Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from scadbuddy.intake_worker.runner import read_token
from scadbuddy.intake_worker.settings import FORBIDDEN_ENV, LOG_LEVELS
from scadbuddy.profile_fetch.remote import BROWSER_PORT, TOKEN_FILE_ENV
from scadbuddy.profile_fetch.wire import DEFAULT_QUEUE

HEALTH_PORT: Final = 9090
#: Playwright's debug log (`DEBUG=pw:api`, `pw:protocol`, …) writes each call with its
#: arguments to stderr, a filled password among them. These are its channels: a ``DEBUG``
#: entry that would turn one on refuses the browser service's start.
PLAYWRIGHT_CHANNELS: Final = ("pw:api", "pw:protocol", "pw:browser", "pw:channel")

#: The 3DFP login's variables. `forget_login` removes them once the settings hold it.
LOGIN_ENV: Final = ("SCADBUDDY_TDFP_EMAIL", "SCADBUDDY_TDFP_PASSWORD")

#: Besides `FORBIDDEN_ENV`, what the browser service refuses to start with (#6780): the
#: worker's proof, and the addresses of Temporal and the API, which it never calls.
BROWSER_FORBIDDEN_ENV: Final = (
    *FORBIDDEN_ENV,
    "SCADBUDDY_SERVICE_TOKEN_FILE",
    "SCADBUDDY_SERVICE_BOOTSTRAP_FILE",
    "SCADBUDDY_TEMPORAL_ADDRESS",
    "SCADBUDDY_API_INTERNAL_URL",
)

__all__ = [
    "BROWSER_FORBIDDEN_ENV",
    "FORBIDDEN_ENV",
    "HEALTH_PORT",
    "LOGIN_ENV",
    "BrowserSettings",
    "ProfileFetchRefusal",
    "ProfileFetchSettings",
    "check_browser_environment",
    "check_environment",
    "forget_login",
]


class ProfileFetchRefusal(Exception):  # noqa: N818  (a refusal to start, not an error)
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _debug_names_playwright(value: str) -> bool:
    """Whether a ``DEBUG`` value (the `debug` package's comma or space separated
    patterns, ``*`` a wildcard, ``-`` an exclusion) turns on a Playwright channel."""
    for entry in re.split(r"[\s,]+", value):
        if not entry or entry.startswith("-"):
            continue
        if entry.startswith("pw:"):
            return True
        pattern = re.compile(".*?".join(re.escape(part) for part in entry.split("*")))
        if any(pattern.fullmatch(channel) for channel in PLAYWRIGHT_CHANNELS):
            return True
    return False


def check_environment(environ: Mapping[str, str]) -> None:
    """The worker's: no database secret, and no 3DFP login (#6780). The login lives in the
    browser service only; the worker never types it anywhere."""
    reasons = [
        f"{name} is set; the profile-fetch worker holds no database secret or key"
        for name in FORBIDDEN_ENV
        if name in environ
    ]
    reasons += [
        f"{name} is set; the 3DFP login belongs to the browser service, not the worker"
        for name in sorted({name.upper() for name in environ} & set(LOGIN_ENV))
    ]
    if reasons:
        raise ProfileFetchRefusal(reasons)


def check_browser_environment(environ: Mapping[str, str]) -> None:
    """The browser service's: no database secret, no proof, no Temporal or API address,
    and no Playwright debug log."""
    reasons = [
        f"{name} is set; the browser service holds no secret but the 3DFP login and the RPC"
        " token, and reaches neither Temporal nor the API"
        for name in BROWSER_FORBIDDEN_ENV
        if name in environ
    ]
    if _debug_names_playwright(environ.get("DEBUG", "")):
        reasons.append(
            "DEBUG names a Playwright channel; its log would write the 3DFP login to stderr"
        )
    if reasons:
        raise ProfileFetchRefusal(reasons)


def forget_login(environ: MutableMapping[str, str]) -> None:
    """Remove the 3DFP login from ``environ`` (``os.environ``) once the browser service's
    settings read it, so no process the service starts (Playwright's driver, the browser)
    inherits it (Task 6 review, I1). pydantic-settings reads names without regard to case,
    so every spelling of them goes."""
    for name in [name for name in environ if name.upper() in LOGIN_ENV]:
        del environ[name]


def _plain_http_url(name: str, url: str) -> str:
    """``url`` without a final slash, when it is a plain ``http://`` URL of a Service: no
    user, path, query or fragment."""
    parts = urlsplit(url)
    if (
        parts.scheme != "http"
        or not parts.hostname
        or parts.username is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError(f"{name} must be a plain http:// URL of a Service, not {url!r}")
    return url.rstrip("/")


def _token(path: Path) -> str:
    try:
        return read_token(path)
    except ValueError as bad:
        raise ValueError(f"{TOKEN_FILE_ENV} {bad}") from None


def _log_level(value: str) -> str:
    level = value.strip().upper()
    if level not in LOG_LEVELS:
        raise ValueError(
            f"SCADBUDDY_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, not {value!r}"
        )
    return level


class ProfileFetchSettings(BaseSettings):
    """The worker's (#6780): no 3DFP login and no browser."""

    model_config = SettingsConfigDict(env_prefix="SCADBUDDY_", extra="ignore")

    #: The CRUD service (the API process).
    api_internal_url: str
    service_token_file: Path | None = None
    service_bootstrap_file: Path | None = None
    temporal_address: str
    temporal_namespace: str = "default"
    temporal_task_queue_profile_fetch: str = DEFAULT_QUEUE
    #: The browser service's Service, e.g. ``http://scadbuddy-profile-fetch-browser:8091``.
    profile_fetch_browser_url: str
    #: The RPC token's file (`intake_worker.runner.read_token`: 32 to 1024 bytes of visible
    #: ASCII), from the Secret ``scadbuddy-profile-fetch-browser-rpc``.
    profile_fetch_browser_token_file: Path
    log_level: str = "INFO"
    _browser_token: str = PrivateAttr(default="")

    @property
    def browser_token(self) -> str:
        """The RPC token, read at start."""
        return self._browser_token

    @field_validator("api_internal_url")
    @classmethod
    def _api_internal_url_is_http(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError(f"SCADBUDDY_API_INTERNAL_URL must be an http(s) URL, not {value!r}")
        return value

    @field_validator("profile_fetch_browser_url")
    @classmethod
    def _browser_url_is_plain(cls, value: str) -> str:
        return _plain_http_url("SCADBUDDY_PROFILE_FETCH_BROWSER_URL", value)

    @field_validator("log_level")
    @classmethod
    def _log_level_known(cls, value: str) -> str:
        return _log_level(value)

    @model_validator(mode="after")
    def _one_proof(self) -> ProfileFetchSettings:
        if (self.service_token_file is None) == (self.service_bootstrap_file is None):
            raise ValueError(
                "set exactly one of SCADBUDDY_SERVICE_TOKEN_FILE and"
                " SCADBUDDY_SERVICE_BOOTSTRAP_FILE"
            )
        return self

    @model_validator(mode="after")
    def _read_token(self) -> ProfileFetchSettings:
        self._browser_token = _token(self.profile_fetch_browser_token_file)
        return self


class BrowserSettings(BaseSettings):
    """The browser service's (#6780): the 3DFP login, the RPC token, its port and the
    browser's profile dir. No proof, no Temporal and no API."""

    model_config = SettingsConfigDict(env_prefix="SCADBUDDY_", extra="ignore")

    #: The 3dfilamentprofiles.com account (plan Decision 15), both required.
    tdfp_email: SecretStr
    tdfp_password: SecretStr
    #: The browser's profile goes under it: a writable volume in a pod whose root file
    #: system is read-only.
    profile_fetch_work_dir: Path = Path("/tmp")
    profile_fetch_browser_port: int = Field(default=BROWSER_PORT, ge=1, le=65535)
    #: The RPC token's file: the Secret the worker reads too.
    profile_fetch_browser_token_file: Path
    log_level: str = "INFO"
    _browser_token: str = PrivateAttr(default="")

    @property
    def browser_token(self) -> str:
        """The RPC token, read at start."""
        return self._browser_token

    @property
    def profile_dir(self) -> Path:
        """The browser's profile: the site's cookies."""
        return self.profile_fetch_work_dir / "chromium-profile"

    @field_validator("tdfp_email", "tdfp_password")
    @classmethod
    def _not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("log_level")
    @classmethod
    def _log_level_known(cls, value: str) -> str:
        return _log_level(value)

    @model_validator(mode="after")
    def _read_token(self) -> BrowserSettings:
        self._browser_token = _token(self.profile_fetch_browser_token_file)
        return self
