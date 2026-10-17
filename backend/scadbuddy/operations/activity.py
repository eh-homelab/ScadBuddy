"""Operations as activity: one kind per ``OperationKind`` (``activity/kinds.py``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scadbuddy.activity.kinds import operation_kind_key
from scadbuddy.activity.models import ActivityRow, ActivityStatus

if TYPE_CHECKING:
    from scadbuddy.operations.store import Operation

_STATUS: dict[str, ActivityStatus] = {
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
}


#: A Bambuddy subject's prefix; the kind before it in a title already names the thing.
_ID_PREFIXES = ("library:", "library-folder:", "project:")


def _subject_text(subject: str) -> str:
    """How a subject reads in a title (#3856): ``Create project “Shelf”`` rather than
    ``Create project project-name:Shelf``; the filter chip shows the reference."""
    if subject.startswith("project-name:"):
        name = subject.removeprefix("project-name:")
        return f"“{name[:40]}…”" if len(name) > 40 else f"“{name}”"
    if subject == "library-files":
        return "(several files)"
    for prefix in _ID_PREFIXES:
        if subject.startswith(prefix):
            rest = subject.removeprefix(prefix)
            return f"#{rest}" if rest.isdigit() else rest  # an OpenSCAD library by name
    return subject


def row_of(op: Operation, workflow_id: str | None = None, run_id: str | None = None) -> ActivityRow:
    return ActivityRow(
        id=f"operation:{op.id}",
        kind=operation_kind_key(op.kind),
        subject=op.subject,
        status=_STATUS[op.status],
        title=f"{op.kind.replace('_', ' ').capitalize()} {_subject_text(op.subject)}",
        source="operations",
        source_id=op.id,
        workflow_id=workflow_id,
        run_id=run_id,
    )


ACTIVITY_KINDS: tuple[()] = ()
