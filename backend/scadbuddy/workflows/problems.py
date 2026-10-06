"""The problem a command's activity reported, as its record keeps it (#1052, #1053)."""

from __future__ import annotations

from typing import Any

from temporalio.exceptions import ActivityError, ApplicationError

from scadbuddy.bambuddy.runs import UNEXPECTED_DETAIL, PrintRunError
from scadbuddy.workflows.print_models import FAILED, REFUSED

#: An operation's unexpected failure (#1053): it names no kind, unlike a print run's.
OPERATION_UNEXPECTED_DETAIL = "ScadBuddy failed unexpectedly while doing this; see its logs."
#: An unexpected failure of an operation's effect (review #1063 second review 1): a crash
#: or timeout may come after Bambuddy took the write, so it may have been done.
OPERATION_UNEXPECTED_RUNNING_DETAIL = (
    "ScadBuddy failed unexpectedly while doing this, so it may have been done. "
    "Check Bambuddy before trying again; ScadBuddy's logs say what failed."
)
#: What an operation cancelled before its record answers: nothing was written or done.
OPERATION_CANCELLED = PrintRunError(
    status=409,
    title="Conflict",
    detail="This was cancelled before it started. Nothing was done; try again.",
)
#: An operation cancelled while its effect ran (review #1063 2): the effect may have
#: reached Bambuddy before the cancel did.
OPERATION_CANCELLED_RUNNING = PrintRunError(
    status=409,
    title="Conflict",
    detail=(
        "This was cancelled while it was running, so it may have been done. "
        "Check Bambuddy before trying again."
    ),
)
#: An operation whose execution ended without recording an outcome (review #1063 1):
#: terminated in the Temporal UI, say. Its effect may or may not have happened.
OPERATION_LOST = PrintRunError(
    status=500,
    title="Internal Server Error",
    detail=(
        "ScadBuddy stopped running this before it recorded how it ended, so it may have "
        "been done. Check Bambuddy before trying again."
    ),
)


def problem_of(error: BaseException, *, unexpected: str = UNEXPECTED_DETAIL) -> PrintRunError:
    """The problem an activity reported (``REFUSED``/``FAILED`` details), else the
    unexpected failure's: never the exception's own text, which may say anything."""
    cause = error.cause if isinstance(error, ActivityError) else error
    if isinstance(cause, ApplicationError) and cause.type in (REFUSED, FAILED) and cause.details:
        detail: Any = cause.details[0]
        return detail if isinstance(detail, PrintRunError) else PrintRunError.model_validate(detail)
    return PrintRunError(status=500, title="Internal Server Error", detail=unexpected)
