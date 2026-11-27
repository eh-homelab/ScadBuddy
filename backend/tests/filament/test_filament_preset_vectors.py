"""The filament preset's credential keys and key diff, which Go core's copy reads too
(#7204): each key, each document and each diff written here by Python's own functions, so
Go strips and compares as Python does.

`python -m tests.filament.test_filament_preset_vectors` (or
`UPDATE_FILAMENT_PRESET_VECTORS=1 uv run --frozen pytest
tests/filament/test_filament_preset_vectors.py`) writes the file again. Without the
variable, the test checks that the file is what these functions write."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from scadbuddy.core.credentials import is_credential, strip_credentials

FILE = Path(__file__).parents[1] / "fixtures" / "filament-preset-vectors.json"

KEYS = [
    "printhost_apikey",
    "PrintHost_Password",
    "api_token",
    "secret_sauce",
    "nozzle_temperature",
    "print_host",
    "",
]

DOCUMENTS: list[dict[str, Any]] = [
    {"name": "PLA", "nozzle_temperature": ["220"], "printhost_apikey": "k"},
    {
        "name": "nested",
        "a": {"b": {"c": 1, "api_token": "t", "keep": [1, 2]}, "PrintHost_Password": "p"},
        "list": [{"x": 1, "secret_sauce": "s"}, {"y": [{"z": 2, "printhost_apikey": "k"}]}],
        "plain": [1, "two", None, True],
    },
    {"l1": {"l2": {"l3": {"printhost_password": "deep", "ok": "yes"}}}},
    {},
]

DIFFS: list[tuple[dict[str, Any], dict[str, Any]]] = [
    ({"a": 1, "b": 2}, {"a": 1, "b": 3}),
    ({"a": 1}, {"a": 1, "b": 2}),
    ({"a": 1, "b": 2}, {"a": 1}),
    ({"t": ["220", "230"]}, {"t": ["220", "240"]}),
    ({"a": 1, "printhost_apikey": "old"}, {"a": 1}),
    ({"a": 1, "printhost_apikey": "old"}, {"a": 2, "printhost_apikey": "new"}),
    ({}, {}),
]


def _changed(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """The rule of `profile_fetch._changed_keys`, over the stripped documents."""
    return sorted(k for k in {*old, *new} if old.get(k) != new.get(k))


def build() -> dict[str, Any]:
    return {
        "credential_keys": [{"key": k, "credential": is_credential(k)} for k in KEYS],
        "documents": [{"in": d, "out": strip_credentials(d)} for d in DOCUMENTS],
        "diffs": [
            {"old": o, "new": n, "changed": _changed(strip_credentials(o), strip_credentials(n))}
            for o, n in DIFFS
        ],
    }


def _text() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    FILE.write_text(_text(), encoding="utf-8")


def test_the_vectors_are_pythons() -> None:
    if os.environ.get("UPDATE_FILAMENT_PRESET_VECTORS"):
        main()
    assert FILE.read_text(encoding="utf-8") == _text()


if __name__ == "__main__":
    main()
