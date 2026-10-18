from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scadbuddy.store.content_models import BlobScope
from scadbuddy.store.local import LocalContentBackend

SCOPE = BlobScope(slug="demo", title="Demo")


async def test_a_slice_blob_round_trips(tmp_path: Path) -> None:
    backend = LocalContentBackend(tmp_path)
    key = await backend.upload("slice", b"gcode", name="plate.3mf", scope=SCOPE)
    assert key == f"slice/{hashlib.sha256(b'gcode').hexdigest()}"
    assert b"".join([c async for c in backend.download(key)]) == b"gcode"


async def test_an_unknown_kind_is_refused(tmp_path: Path) -> None:
    backend = LocalContentBackend(tmp_path)
    with pytest.raises(ValueError, match="not a local object id"):
        await backend.exists("model/" + "0" * 64)
