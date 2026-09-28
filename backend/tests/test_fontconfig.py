from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.core.fontconfig import (
    PASSTHROUGH,
    cache_dir,
    conf_path,
    env_for,
    fonts_dir,
    minimal_env,
    write_conf,
)


def test_the_fonts_directory_hangs_off_the_data_dir() -> None:
    assert fonts_dir(Path("/data")) == Path("/data/fonts")
    assert conf_path(Path("/data")) == Path("/data/fonts/fonts.conf")


def test_write_conf_creates_the_directories_and_names_them_in_the_file(tmp_path: Path) -> None:
    written = write_conf(tmp_path)

    assert written == conf_path(tmp_path)
    assert fonts_dir(tmp_path).is_dir()
    assert cache_dir(tmp_path).is_dir()
    body = written.read_text(encoding="utf-8")
    assert f"<dir>{fonts_dir(tmp_path)}</dir>" in body
    assert f"<cachedir>{cache_dir(tmp_path)}</cachedir>" in body


def test_the_cache_directory_is_declared_before_the_system_include(tmp_path: Path) -> None:
    body = write_conf(tmp_path).read_text(encoding="utf-8")
    assert body.index("<cachedir>") < body.index("<include")


def test_the_system_config_is_included_and_may_be_missing(tmp_path: Path) -> None:
    body = write_conf(tmp_path).read_text(encoding="utf-8")
    assert '<include ignore_missing="yes">/etc/fonts/fonts.conf</include>' in body


def test_env_points_at_the_config_once_it_exists(tmp_path: Path) -> None:
    assert "FONTCONFIG_FILE" not in env_for(tmp_path, base={})
    write_conf(tmp_path)
    assert env_for(tmp_path, base={}) == {"FONTCONFIG_FILE": str(conf_path(tmp_path))}


def test_env_keeps_what_the_child_needs(tmp_path: Path) -> None:
    write_conf(tmp_path)
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/scadbuddy",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_CACHE_HOME": "/tmp/cache",
        "TMPDIR": "/tmp",
    }
    assert env_for(tmp_path, base=base) == base | {"FONTCONFIG_FILE": str(conf_path(tmp_path))}


def test_minimal_env_keeps_exactly_the_allowlist() -> None:
    """Every PASSTHROUGH name and LC_* survives; the allowlist is the contract."""
    base = {name: f"/value/{name}" for name in PASSTHROUGH} | {
        "LC_CTYPE": "C.UTF-8",
        "LC_NUMERIC": "C",
        "FONTCONFIG_PATH": "/etc/fonts",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "XDG_DATA_DIRS": "/usr/share",
    }
    assert minimal_env(base) == base
    assert {"FONTCONFIG_PATH", "FONTCONFIG_FILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME"} <= PASSTHROUGH


def test_env_drops_everything_else(tmp_path: Path) -> None:
    """#281: a template can read /proc/self/environ through import()."""
    base = {
        "PATH": "/usr/bin",
        "SCADBUDDY_BAMBUDDY_API_KEY": "hunter2",
        "SCADBUDDY_DATABASE_URL": "postgresql://u:p@db/x",
        "AWS_SECRET_ACCESS_KEY": "shh",
        "OPENSCADPATH": "/somewhere",
    }
    assert env_for(tmp_path, base=base) == {"PATH": "/usr/bin"}


def test_env_defaults_to_the_filtered_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCADBUDDY_BAMBUDDY_API_KEY", "hunter2")
    monkeypatch.setenv("HOME", "/home/scadbuddy")
    env = env_for(tmp_path)
    assert "SCADBUDDY_BAMBUDDY_API_KEY" not in env
    assert env["HOME"] == "/home/scadbuddy"
