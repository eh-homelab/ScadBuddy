from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.core.config import load_config
from scadbuddy.core.settings import Settings


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_render_concurrency_below_one_is_refused_by_name(value: str) -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_RENDER_CONCURRENCY must be at least 1"):
        load_config({"SCADBUDDY_RENDER_CONCURRENCY": value})


def test_the_default_render_concurrency_loads() -> None:
    assert load_config({}).render_concurrency >= 1


def test_a_negative_lsp_sessions_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_LSP_SESSIONS must be at least 0"):
        load_config({"SCADBUDDY_LSP_SESSIONS": "-1"})


def test_zero_lsp_sessions_loads() -> None:
    assert load_config({"SCADBUDDY_LSP_SESSIONS": "0"}).lsp_sessions == 0


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_database_pool_size_below_one_is_refused_by_name(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCADBUDDY_DATABASE_POOL_SIZE", value)
    with pytest.raises(ValueError, match="SCADBUDDY_DATABASE_POOL_SIZE must be at least 1"):
        Settings()


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_library_max_bytes_below_one_is_refused_by_name(value: str) -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_LIBRARY_MAX_BYTES must be at least 1"):
        load_config({"SCADBUDDY_LIBRARY_MAX_BYTES": value})


def test_a_library_max_bytes_below_one_is_refused_through_settings(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, library_max_bytes=0)
    with pytest.raises(ValueError, match="SCADBUDDY_LIBRARY_MAX_BYTES must be at least 1"):
        settings.to_config()
