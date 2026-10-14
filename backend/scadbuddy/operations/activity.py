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


def row_of(op: Operation, workflow_id: str | None = None, run_id: str | None = None) -> ActivityRow:
    return ActivityRow(
        id=f"operation:{op.id}",
        kind=operation_kind_key(op.kind),
        subject=op.subject,
        status=_STATUS[op.status],
        title=f"{op.kind.replace('_', ' ').capitalize()} {op.subject}",
        source="operations",
        source_id=op.id,
        workflow_id=workflow_id,
        run_id=run_id,
    )


ACTIVITY_KINDS: tuple[()] = ()
