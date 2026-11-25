from __future__ import annotations

import pytest

from scadbuddy.camera.settings import CameraRefusal, RecorderSettings, RelaySettings


@pytest.mark.parametrize(
    "name",
    ["SCADBUDDY_DATABASE_URL", "SCADBUDDY_SECRET_KEY_FILE", "SCADBUDDY_SECRET_KEY_PREVIOUS_FILE"],
)
def test_recorder_refuses_database_secrets(name: str) -> None:
    with pytest.raises(CameraRefusal, match=name):
        RecorderSettings.check_environment({name: "x"})


def test_relay_refuses_database_url() -> None:
    with pytest.raises(CameraRefusal, match="SCADBUDDY_DATABASE_URL"):
        RelaySettings.check_environment({"SCADBUDDY_DATABASE_URL": "postgresql://x"})


def test_recorder_refuses_non_utc_tz() -> None:
    with pytest.raises(CameraRefusal, match="TZ"):
        RecorderSettings.check_environment({"TZ": "America/New_York"})
    RecorderSettings.check_environment({"TZ": "UTC"})
    RecorderSettings.check_environment({})


def test_recorder_settings_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "API_INTERNAL_URL": "http://api",
        "CAMERA_S3_ENDPOINT": "http://s3",
        "CAMERA_S3_BUCKET": "b",
        "CAMERA_S3_ACCESS_KEY": "ak",
        "CAMERA_S3_SECRET_KEY": "s3-secret-value",
        "CAMERA_RELAY_RTSP_URL": "rtsp://relay:8554",
        "CAMERA_RELAY_READ_USER": "u",
        "CAMERA_RELAY_READ_PASS": "p",
    }.items():
        monkeypatch.setenv(f"SCADBUDDY_{key}", value)
    settings = RecorderSettings()  # type: ignore[call-arg]
    assert settings.camera_disk_max_bytes == 2 * 1024**3
    assert settings.camera_recorder_name
    assert "s3-secret-value" not in repr(settings)
