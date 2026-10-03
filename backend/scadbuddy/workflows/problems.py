"""The problem a command's activity reported, as its record keeps it (#1052, #1053)."""

from __future__ import annotations

from typing import Any

from temporalio.exceptions import ActivityError, ApplicationError

from scadbuddy.bambuddy.runs import UNEXPECTED_DETAIL, PrintRunError
from scadbuddy.workflows.print_models import FAILED, REFUSED


def problem_of(error: BaseException) -> PrintRunError:
    """The problem an activity reported (``REFUSED``/``FAILED`` details), else the
    unexpected failure's: never the exception's own text, which may say anything."""
    cause = error.cause if isinstance(error, ActivityError) else error
    if isinstance(cause, ApplicationError) and cause.type in (REFUSED, FAILED) and cause.details:
        detail: Any = cause.details[0]
        return detail if isinstance(detail, PrintRunError) else PrintRunError.model_validate(detail)
    return PrintRunError(status=500, title="Internal Server Error", detail=UNEXPECTED_DETAIL)
