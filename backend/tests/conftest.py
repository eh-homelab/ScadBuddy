from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.core.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


def load_fixture_param(stem: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{stem}.param").read_text(encoding="utf-8"))
    return data


def load_fixture_source(stem: str) -> str:
    return (FIXTURES / f"{stem}.scad").read_text(encoding="utf-8")


def openscad_binary() -> str | None:
    return shutil.which(load_config().openscad)


@pytest.fixture(autouse=True)
def _skip_without_openscad(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("requires_openscad") and openscad_binary() is None:
        pytest.skip("openscad is not on PATH")
