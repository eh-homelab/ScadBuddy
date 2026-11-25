"""The `templates` rows for tests (#7317 phase 3): the catalogue's lists read only rows.

A test's app copies the templates to rows at its start (`main._first_template_copy`).
A test that writes template files with no catalogue write afterwards (a hand edit) calls
`sync_template_rows`, which runs the copy that repairs such an edit in production. A test
that makes its own `Catalogue` gives it rows with `open_template_rows`."""

from __future__ import annotations

from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.template_copy import TemplateCopyResult, copy_templates
from scadbuddy.library.template_refresh import TemplateRefresh
from scadbuddy.library.template_rows import TemplateRows
from tests.conftest import PgPool


def _finished(result: TemplateCopyResult | None) -> TemplateCopyResult:
    assert result is not None, "another run holds the template copy's lock"
    assert not result.incomplete, f"the template copy did not see every folder: {result}"
    return result


def sync_template_rows(client: TestClient) -> TemplateCopyResult:
    """One copy of the templates' files to rows on ``client``'s app."""
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    refresh = state.catalogue.refresh
    assert refresh is not None
    return _finished(copy_templates(state.catalogue, refresh.rows, refresh, state.projection.pool))


def open_template_rows(catalogue: Catalogue, pool: PgPool) -> TemplateRows:
    """``catalogue`` with rows on ``pool``, as the app wires them, after one copy."""
    catalogue.refresh = TemplateRefresh(catalogue, TemplateRows(pool))
    catalogue.rows = catalogue.refresh.rows
    if catalogue.history is not None:
        catalogue.history.template_rows = catalogue.refresh
    _finished(copy_templates(catalogue, catalogue.rows, catalogue.refresh, pool))
    return catalogue.rows
