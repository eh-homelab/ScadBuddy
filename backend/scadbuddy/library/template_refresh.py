"""Each catalogue write refreshes the ``templates`` rows of what it touched (#7317, model
records spec §5.2).

`TemplateRefresh` is the history's `TemplateRowsHook` and the catalogue's own refresh.
It reads each template's row from its files (`Catalogue.template_row`) and writes it:
in the transaction that records a new HEAD (`for_commit`), or in a transaction of its
own (`refresh_locked`) when no commit landed, when there is no coordinator, or when
there is no git. A failed refresh never fails the write (spec §7.3): it is logged with
the template ids and counted, and the next copy run repairs the row.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any

from scadbuddy.core.paths import model_path
from scadbuddy.core.tracing import span
from scadbuddy.library.history import RefreshPath
from scadbuddy.library.template_rows import TemplateRow, TemplateRows, write_rows

if TYPE_CHECKING:
    from scadbuddy.library.catalogue import Catalogue

logger = logging.getLogger(__name__)


class TemplateRefresh:
    """A `TemplateRowsHook` over the catalogue's files and the ``templates`` table.

    ``on_failure`` hears the path of each failed refresh (``commit``, ``no_commit`` or
    ``no_git``); the app counts it in ``template_refresh_failures``."""

    def __init__(
        self,
        catalogue: Catalogue,
        rows: TemplateRows,
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        self.catalogue = catalogue
        self.rows = rows
        self.on_failure = on_failure

    def read(
        self,
        ids: Sequence[str],
        versions: Mapping[str, str | None],
        head: str | None,
        *,
        path: str = "copy",
    ) -> tuple[list[TemplateRow], list[str]]:
        """The rows of ``ids`` that have a ``model.scad``, and the ids that are gone,
        in a span ``templates.refresh``. Raises what the reads raise."""
        rows: list[TemplateRow] = []
        gone: list[str] = []
        with span(
            "templates.refresh",
            attributes={"scadbuddy.templates": len(ids), "scadbuddy.path": path},
        ):
            for template_id in ids:
                row = self.catalogue.template_row(
                    template_id, version=versions.get(template_id), head=head
                )
                if row is None:
                    gone.append(template_id)
                else:
                    rows.append(row)
        return rows, gone

    def for_commit(self, models: list[str], commit: str) -> Callable[[Any], None] | None:
        """The rows of ``models`` as ``commit`` left them, read before the HEAD
        transaction opens, and what writes them in it. Never raises: the commit has
        landed, so a failed read answers None and the HEAD is still recorded."""
        try:
            rows, gone = self.read(models, dict.fromkeys(models, commit), commit, path="commit")
        except Exception:
            self._failed(models, "commit")
            return None
        write = partial(write_rows, rows=rows, gone=gone)

        def run(conn: Any) -> None:
            # A failed write rolls the HEAD transaction back (`_PgHold.record`), and
            # `_landed` logs that; it is counted here as a failed refresh too.
            try:
                write(conn)
            except Exception:
                self._failed(models, "commit")
                raise

        return run

    def refresh_locked(self, models: list[str], *, path: RefreshPath) -> None:
        """Read and write the rows of ``models`` in a transaction of their own. The
        caller holds the write lock (or, without git, there is no other writer). Each
        version is the template's last commit, None without git. Never raises."""
        try:
            history = self.catalogue.history
            versions: dict[str, str | None] = {}
            head: str | None = None
            if history is not None and history.available:
                versions = {
                    template_id: history.last_commit(model_path(template_id))
                    for template_id in models
                }
                head = history.head()
            rows, gone = self.read(models, versions, head, path=path)
            self.rows.put(rows, gone)
        except Exception:
            self._failed(models, path)

    def _failed(self, models: Sequence[str], path: str) -> None:
        logger.exception(
            "could not refresh the templates' rows",
            extra={"templates": list(models), "refresh_path": path},
        )
        if self.on_failure is None:
            return
        try:
            self.on_failure(path)
        except Exception:
            logger.exception("a template refresh failure listener failed")
