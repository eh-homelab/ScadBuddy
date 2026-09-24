from __future__ import annotations

from pathlib import Path

from scadbuddy.core.fontconfig import cache_dir, conf_path, env_for, fonts_dir, write_conf


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


def test_env_keeps_the_rest_of_the_environment(tmp_path: Path) -> None:
    write_conf(tmp_path)
    assert env_for(tmp_path, base={"HOME": "/home/scadbuddy"})["HOME"] == "/home/scadbuddy"
