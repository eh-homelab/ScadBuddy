"""Each automatic import as activity (#4699): one finished row per archive the watcher
took, with where its 3MF came from and how its model was matched."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scadbuddy.activity.models import ActivityKind, ActivityRow, Facet
from scadbuddy.bambuddy.subject import PrintSubject

if TYPE_CHECKING:
    from scadbuddy.print_watch.store import AutoImport

KIND = "print_auto_import"


def row_of(row: AutoImport) -> ActivityRow:
    name = row.print_name or row.filename or f"archive {row.archive_id}"
    if row.state == "dismissed":
        # A dismiss rewrites the row (`AutoImportStore.dismiss`).
        title = f"Dismissed print {name} from Unsorted imports"
    elif row.slug is not None:
        title = f"Imported print {name} onto {row.slug}"
    else:
        title = f"Imported print {name} into Unsorted imports"
    return ActivityRow(
        id=f"{KIND}:{row.archive_id}",
        kind=KIND,
        subject=PrintSubject.model(row.slug).key if row.slug is not None else "",
        status="succeeded",
        title=title,
        source="print_auto_imports",
        source_id=str(row.archive_id),
        facets={
            "state": row.state,
            "source": row.source or "none",
            "matched_by": row.matched_by or "none",
        },
        detail={
            "archive_id": row.archive_id,
            # Bambuddy's printer id; the `printer` facet is a registry printer only.
            "bambuddy_printer_id": row.printer_id,
            "filename": row.filename,
            "reason": row.reason,
            "suggested": row.suggested,
        },
    )


PRINT_AUTO_IMPORT = ActivityKind(
    key=KIND,
    label="Imported print",
    facets=(
        Facet("state", "keyword", "Where"),
        Facet("source", "keyword", "File from"),
        Facet("matched_by", "keyword", "Matched by"),
    ),
)
ACTIVITY_KINDS = (PRINT_AUTO_IMPORT,)
