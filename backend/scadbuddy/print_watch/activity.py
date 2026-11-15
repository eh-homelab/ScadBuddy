"""Each automatic import as activity (#4699): one finished row per archive the watcher
took, with where its 3MF came from and how its model was matched."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scadbuddy.activity.models import ActivityKind, ActivityRow, Facet
from scadbuddy.bambuddy.subject import PrintSubject

if TYPE_CHECKING:
    from scadbuddy.bambuddy.print_links import PrintImport
    from scadbuddy.print_watch.lan import LanImport
    from scadbuddy.print_watch.store import AutoImport

KIND = "print_auto_import"


def row_of(row: AutoImport, *, matched_by: str | None = None) -> ActivityRow:
    """``row``'s activity row; ``matched_by``, when given, names the match in place of the
    row's own (Import print's `import`, #6029)."""
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
            "matched_by": matched_by or row.matched_by or "none",
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


def lan_row_of(row: LanImport) -> ActivityRow:
    """A LAN-only printer's record (#4699 phase 4): the same kind, keyed by the record's
    id under ``lan:``, so it never meets an archive's row."""
    name = row.subtask_name
    if row.state == "dismissed":
        title = f"Dismissed print {name} from Unsorted imports"
    elif row.slug is not None:
        title = f"Imported print {name} onto {row.slug}"
    else:
        title = f"Imported print {name} into Unsorted imports"
    return ActivityRow(
        id=f"{KIND}:lan:{row.id}",
        kind=KIND,
        subject=PrintSubject.model(row.slug).key if row.slug is not None else "",
        status="succeeded",
        title=title,
        source="print_lan_imports",
        source_id=str(row.id),
        facets={
            "state": row.state,
            "source": row.source or "none",
            "matched_by": row.matched_by or "none",
        },
        detail={
            "lan_import_id": row.id,
            # The registry printer: a LAN-only printer has no Bambuddy id.
            "printer": row.printer_id,
            "filename": row.filename,
            "reason": row.reason,
            "suggested": row.suggested,
        },
    )


def imported_row(row: AutoImport, record: PrintImport) -> ActivityRow:
    """``row``'s activity row once Import print (#4698) linked its archive to a model
    (#6029): the print is on that model now, from the source the person chose. The
    ``print_auto_imports`` row stays as the watcher wrote it."""
    linked = row.model_copy(
        update={
            "state": "linked",
            "slug": record.slug,
            "source": record.source,
            "filename": record.filename,
            "matched_by": None,
            "reason": None,
            "suggested": [],
        }
    )
    return row_of(linked, matched_by="import")


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
