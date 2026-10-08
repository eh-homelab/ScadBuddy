import json
from pathlib import Path
from typing import Any

import pytest

VECTORS = (
    Path(__file__).resolve().parents[2] / "agent" / "test" / "fixtures" / "secret-vectors.json"
)


@pytest.fixture(scope="session")
def vectors() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(VECTORS.read_text())
    return data
