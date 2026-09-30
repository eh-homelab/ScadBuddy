from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.core.config import (
    ACTIVITY_TIMEOUT_MARGIN,
    available_cpus,
    default_solid_concurrency,
    load_config,
)
from scadbuddy.core.settings import Settings
from tests.conftest import UNUSED_DATABASE_URL


@pytest.fixture(autouse=True)
def _database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `Settings` needs one (#401); these tests are about the other fields."""
    monkeypatch.setenv("SCADBUDDY_DATABASE_URL", UNUSED_DATABASE_URL)


@pytest.mark.parametrize("value", [None, "", "  "])
def test_settings_refuse_to_start_without_a_database_url(
    value: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named, so the log of a pod that will not start says what to set (#401)."""
    if value is None:
        monkeypatch.delenv("SCADBUDDY_DATABASE_URL")
    else:
        monkeypatch.setenv("SCADBUDDY_DATABASE_URL", value)
    with pytest.raises(ValueError, match="SCADBUDDY_DATABASE_URL is required"):
        Settings()


@pytest.mark.parametrize(
    "variable",
    [
        "SCADBUDDY_TEMPORAL_ADDRESS",
        "SCADBUDDY_TEMPORAL_NAMESPACE",
        "SCADBUDDY_TEMPORAL_TASK_QUEUE_RENDER",
    ],
)
@pytest.mark.parametrize("value", ["  ", " temporal:7233", "temporal:7233\n"])
def test_a_temporal_setting_with_whitespace_is_refused_by_name(
    variable: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whitespace-only would read as "set" (the Temporal path) with a garbage value."""
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        Settings()


@pytest.mark.parametrize(
    "variable", ["SCADBUDDY_TEMPORAL_NAMESPACE", "SCADBUDDY_TEMPORAL_TASK_QUEUE_RENDER"]
)
def test_an_empty_temporal_namespace_or_queue_is_refused_by_name(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(variable, "")
    with pytest.raises(ValueError, match=variable):
        Settings()


def test_an_empty_temporal_address_keeps_the_legacy_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCADBUDDY_TEMPORAL_ADDRESS", "")
    assert Settings().temporal_address == ""


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_render_concurrency_below_one_is_refused_by_name(value: str) -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_RENDER_CONCURRENCY must be at least 1"):
        load_config({"SCADBUDDY_RENDER_CONCURRENCY": value})


def test_the_default_render_concurrency_loads() -> None:
    assert load_config({}).render_concurrency >= 1


def test_a_negative_lsp_sessions_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_LSP_SESSIONS must be at least 0"):
        load_config({"SCADBUDDY_LSP_SESSIONS": "-1"})


def test_a_negative_realtime_sockets_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_REALTIME_SOCKETS must be at least 0"):
        load_config({"SCADBUDDY_REALTIME_SOCKETS": "-1"})


def test_realtime_sockets_defaults_and_loads() -> None:
    assert load_config({}).realtime_sockets == 256
    assert load_config({"SCADBUDDY_REALTIME_SOCKETS": "3"}).realtime_sockets == 3


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
    ("cpus", "workers", "checks", "expected"),
    [
        (8, 2, 1, 3),  # the deployment today: 2 x 3 + 1 = 7 processes on 8 CPUs
        (9, 2, 1, 4),
        (2, 2, 1, 1),
        (1, 2, 1, 1),
        (3, 2, 0, 1),
        (64, 2, 1, 8),
        (4, 1, 0, 4),
        (4, 1, 1, 3),
    ],
)
def test_the_default_solid_concurrency_shares_the_cpus_the_checks_leave(
    cpus: int, workers: int, checks: int, expected: int
) -> None:
    assert default_solid_concurrency(cpus, workers, checks) == expected


def test_above_the_sequential_floor_the_derived_default_never_oversubscribes() -> None:
    for cpus in range(1, 65):
        for workers in range(1, 5):
            for checks in range(3):
                slots = default_solid_concurrency(cpus, workers, checks)
                if slots > 1:
                    assert workers * slots + checks <= cpus


@pytest.mark.parametrize(
    ("cpu_max", "limit"),
    [("200000 100000\n", 2), ("150000 100000\n", 2), ("50000 100000\n", 1)],
)
def test_available_cpus_honours_a_cgroup_v2_cpu_limit(
    cpu_max: str, limit: int, tmp_path: Path
) -> None:
    (tmp_path / "cpu.max").write_text(cpu_max, encoding="utf-8")
    uncapped = available_cpus(tmp_path / "nowhere")
    assert available_cpus(tmp_path) == min(uncapped, limit)


@pytest.mark.parametrize("controller", ["cpu", "cpu,cpuacct"])
def test_available_cpus_honours_a_cgroup_v1_cpu_limit(controller: str, tmp_path: Path) -> None:
    (tmp_path / controller).mkdir()
    (tmp_path / controller / "cpu.cfs_quota_us").write_text("100000\n", encoding="utf-8")
    (tmp_path / controller / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")
    assert available_cpus(tmp_path) == 1


def test_available_cpus_without_a_cgroup_limit_is_the_affinity_mask(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    v2 = tmp_path / "v2"
    v2.mkdir()
    (v2 / "cpu.max").write_text("max 100000\n", encoding="utf-8")
    v1 = tmp_path / "v1"
    (v1 / "cpu").mkdir(parents=True)
    (v1 / "cpu" / "cpu.cfs_quota_us").write_text("-1\n", encoding="utf-8")

    assert available_cpus(v2) == available_cpus(v1) == available_cpus(tmp_path / "none") >= 1
    # An explicit "no limit" is silent; no readable controller at all is not.
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "no cgroup CPU limit is readable" in warnings[0]
    assert str(tmp_path / "none") in warnings[0]


def test_an_explicit_solid_concurrency_wins_over_the_derived_one() -> None:
    config = load_config({"SCADBUDDY_SOLID_CONCURRENCY": "3"})
    assert config.solid_concurrency == 3
    assert config.solid_slots() == 3


def test_the_default_solid_concurrency_is_derived() -> None:
    config = load_config({})
    assert config.solid_concurrency == 0
    assert config.solid_slots() == default_solid_concurrency(
        available_cpus(), config.render_concurrency, config.check_concurrency
    )


def test_a_negative_solid_concurrency_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_SOLID_CONCURRENCY must be at least 0"):
        load_config({"SCADBUDDY_SOLID_CONCURRENCY": "-1"})
    monkeypatch.setenv("SCADBUDDY_SOLID_CONCURRENCY", "5")
    assert Settings().to_config().solid_concurrency == 5


def test_the_asset_caps_and_sweep_interval_take_zero() -> None:
    config = load_config(
        {
            "SCADBUDDY_ASSET_MAX_TOTAL_BYTES": "0",
            "SCADBUDDY_ASSET_MAX_COUNT": "0",
            "SCADBUDDY_ASSET_SWEEP_INTERVAL": "0",
        }
    )
    assert config.asset_max_total_bytes == 0
    assert config.asset_max_count == 0
    assert config.asset_sweep_interval == 0


@pytest.mark.parametrize(
    "name",
    [
        "SCADBUDDY_ASSET_MAX_TOTAL_BYTES",
        "SCADBUDDY_ASSET_MAX_COUNT",
        "SCADBUDDY_ASSET_SWEEP_INTERVAL",
    ],
)
def test_a_negative_asset_setting_is_refused_by_name(name: str) -> None:
    with pytest.raises(ValueError, match=f"{name} must be at least 0"):
        load_config({name: "-1"})


def test_an_asset_sweep_grace_under_an_hour_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_ASSET_SWEEP_GRACE must be at least 3600"):
        load_config({"SCADBUDDY_ASSET_SWEEP_GRACE": "60"})
    with pytest.raises(ValueError, match="SCADBUDDY_ASSET_SWEEP_GRACE must be at least 3600"):
        Settings(data_dir=tmp_path, asset_sweep_grace=0).to_config()


def test_the_duplicate_staging_max_age_defaults_to_an_hour_and_is_read() -> None:
    assert load_config({}).duplicate_staging_max_age == 3600
    config = load_config({"SCADBUDDY_DUPLICATE_STAGING_MAX_AGE": "600"})
    assert config.duplicate_staging_max_age == 600


@pytest.mark.parametrize("value", ["0", "0.5", "-1"])
def test_a_duplicate_staging_max_age_under_one_is_refused(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_DUPLICATE_STAGING_MAX_AGE must be at least 1"):
        load_config({"SCADBUDDY_DUPLICATE_STAGING_MAX_AGE": value})
    with pytest.raises(ValueError, match="SCADBUDDY_DUPLICATE_STAGING_MAX_AGE must be at least 1"):
        Settings(data_dir=tmp_path, duplicate_staging_max_age=float(value)).to_config()


def test_temporal_settings_reach_the_config() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        temporal_address="temporal:7233",
        render_timeout=45.0,
    )
    config = settings.to_config()
    assert config.temporal_address == "temporal:7233"
    assert config.temporal_namespace == "scadbuddy"
    assert config.temporal_task_queue_render == "render"
    assert config.activity_timeout == 45.0 + ACTIVITY_TIMEOUT_MARGIN


def test_load_config_reads_the_temporal_settings() -> None:
    config = load_config(
        {
            "SCADBUDDY_TEMPORAL_ADDRESS": "temporal:7233",
            "SCADBUDDY_TEMPORAL_NAMESPACE": "elsewhere",
            "SCADBUDDY_TEMPORAL_TASK_QUEUE_RENDER": "render-2",
        }
    )
    assert config.temporal_address == "temporal:7233"
    assert config.temporal_namespace == "elsewhere"
    assert config.temporal_task_queue_render == "render-2"


def test_load_config_defaults_the_temporal_settings() -> None:
    config = load_config({})
    assert config.temporal_address == ""
    assert config.temporal_namespace == "scadbuddy"
    assert config.temporal_task_queue_render == "render"


def test_the_temporal_ui_url_is_empty_by_default_and_seeded_by_its_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings().temporal_ui_url is None
    monkeypatch.setenv("SCADBUDDY_TEMPORAL_UI_URL", "")
    assert Settings().temporal_ui_url is None
    monkeypatch.setenv("SCADBUDDY_TEMPORAL_UI_URL", "https://temporal.lan")
    assert Settings().temporal_ui_url == "https://temporal.lan"


@pytest.mark.parametrize(
    "value",
    ["javascript:alert(1)", "temporal.lan", "ftp://temporal.lan", "https://", "http:// x"],
)
def test_a_temporal_ui_url_that_is_not_http_is_refused_by_name(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It becomes a link on the Settings page, so only an http(s) URL is one."""
    monkeypatch.setenv("SCADBUDDY_TEMPORAL_UI_URL", value)
    with pytest.raises(ValueError, match="SCADBUDDY_TEMPORAL_UI_URL"):
        Settings()
