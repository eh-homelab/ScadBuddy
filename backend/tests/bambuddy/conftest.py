from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig

RECORDINGS = Path(__file__).parent / "recordings"
BASE_URL = "https://bambuddy.test"


def recording(name: str) -> Any:
    """A response body recorded from the live Bambuddy; see recordings/README.md."""
    return json.loads((RECORDINGS / name).read_text(encoding="utf-8"))


@pytest.fixture
def config() -> BambuddyConfig:
    # No poll delay: await_slice is driven through respx, not a real slicer.
    return BambuddyConfig(
        base_url=BASE_URL, api_key="s3cret", slice_timeout=1.0, slice_poll_interval=0.0
    )


@pytest.fixture
async def bambuddy(config: BambuddyConfig) -> Any:
    async with BambuddyClient(config) as client:
        yield client
