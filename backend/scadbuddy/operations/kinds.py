"""A kind of operation (#1053): its check, its effect and how often the effect may run."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from scadbuddy.core.components import Components, Core

#: Which worker runs a kind (§4.3): the one that holds what its effect needs.
Queue = Literal["bambuddy", "library"]
#: What a user checks when a kind's effect may have happened unrecorded, by queue.
WHERE: dict[Queue, str] = {"bambuddy": "Bambuddy", "library": "the model and its libraries"}

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
    #: What to check before repeating an effect that may have happened ("Check
    #: Bambuddy ..."); its queue's ``WHERE`` when None.
    where: str | None = None

    def __post_init__(self) -> None:
        if self.where is None:
            object.__setattr__(self, "where", WHERE[self.queue])


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


class ThreadSteps:
    """How many of a run's ``to_thread_to_end`` steps are in their threads now."""

    def __init__(self) -> None:
        self.running = 0


#: The run's steps, set by the run's activity (`workflows/operation_activities.py`);
#: None outside one.
THREAD_STEPS: ContextVar[ThreadSteps | None] = ContextVar("thread_steps", default=None)


async def to_thread_to_end[T](fn: Callable[[], T]) -> T:
    """``asyncio.to_thread``, except that a cancel raises only once the thread has
    returned: a thread cannot be stopped, so a lock held around this stays held for as
    long as the thread runs. Its effect (a commit, say) still lands, so it is counted
    in the run's ``THREAD_STEPS`` while it runs."""
    future = asyncio.ensure_future(asyncio.to_thread(fn))
    steps = THREAD_STEPS.get()
    if steps is not None:
        steps.running += 1

        def ended(_: object) -> None:
            steps.running -= 1

        future.add_done_callback(ended)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({future})
        raise
