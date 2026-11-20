"""The profile-fetch worker's own settings (#6625 Task 6). It has no database, key, store or
Bambuddy field: it reaches the CRUD service, Temporal and 3dfilamentprofiles.com only. The
3DFP login is two `SecretStr`s, so no repr, log line or dump of the settings shows it."""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from scadbuddy.intake_worker.settings import FORBIDDEN_ENV, LOG_LEVELS
from scadbuddy.profile_fetch.wire import DEFAULT_QUEUE

HEALTH_PORT: Final = 9090
#: Playwright's debug log (`DEBUG=pw:api`, `pw:protocol`, …) writes each call with its
#: arguments to stderr, a filled password among them. These are its channels: a ``DEBUG``
#: entry that would turn one on refuses the start.
PLAYWRIGHT_CHANNELS: Final = ("pw:api", "pw:protocol", "pw:browser", "pw:channel")

#: The 3DFP login's variables. `forget_login` removes them once the settings hold it.
LOGIN_ENV: Final = ("SCADBUDDY_TDFP_EMAIL", "SCADBUDDY_TDFP_PASSWORD")

__all__ = [
    "FORBIDDEN_ENV",
    "HEALTH_PORT",
    "LOGIN_ENV",
    "ProfileFetchRefusal",
    "ProfileFetchSettings",
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
    reasons = [
        f"{name} is set; the profile-fetch worker holds no database secret or key"
        for name in FORBIDDEN_ENV
        if name in environ
    ]
    if _debug_names_playwright(environ.get("DEBUG", "")):
        reasons.append(
            "DEBUG names a Playwright channel; its log would write the 3DFP login to stderr"
        )
    if reasons:
        raise ProfileFetchRefusal(reasons)


def forget_login(environ: MutableMapping[str, str]) -> None:
    """Remove the 3DFP login from ``environ`` (``os.environ``) once the settings read it,
    so no process the worker starts (Playwright's driver, the browser) inherits it
    (Task 6 review, I1). pydantic-settings reads names without regard to case, so every
    spelling of them goes."""
    for name in [name for name in environ if name.upper() in LOGIN_ENV]:
        del environ[name]


class ProfileFetchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCADBUDDY_", extra="ignore")

    #: The CRUD service (the API process).
    api_internal_url: str
    service_token_file: Path | None = None
    service_bootstrap_file: Path | None = None
    temporal_address: str
    temporal_namespace: str = "default"
    temporal_task_queue_profile_fetch: str = DEFAULT_QUEUE
    #: The 3dfilamentprofiles.com account (plan Decision 15), both required.
    tdfp_email: SecretStr
    tdfp_password: SecretStr
    #: The browser's profile goes under it: a writable volume in a pod whose root file
    #: system is read-only.
    profile_fetch_work_dir: Path = Path("/tmp")
    log_level: str = "INFO"

    @field_validator("api_internal_url")
    @classmethod
    def _api_internal_url_is_http(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError(f"SCADBUDDY_API_INTERNAL_URL must be an http(s) URL, not {value!r}")
        return value

    @field_validator("tdfp_email", "tdfp_password")
    @classmethod
    def _not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("log_level")
    @classmethod
    def _log_level_known(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in LOG_LEVELS:
            raise ValueError(
                f"SCADBUDDY_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, not {value!r}"
            )
        return level

    @model_validator(mode="after")
    def _one_proof(self) -> ProfileFetchSettings:
        if (self.service_token_file is None) == (self.service_bootstrap_file is None):
            raise ValueError(
                "set exactly one of SCADBUDDY_SERVICE_TOKEN_FILE and"
                " SCADBUDDY_SERVICE_BOOTSTRAP_FILE"
            )
        return self

    @property
    def profile_dir(self) -> Path:
        """The browser's profile: the site's cookies."""
        return self.profile_fetch_work_dir / "chromium-profile"
