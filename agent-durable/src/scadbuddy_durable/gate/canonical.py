"""Canonical JSON and the hash of a response (#3717, #3724).

The same function as the agent's ``src/gate/canonical.ts``; the shared vectors
(``agent/test/fixtures/response-hash-vectors.json``) pin the two together. Keys sort by
UTF-16 code unit, as JavaScript's do; a response holds only strings, booleans, null,
arrays and objects.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# An unpaired surrogate: JSON.stringify writes it as a lowercase ``\uXXXX`` escape
# (well-formed JSON), where ``ensure_ascii=False`` leaves it raw and UTF-8 refuses it.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _dumps(value: Any) -> str:
    return _LONE_SURROGATE.sub(
        lambda m: f"\\u{ord(m.group()):04x}", json.dumps(value, ensure_ascii=False)
    )


def canonical_json(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(f"{_dumps(k)}:{canonical_json(value[k])}" for k in keys) + "}"
    return _dumps(value)


def response_hash(response: Any) -> str:
    """sha256 hex of the canonical JSON of a response."""
    return hashlib.sha256(canonical_json(response).encode("utf-8")).hexdigest()
