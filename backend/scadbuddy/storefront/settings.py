"""The edge's own settings. It has no database, key, Temporal or store field (spec §10)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from scadbuddy.core.proxies import parse_cidr_list
from scadbuddy.core.urls import https_origin

#: The edge refuses to start when any of these is set: it must never hold a database
#: secret or the KEK's file.
FORBIDDEN_ENV: Final = (
    "SCADBUDDY_DATABASE_URL",
    "PGPASSWORD",
    "PGPASSFILE",
    "SCADBUDDY_SECRET_KEY_FILE",
)
STOREFRONT_PORT: Final = 8083
STOREFRONT_PREFIX: Final = "/api/store/v1"
MIN_COOKIE_KEY_BYTES: Final = 32
LOG_LEVELS: Final = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


class EdgeRefusal(Exception):  # noqa: N818  (a refusal to start, not an error)
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def check_environment(environ: Mapping[str, str]) -> None:
    reasons = [
        f"{name} is set; the storefront holds no database secret or key"
        for name in FORBIDDEN_ENV
        if name in environ
    ]
    if reasons:
        raise EdgeRefusal(reasons)


class StorefrontSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCADBUDDY_", extra="ignore")

    #: The CRUD service (the API process).
    api_internal_url: str
    service_token_file: Path | None = None
    service_bootstrap_file: Path | None = None
    #: The cookie's signing key: its own, never the KEK.
    storefront_cookie_key_file: Path
    #: The page's origin, as the browser sees it: a write's ``Origin`` must be it
    #: (`app.py` `CrossSiteGate`).
    storefront_public_url: str
    trusted_proxies: str = ""
    log_level: str = "INFO"

    @field_validator("api_internal_url")
    @classmethod
    def _api_internal_url_is_http(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError(f"SCADBUDDY_API_INTERNAL_URL must be an http(s) URL, not {value!r}")
        return value

    @field_validator("storefront_public_url")
    @classmethod
    def _public_url_is_an_https_origin(cls, value: str) -> str:
        return https_origin(value, "SCADBUDDY_STOREFRONT_PUBLIC_URL")

    @field_validator("trusted_proxies")
    @classmethod
    def _trusted_proxies_are_cidrs(cls, value: str) -> str:
        parse_cidr_list(value)
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
    def _one_proof_and_a_strong_key(self) -> StorefrontSettings:
        if (self.service_token_file is None) == (self.service_bootstrap_file is None):
            raise ValueError(
                "set exactly one of SCADBUDDY_SERVICE_TOKEN_FILE and"
                " SCADBUDDY_SERVICE_BOOTSTRAP_FILE"
            )
        try:
            key = self.storefront_cookie_key_file.read_bytes()
        except OSError as error:
            raise ValueError(
                f"SCADBUDDY_STOREFRONT_COOKIE_KEY_FILE cannot be read ({type(error).__name__})"
            ) from error
        if len(key) < MIN_COOKIE_KEY_BYTES:
            raise ValueError(
                f"SCADBUDDY_STOREFRONT_COOKIE_KEY_FILE must hold at least"
                f" {MIN_COOKIE_KEY_BYTES} bytes"
            )
        return self
