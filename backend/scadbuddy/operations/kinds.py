"""A kind of operation (#1053): its check, its effect and how often the effect may run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scadbuddy.core.components import Components, Core

#: The route's refusals: raises ``ApiError`` to refuse, writes nothing, and returns what
#: ``run`` needs (JSON).
CheckFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
#: The effect: the request and what the check returned; returns the route's answer body.
RunFn = Callable[[dict[str, Any], dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


@dataclass(frozen=True)
class OperationKind:
    name: str
    check: CheckFn
    run: RunFn
    #: 1 unless Bambuddy dedupes the effect (§4.2: a repeat never repeats the effect).
    run_attempts: int = 1


#: Set by the check activity; true while its check waits on Bambuddy, and left true when
#: that wait is cut short, so a check out of time blames the right service.
CHECK_ON_BAMBUDDY: ContextVar[list[bool] | None] = ContextVar("check_on_bambuddy", default=None)


@asynccontextmanager
async def waiting_on_bambuddy() -> AsyncIterator[None]:
    """Wraps a check's Bambuddy calls: only a timeout inside one is Bambuddy's."""
    waiting = CHECK_ON_BAMBUDDY.get()
    if waiting is not None:
        waiting[0] = True
    yield
    if waiting is not None:
        waiting[0] = False


#: A feature's kinds, built over the core and the components (`operations/component.py`).
KindsBuild = Callable[["Core", "Components"], Iterable[OperationKind]]
#: Where a feature exports its ``KindsBuild``: ``scadbuddy/<feature>/operations.py``.
KINDS_MODULE = "operations"
KINDS_ATTR = "OPERATION_KINDS"


class DuplicateKindError(ValueError):
    pass


def build_kinds(
    core: Core, components: Components, builds: Iterable[KindsBuild]
) -> dict[str, OperationKind]:
    """Every feature's kinds by name; a name claimed twice is refused, not shadowed."""
    kinds: dict[str, OperationKind] = {}
    for build in builds:
        for kind in build(core, components):
            if kind.name in kinds:
                raise DuplicateKindError(f"two operation kinds are named {kind.name!r}")
            kinds[kind.name] = kind
    return kinds


def operation_key(kind: str, subject: str, request: dict[str, Any], request_id: str) -> str:
    """The kind, its subject, the canonical body and the client's key (§4.2 step 1)."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{kind}\n{subject}\n{canonical}\n{request_id}".encode()).hexdigest()
