import pytest

from scadbuddy_durable.check_cli_version import (
    MINIMUM,
    bundled_cli_path,
    declared_cli_version,
    parse_cli_version,
)


def test_parses_claude_codes_version_line() -> None:
    assert parse_cli_version("2.1.292 (Claude Code)\n") == "2.1.292"


@pytest.mark.parametrize("out", ["", "claude 2.1.292", "2.1 (Claude Code)"])
def test_refuses_anything_else(out: str) -> None:
    with pytest.raises(ValueError, match="unrecognised"):
        parse_cli_version(out)


def test_the_pinned_sdk_declares_its_cli_and_meets_the_plugins_floor() -> None:
    declared = declared_cli_version()
    assert declared == "2.1.292"
    assert tuple(map(int, declared.split("."))) >= MINIMUM


def test_the_bundled_binary_is_where_the_sdk_keeps_it() -> None:
    path = bundled_cli_path()
    assert path.name == "claude"
    assert path.parent.name == "_bundled"
