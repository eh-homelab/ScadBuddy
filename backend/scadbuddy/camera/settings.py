"""The camera relay's, recorder's and live edge's settings (printer camera recording, phases
1 and 3). No camera process has a database, key or SQL client: each reaches the CRUD service
over HTTP with a service credential, and refuses to start with a database secret."""

from __future__ import annotations

import socket
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from scadbuddy.intake_worker.settings import FORBIDDEN_ENV


class CameraRefusal(Exception):  # noqa: N818  (a refusal to start, not an error)
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _forbidden(environ: Mapping[str, str], process: str) -> list[str]:
    return [
        f"{name} is set; the camera {process} holds no database secret or key"
        for name in (*FORBIDDEN_ENV, "SCADBUDDY_SECRET_KEY_PREVIOUS_FILE")
        if name in environ
    ]


class RelaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCADBUDDY_", extra="ignore")

    #: The CRUD service (the API process).
    api_internal_url: str
    service_token_file: Path | None = None
    service_bootstrap_file: Path | None = None
    #: MediaMTX's control API, in the same pod.
    camera_mediamtx_api_url: str = "http://127.0.0.1:9997"
    log_level: str = "INFO"

    @staticmethod
    def check_environment(environ: Mapping[str, str]) -> None:
        reasons = _forbidden(environ, "relay")
        if reasons:
            raise CameraRefusal(reasons)


class RecorderSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCADBUDDY_", extra="ignore")

    api_internal_url: str
    service_token_file: Path | None = None
    service_bootstrap_file: Path | None = None
    camera_mediamtx_api_url: str = "http://127.0.0.1:9997"
    camera_s3_endpoint: str
    camera_s3_bucket: str
    camera_s3_prefix: str = ""
    camera_s3_access_key: SecretStr
    camera_s3_secret_key: SecretStr
    camera_disk_max_bytes: int = 2 * 1024**3
    camera_recorder_name: str = Field(default_factory=socket.gethostname)
    #: The relay's RTSP address, e.g. ``rtsp://scadbuddy-camera-relay:8554``.
    camera_relay_rtsp_url: str
    camera_relay_read_user: str
    camera_relay_read_pass: SecretStr
    log_level: str = "INFO"

    @staticmethod
    def check_environment(environ: Mapping[str, str]) -> None:
        """MediaMTX names segment files in local time, so the pod must run in UTC."""
        reasons = _forbidden(environ, "recorder")
        if environ.get("TZ", "UTC") != "UTC":
            reasons.append(
                f"TZ is {environ['TZ']!r}; segment times are read as UTC, so TZ must be UTC"
            )
        if reasons:
            raise CameraRefusal(reasons)


class EdgeSettings(BaseSettings):
    """The live edge (camera phase 3): it reads the relay with the relay read secret, as the
    recorders do, and holds no access code and no bucket keys."""

    model_config = SettingsConfigDict(env_prefix="SCADBUDDY_", extra="ignore")

    api_internal_url: str
    service_token_file: Path | None = None
    service_bootstrap_file: Path | None = None
    camera_mediamtx_api_url: str = "http://127.0.0.1:9997"
    #: The relay's RTSP address, e.g. ``rtsp://scadbuddy-camera-relay:8554``.
    camera_relay_rtsp_url: str
    camera_relay_read_user: str
    camera_relay_read_pass: SecretStr
    log_level: str = "INFO"

    @staticmethod
    def check_environment(environ: Mapping[str, str]) -> None:
        reasons = _forbidden(environ, "edge")
        if reasons:
            raise CameraRefusal(reasons)
