"""The zip `store/archive.py` `pack_dir` makes of a directory that holds one file, as core
packs a package mirror (#8137, `tools/go/core/internal/blobstore` `PublishFile`):
`fixtures/pack-dir-vectors.json` is what Python packs for each input, and core's tests check
that core writes the same container around it. After a change to `pack_dir`, run this file
with ``UPDATE_PACK_DIR_VECTORS=1`` and commit the fixture."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.store.archive import pack_dir

FIXTURE = Path(__file__).parents[1] / "fixtures" / "pack-dir-vectors.json"
UPDATE = os.environ.get("UPDATE_PACK_DIR_VECTORS") == "1"

#: Each input: the file's name and its bytes.
INPUTS: list[tuple[str, str, bytes]] = [
    ("empty", "file", b""),
    ("one_byte", "file", b"a"),
    ("text", "file", b"hello world\n"),
    ("repeated", "file", b"abcabcabd" * 300),
    ("binary", "file", bytes(range(256)) * 40),
    ("other_name", "plugin.zip", b"PK\x03\x04 not really a zip"),
]


def _pack(tmp_path: Path, name: str, data: bytes) -> bytes:
    directory = tmp_path / "blob"
    directory.mkdir()
    (directory / name).write_bytes(data)
    return pack_dir(directory)


def _cases(tmp_path: Path) -> list[dict[str, Any]]:
    cases = []
    for case, name, data in INPUTS:
        root = tmp_path / case
        root.mkdir()
        cases.append(
            {
                "name": case,
                "file": name,
                "data": base64.b64encode(data).decode(),
                "zip": base64.b64encode(_pack(root, name, data)).decode(),
            }
        )
    return cases


def test_pack_dir_writes_the_vectors(tmp_path: Path) -> None:
    cases = _cases(tmp_path)
    if UPDATE:
        FIXTURE.write_text(json.dumps({"cases": cases}, indent=2) + "\n")
    stored = json.loads(FIXTURE.read_text())["cases"]
    if stored != cases:
        pytest.fail("pack_dir changed; run with UPDATE_PACK_DIR_VECTORS=1 and commit the fixture")
