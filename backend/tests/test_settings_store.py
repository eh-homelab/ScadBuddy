"""The upload-limit setting (#274): env-seeded like the Bambuddy fields, overridden
from the Settings page, and put back to the environment's value by a clear."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from scadbuddy.core.config import DEFAULT_MEDIA_UPLOAD_MAX_BYTES
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore


def test_the_upload_limit_defaults_to_one_gibibyte(tmp_path: Path) -> None:
    assert DEFAULT_MEDIA_UPLOAD_MAX_BYTES == 1024**3
    store = SettingsStore(tmp_path / "settings.json", Settings(data_dir=tmp_path))

    assert store.load().media_upload_max_bytes == DEFAULT_MEDIA_UPLOAD_MAX_BYTES


def test_the_environment_seeds_the_upload_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES", "5000")
    store = SettingsStore(tmp_path / "settings.json", Settings(data_dir=tmp_path))

    assert store.load().media_upload_max_bytes == 5000


def test_the_ui_overrides_the_environments_limit_and_a_clear_gives_it_back(
    tmp_path: Path,
) -> None:
    store = SettingsStore(
        tmp_path / "settings.json", Settings(data_dir=tmp_path, media_upload_max_bytes=5000)
    )

    assert store.save(SettingsPatch(media_upload_max_bytes=7000)).media_upload_max_bytes == 7000
    assert store.load().media_upload_max_bytes == 7000

    cleared = store.save(SettingsPatch.model_validate({"media_upload_max_bytes": None}))
    assert cleared.media_upload_max_bytes == 5000


def test_a_settings_file_from_before_the_limit_still_follows_the_environment(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"public_url": "https://scad.example"}), encoding="utf-8")
    store = SettingsStore(path, Settings(data_dir=tmp_path, media_upload_max_bytes=5000))

    assert store.load().media_upload_max_bytes == 5000


@pytest.mark.parametrize("value", [0, -1])
def test_a_limit_must_be_positive(value: int) -> None:
    with pytest.raises(ValidationError):
        SettingsPatch(media_upload_max_bytes=value)
    with pytest.raises(ValidationError):
        Settings(media_upload_max_bytes=value)


def test_a_cleared_limit_keeps_following_the_environment(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    SettingsStore(path, Settings(data_dir=tmp_path, media_upload_max_bytes=5000)).save(
        SettingsPatch.model_validate({"media_upload_max_bytes": None})
    )

    store = SettingsStore(path, Settings(data_dir=tmp_path, media_upload_max_bytes=9000))

    assert store.load().media_upload_max_bytes == 9000
