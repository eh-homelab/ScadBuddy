"""A kind of operation (#1053): its check, its effect and how often the effect may run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

#: Which worker runs a kind (§4.3): the one that holds what its effect needs.
Queue = Literal["bambuddy", "library"]

#: The route's refusals: raises ``ApiError`` to refuse, writes nothing, and returns what
#: ``run`` needs (JSON).
CheckFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
#: The effect: the request and what the check returned; returns the route's answer body.
RunFn = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class OperationKind:
    name: str
    check: CheckFn
    run: RunFn
    #: 1 unless Bambuddy dedupes the effect (§4.2: a repeat never repeats the effect).
    run_attempts: int = 1
    queue: Queue = "bambuddy"
    #: How long one run may take; the workflow's ``RUN_TIMEOUT`` when None.
    run_timeout: timedelta | None = None


def operation_key(kind: str, subject: str, request: dict[str, Any], request_id: str) -> str:
    """The kind, its subject, the canonical body and the client's key (§4.2 step 1)."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{kind}\n{subject}\n{canonical}\n{request_id}".encode()).hexdigest()
