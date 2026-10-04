"""The problem a command's activity reported, as its record keeps it (#1052, #1053)."""

from __future__ import annotations

import asyncio
from typing import Any

from temporalio.exceptions import ActivityError, ApplicationError, CancelledError

from scadbuddy.bambuddy.runs import UNEXPECTED_DETAIL, PrintRunError
from scadbuddy.workflows.print_models import FAILED, REFUSED

#: An operation's unexpected failure (#1053): it names no kind, unlike a print run's.
OPERATION_UNEXPECTED_DETAIL = "ScadBuddy failed unexpectedly while doing this; see its logs."
#: What an operation cancelled before its record answers: nothing was written or done.
OPERATION_CANCELLED = PrintRunError(
    status=409,
    title="Conflict",
    detail="This was cancelled before it started. Nothing was done; try again.",
)


def cancelled(error: BaseException) -> bool:
    """Whether ``error`` is the execution's own cancel, as the awaited activity or the
    workflow task itself raises it (review #1061 1c)."""
    if isinstance(error, ActivityError):
        return isinstance(error.cause, CancelledError)
    return isinstance(error, asyncio.CancelledError)


def problem_of(error: BaseException, *, unexpected: str = UNEXPECTED_DETAIL) -> PrintRunError:
    """The problem an activity reported (``REFUSED``/``FAILED`` details), else the
    unexpected failure's: never the exception's own text, which may say anything."""
    cause = error.cause if isinstance(error, ActivityError) else error
    if isinstance(cause, ApplicationError) and cause.type in (REFUSED, FAILED) and cause.details:
        detail: Any = cause.details[0]
        return detail if isinstance(detail, PrintRunError) else PrintRunError.model_validate(detail)
    return PrintRunError(status=500, title="Internal Server Error", detail=unexpected)
