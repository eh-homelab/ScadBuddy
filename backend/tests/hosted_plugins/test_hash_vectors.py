"""The content hash of a hosted plugin's version (#6932) is what the agent's
``hashTree`` (``agent/src/plugins/packages/hash.ts``) computes for the unpacked zip. The
vectors are written by ``agent/test/hostedPluginHashVectors.test.ts`` from the
TypeScript code; this copy must match them case for case."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.hosted_plugins.bundle import content_hash, file_list

VECTORS = Path(__file__).parent.parent / "fixtures" / "hosted-plugin-hash-vectors.json"
CASES: list[dict[str, Any]] = json.loads(VECTORS.read_text("utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_hash_is_the_typescript_one(case: dict[str, Any]) -> None:
    files = {f["path"]: (f["text"].encode("utf-8"), f["executable"]) for f in case["files"]}
    listed = file_list(files)
    assert listed == case["file_list"]
    # Sorted as JavaScript sorts, so the key order is the vector's too.
    assert list(listed) == list(case["file_list"])
    assert content_hash(listed) == case["hash"]
