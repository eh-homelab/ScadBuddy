"""The input hash a flow run's approval binds to (#2318): the agent's `inputHash`
(agent/src/approvals/service.ts) in Python, so the agent can check that a `tool(...)`
call it runs is the one a person approved, not only one with the approved call id.

An HMAC-SHA256 under a key derived from the KEK (`approvalHashKey`) over the canonical
JSON of `{"tool", "input"}`: the agent's `canonicalJson`, which is `JSON.stringify`
with object keys sorted at every depth. Python's JSON differs from JavaScript's in its
numbers (`1.0`, `1e-07`) and its key order (code points, not UTF-16 code units), so
both are written out here. agent/test/fixtures/approval-hash-vectors.json pins the two
together (tests/test_approval_hash.py, agent/test/approvalHashVectors.test.ts).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from decimal import Decimal
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from scadbuddy.core.secrets import Kek

#: approvals/service.ts `approvalHashKey`'s HKDF label.
HASH_KEY_INFO = b"scadbuddy:ai_approvals:input_hash:v1"
#: The largest integer a JavaScript number holds exactly.
_SAFE_INTEGER = 2**53
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def approval_hash_key(kek: Kek) -> bytes:
    """`approvalHashKey`: HKDF-SHA256 of the KEK with no salt, 32 bytes."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HASH_KEY_INFO).derive(kek.key)


def input_hash(key: bytes, tool: str, input: dict[str, Any]) -> str:
    """`inputHash`: hex HMAC-SHA256 of the canonical `{"tool", "input"}`."""
    text = canonical_json({"tool": tool, "input": input})
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


def canonical_json(value: Any) -> str:
    """`canonicalJson`: what `JSON.stringify` writes for the value JavaScript parses
    from the same JSON, with object keys sorted by UTF-16 code unit."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        # Past 2**53 JavaScript holds the nearest double, as Python's float does.
        return str(value) if abs(value) <= _SAFE_INTEGER else _number(float(value))
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list | tuple):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: str(k).encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(f"{_string(str(k))}:{canonical_json(value[k])}" for k in keys) + "}"
    raise TypeError(f"{type(value).__name__} is not JSON")


def _string(text: str) -> str:
    # JSON.stringify escapes the same characters as Python's ensure_ascii=False, and a
    # lone surrogate as \uXXXX, which Python leaves raw.
    quoted = json.dumps(text, ensure_ascii=False)
    return _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", quoted)


def _number(x: float) -> str:
    """ECMAScript Number::toString (radix 10) of `x`; `null` for NaN and the infinities,
    as JSON.stringify writes them."""
    if not math.isfinite(x):
        return "null"
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    # repr is the shortest string that round-trips, as ECMAScript's digits are.
    parts = Decimal(repr(abs(x))).normalize().as_tuple()
    digits = "".join(str(d) for d in parts.digits)
    k = len(digits)
    assert isinstance(parts.exponent, int)
    n = parts.exponent + k
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * -n + digits
    e = n - 1
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    return f"{sign}{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"
