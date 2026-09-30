"""Every bundled template's `ui` names a module it ships (spec 2026-09-27 §4.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scadbuddy.library.catalogue import UiDeclaration

MODELS = Path(__file__).parents[2] / "models"
DECLARED = [
    path
    for path in sorted(MODELS.glob("*/model.json"))
    if "ui" in json.loads(path.read_text(encoding="utf-8"))
]


@pytest.mark.parametrize("meta", DECLARED, ids=lambda path: path.parent.name)
def test_a_bundled_ui_is_valid_and_present(meta: Path) -> None:
    ui = UiDeclaration.model_validate(json.loads(meta.read_text(encoding="utf-8"))["ui"])
    assert ui.api == 1
    assert (meta.parent / ui.module).is_file()
