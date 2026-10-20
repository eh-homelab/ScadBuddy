"""Print runs as activity (spec 2026-10-09 §3): ``print_runs`` states mapped onto the six."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scadbuddy.activity.models import ActivityKind, ActivityRow, ActivityStatus, Facet

if TYPE_CHECKING:
    from scadbuddy.bambuddy.runs import PrintRun

#: `bambuddy/runs.py` `RunStatus`.
_STATUS: dict[str, ActivityStatus] = {
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
}


def row_of(
    run: PrintRun,
    slug: str = "",
    workflow_id: str | None = None,
    workflow_run_id: str | None = None,
) -> ActivityRow:
    return ActivityRow(
        id=f"print_run:{run.id}",
        kind="print_run",
        subject=run.subject,
        status=_STATUS[run.status],
        title=f"Print {slug or run.subject}",
        source="print_runs",
        source_id=run.id,
        facets={
            "subject": run.subject,
            "may_have_queued": run.may_have_queued,
            # The registry printer, by identity (#3823): never its path or Bambuddy id.
            **({"printer": run.printer} if run.printer else {}),
        },
        workflow_id=workflow_id,
        run_id=workflow_run_id,
    )


PRINT_RUN = ActivityKind(
    key="print_run",
    label="Print",
    facets=(
        Facet("subject", "keyword", "Subject"),
        Facet("may_have_queued", "bool", "May have queued", filterable=False),
        # A registry uuid, so the filter offers the registry's printers by name (#4442 review).
        Facet("printer", "keyword", "Printer", options="printers"),
    ),
)
ACTIVITY_KINDS = (PRINT_RUN,)
