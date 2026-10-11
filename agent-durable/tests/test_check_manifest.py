from __future__ import annotations

import json
from pathlib import Path

import pytest

from scadbuddy_durable.check_manifest import main

ENTRY = {
    "name": "models_list",
    "description": "List the models.",
    "input_schema": {"type": "object"},
    "tier": "read",
}


def write(tmp_path: Path, data: object) -> str:
    path = tmp_path / "tools.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_a_valid_manifest_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([write(tmp_path, [ENTRY])]) == 0
    assert "1 tools" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ([], "names no tool"),
        ({"name": "x"}, "not a list"),
        ([{"name": "x"}], "KeyError"),
        ([{**ENTRY, "hitl": "maybe"}], "unknown hitl"),
    ],
)
def test_a_bad_manifest_fails_naming_path_and_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], data: object, reason: str
) -> None:
    path = write(tmp_path, data)
    assert main([path]) == 1
    err = capsys.readouterr().err
    assert path in err
    assert reason in err


def test_a_missing_file_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = str(tmp_path / "absent.json")
    assert main([path]) == 1
    assert "FileNotFoundError" in capsys.readouterr().err


def test_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
