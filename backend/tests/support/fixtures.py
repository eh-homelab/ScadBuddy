"""Where the repo's real-file fixtures are (`test-fixtures/` at the repo root, Git LFS).

They are outside `backend/` so no image carries them (the app stage copies all of
`backend/`): CI mounts them into the test container at `/app/test-fixtures`
(`.github/workflows/ci.yml`), and a checkout finds them beside `backend/`.
`SCADBUDDY_TEST_FIXTURES` names another place. A checkout without `git lfs pull` holds
only pointer files, which count as absent, so `requires_fixtures` tests skip.
"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

#: How a Git LFS pointer file starts (https://github.com/git-lfs/git-lfs/blob/main/docs/spec.md).
LFS_POINTER = b"version https://git-lfs.github.com/spec/"
CANDIDATES = (
    Path(__file__).resolve().parents[3] / "test-fixtures",  # a checkout: beside backend/
    Path("/app/test-fixtures"),  # the test image, as CI mounts it
)


@cache
def _manifest_root() -> Path | None:
    named = os.environ.get("SCADBUDDY_TEST_FIXTURES")
    for root in (Path(named),) if named else CANDIDATES:
        if (root / "makerworld" / "manifest.json").is_file():
            return root
    return None


@cache
def fixtures_root() -> Path | None:
    """Where the fixtures are, or None when only their LFS pointers are."""
    root = _manifest_root()
    if root is None:
        return None
    for entry in makerworld():
        with (root / "makerworld" / entry["file"]).open("rb") as handle:
            if handle.read(len(LFS_POINTER)).startswith(LFS_POINTER):
                return None
    return root


def makerworld() -> list[dict[str, Any]]:
    """The MakerWorld fixtures' manifest entries. The manifest is plain JSON, not in
    LFS, so tests parametrized by it collect the same with or without the files."""
    root = _manifest_root()
    if root is None:
        return []
    entries: list[dict[str, Any]] = json.loads(
        (root / "makerworld" / "manifest.json").read_text(encoding="utf-8")
    )
    return entries


def makerworld_path(name: str) -> Path:
    root = fixtures_root()
    assert root is not None
    return root / "makerworld" / name
