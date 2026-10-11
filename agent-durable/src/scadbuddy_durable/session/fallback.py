"""Whether a failed segment was its credential's fault, so the next credential is tried.

A port of ``agent/src/harness/credentialErrors.ts`` ``classifyFailure`` for what a
segment's failure carries: Claude Code's ``ResultError`` (its ``api_error_status`` and
the "API Error: …" result text). A classic turn moves to the next credential on a
``permanent`` or ``rate_limited`` failure (``harness/fallback.ts``); so does a durable
segment (``ScadBuddyRunner.run``). A ``transient`` failure is retried by the plugin's
segment retry policy, and an ``other`` one would fail the same way on any key.
"""

from __future__ import annotations

import re
from typing import Literal

from claude_agent_sdk import ResultError

FailureClass = Literal["permanent", "rate_limited", "transient", "other"]

# credentialErrors.ts BILLING_MESSAGE: a 400 about the account, not the request.
BILLING_MESSAGE = re.compile(
    r"credit balance|billing|payment required|insufficient (?:funds|credit|quota)|quota exceeded",
    re.IGNORECASE,
)


def classify(err: BaseException) -> FailureClass:
    """classifyFailure's status rules, for a segment that raised."""
    if not isinstance(err, ResultError):
        return "other"
    status = err.api_error_status
    message = err.result or str(err)
    if status in (401, 402, 403):
        return "permanent"
    if status == 429:
        return "rate_limited"
    if status in (408, 409) or (status is not None and status >= 500):
        return "transient"
    if status == 400 and BILLING_MESSAGE.search(message):
        return "permanent"
    return "other"


def credentials_fault(err: BaseException) -> bool:
    """Whether another credential might succeed where this one failed."""
    return classify(err) in ("permanent", "rate_limited")
