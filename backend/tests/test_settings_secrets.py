"""The secret settings, sealed at rest under ``SCADBUDDY_SECRET_KEY_FILE`` (#602)."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from scadbuddy.core.secrets import SecretKeyError
from scadbuddy.core.settings import SECRET_FIELDS, Settings
from scadbuddy.library.settings_store import (
    SettingsPatch,
    SettingsStore,
    load_render_store_settings,
)
from tests.conftest import UNUSED_TEMPORAL_ADDRESS

KEY = base64.b64encode(b"\x5a" * 32).decode()
OTHER_KEY = base64.b64encode(b"\x01" * 32).decode()


def _key_file(tmp_path: Path, text: str, name: str = "secret.key") -> Path:
    path = tmp_path / name
    path.write_text(text + "\n")
    return path


def _settings(tmp_path: Path, pg_conninfo: str, key: Path | None, **extra: Any) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=pg_conninfo,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        secret_key_file=key,
        **extra,
    )


def _open(settings: Settings) -> SettingsStore:
    store = SettingsStore(settings)
    store.open()
    return store


@pytest.fixture
def keyed(tmp_path: Path, pg_conninfo: str) -> Settings:
    return _settings(tmp_path, pg_conninfo, _key_file(tmp_path, KEY))


@pytest.fixture
def plain(tmp_path: Path, pg_conninfo: str) -> Settings:
    return _settings(tmp_path, pg_conninfo, None)


@pytest.fixture
def store(keyed: Settings) -> Iterator[SettingsStore]:
    opened = _open(keyed)
    try:
        yield opened
    finally:
        opened.close()


def _rows(conninfo: str) -> dict[str, Any]:
    with psycopg.connect(conninfo) as conn:
        return {name: value for name, value in conn.execute("SELECT name, value FROM settings")}


def _write_row(conninfo: str, name: str, value: Any) -> None:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO settings (name, value) VALUES (%s, %s)"
            " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
            (name, Jsonb(value)),
        )


def _secret_free(caplog: pytest.LogCaptureFixture, *secrets: str) -> None:
    for record in caplog.records:
        text = record.getMessage() + json.dumps(record.__dict__, default=str)
        for secret in secrets:
            assert secret not in text


def test_a_saved_key_is_sealed_in_its_row_and_reads_back(
    store: SettingsStore, keyed: Settings
) -> None:
    store.save(
        SettingsPatch(
            bambuddy_api_key="full-key-123456",
            bambuddy_render_api_key="render-key-123456",
            google_fonts_api_key="fonts-key-123456",
        )
    )
    rows = _rows(keyed.database_url)
    for name in SECRET_FIELDS:
        assert set(rows[name]) == {"sealed"}
        assert set(rows[name]["sealed"]) == {"secret", "dek", "kek_id"}
    assert "123456" not in json.dumps(rows)

    loaded = store.load()
    assert loaded.bambuddy_api_key == "full-key-123456"
    assert loaded.bambuddy_render_api_key == "render-key-123456"
    assert store.snapshot().runtime.google_fonts_api_key == "fonts-key-123456"
    assert store.snapshot().sources["bambuddy_api_key"] == "stored"
    render = load_render_store_settings(store.pool, keyed)
    assert render.api_key == "render-key-123456"


def test_a_cleared_key_stays_a_null_row(store: SettingsStore, keyed: Settings) -> None:
    store.save(SettingsPatch(bambuddy_api_key="full-key-123456"))
    store.save(SettingsPatch(bambuddy_api_key=""))
    assert _rows(keyed.database_url)["bambuddy_api_key"] is None
    assert store.load().bambuddy_api_key is None


def test_a_sealed_value_moved_to_another_setting_does_not_open(
    store: SettingsStore, keyed: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    store.save(SettingsPatch(bambuddy_api_key="full-key-123456"))
    _write_row(
        keyed.database_url,
        "bambuddy_render_api_key",
        _rows(keyed.database_url)["bambuddy_api_key"],
    )
    with caplog.at_level(logging.WARNING):
        assert store.load().bambuddy_render_api_key is None
    assert any(getattr(r, "setting", None) == "bambuddy_render_api_key" for r in caplog.records)
    _secret_free(caplog, "full-key-123456")


def test_plaintext_rows_are_sealed_at_start_and_only_once(
    plain: Settings, keyed: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    before = _open(plain)
    try:
        before.save(SettingsPatch(bambuddy_api_key="full-key-123456", bambuddy_render_api_key=""))
    finally:
        before.close()
    assert _rows(plain.database_url)["bambuddy_api_key"] == "full-key-123456"

    with caplog.at_level(logging.INFO):
        after = _open(keyed)
    try:
        rows = _rows(keyed.database_url)
        assert set(rows["bambuddy_api_key"]) == {"sealed"}
        assert rows["bambuddy_render_api_key"] is None
        assert after.load().bambuddy_api_key == "full-key-123456"
    finally:
        after.close()
    _secret_free(caplog, "full-key-123456")

    again = _open(keyed)
    try:
        assert _rows(keyed.database_url) == rows
    finally:
        again.close()


def test_a_plaintext_save_beside_sealed_rows_reads_back_with_the_key(
    store: SettingsStore, plain: Settings, keyed: Settings
) -> None:
    """Mid-rollout (#1900): a process without the key saves while another has sealed."""
    store.save(SettingsPatch(bambuddy_api_key="full-key-123456"))
    unkeyed = _open(plain)
    try:
        unkeyed.save(SettingsPatch(bambuddy_render_api_key="render-key-123456"))
    finally:
        unkeyed.close()
    rows = _rows(keyed.database_url)
    assert rows["bambuddy_render_api_key"] == "render-key-123456"
    assert set(rows["bambuddy_api_key"]) == {"sealed"}
    loaded = store.load()
    assert loaded.bambuddy_api_key == "full-key-123456"
    assert loaded.bambuddy_render_api_key == "render-key-123456"
    assert load_render_store_settings(store.pool, keyed).api_key == "render-key-123456"


def test_without_a_key_file_keys_stay_plaintext_with_one_warning(
    plain: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        store = _open(plain)
    try:
        store.save(SettingsPatch(bambuddy_api_key="full-key-123456"))
        assert _rows(plain.database_url)["bambuddy_api_key"] == "full-key-123456"
        assert store.load().bambuddy_api_key == "full-key-123456"
    finally:
        store.close()
    warnings = [r for r in caplog.records if "SCADBUDDY_SECRET_KEY_FILE" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.parametrize("reader_key", [None, OTHER_KEY])
def test_a_sealed_row_this_process_cannot_open_follows_the_environment(
    tmp_path: Path,
    pg_conninfo: str,
    store: SettingsStore,
    reader_key: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store.save(SettingsPatch(bambuddy_api_key="full-key-123456"))
    key = None if reader_key is None else _key_file(tmp_path, reader_key, "other.key")
    reader_settings = _settings(tmp_path, pg_conninfo, key, bambuddy_api_key="from-env")
    with caplog.at_level(logging.WARNING):
        reader = _open(reader_settings)
        try:
            snapshot = reader.snapshot()
            assert snapshot.stored.bambuddy_api_key == "from-env"
            assert snapshot.sources["bambuddy_api_key"] == "env"
            render = load_render_store_settings(reader.pool, reader_settings)
            assert render.api_key == "from-env"
        finally:
            reader.close()
    assert any(getattr(r, "setting", None) == "bambuddy_api_key" for r in caplog.records)
    _secret_free(caplog, "full-key-123456", KEY)


def test_a_malformed_key_file_fails_the_start(tmp_path: Path, pg_conninfo: str) -> None:
    settings = _settings(tmp_path, pg_conninfo, _key_file(tmp_path, "not a key"))
    with pytest.raises(SecretKeyError):
        SettingsStore(settings)
    with pytest.raises(SecretKeyError):
        load_render_store_settings(None, settings)  # type: ignore[arg-type]
