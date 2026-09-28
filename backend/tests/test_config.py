from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.core.config import available_cpus, default_solid_concurrency, load_config
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


@pytest.mark.parametrize(
    ("cpus", "workers", "expected"),
    [(8, 2, 4), (2, 2, 1), (1, 2, 1), (3, 2, 1), (64, 2, 8), (4, 1, 4)],
)
def test_the_default_solid_concurrency_shares_the_cpus_between_the_workers(
    cpus: int, workers: int, expected: int
) -> None:
    assert default_solid_concurrency(cpus, workers) == expected


@pytest.mark.parametrize(
    ("cpu_max", "limit"),
    [("200000 100000\n", 2), ("150000 100000\n", 2), ("50000 100000\n", 1)],
)
def test_available_cpus_honours_a_cgroup_cpu_limit(
    cpu_max: str, limit: int, tmp_path: Path
) -> None:
    (tmp_path / "cpu.max").write_text(cpu_max, encoding="utf-8")
    uncapped = available_cpus(tmp_path / "nowhere")
    assert available_cpus(tmp_path) == min(uncapped, limit)


def test_available_cpus_without_a_cgroup_limit_is_the_affinity_mask(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("max 100000\n", encoding="utf-8")
    assert available_cpus(tmp_path) == available_cpus(tmp_path / "nowhere") >= 1


def test_an_explicit_solid_concurrency_wins_over_the_derived_one() -> None:
    config = load_config({"SCADBUDDY_SOLID_CONCURRENCY": "3"})
    assert config.solid_concurrency == 3
    assert config.solid_slots() == 3


def test_the_default_solid_concurrency_is_derived() -> None:
    config = load_config({})
    assert config.solid_concurrency == 0
    assert config.solid_slots() == default_solid_concurrency(
        available_cpus(), config.render_concurrency
    )


def test_a_negative_solid_concurrency_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_SOLID_CONCURRENCY must be at least 0"):
        load_config({"SCADBUDDY_SOLID_CONCURRENCY": "-1"})
    monkeypatch.setenv("SCADBUDDY_SOLID_CONCURRENCY", "5")
    assert Settings().to_config().solid_concurrency == 5
