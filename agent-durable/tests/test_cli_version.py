"""The image's build-time assertion that the bundled Claude Code is the pinned one."""

from __future__ import annotations

import pytest
from check_cli_version import main
from claude_agent_sdk._cli_version import __cli_version__


def test_matching_pin_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([__cli_version__]) == 0
    assert __cli_version__ in capsys.readouterr().out


def test_pin_matches_the_locked_sdk() -> None:
    assert main(["2.1.283"]) == 0


def test_mismatch_fails_and_names_both_versions(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["2.1.999"]) == 1
    err = capsys.readouterr().err
    assert "2.1.999" in err
    assert __cli_version__ in err


def test_no_argument_is_a_usage_error() -> None:
    assert main([]) == 2
