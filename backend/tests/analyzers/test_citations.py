"""The citation lint (#284 "Citations are required"; AI spec D10).

Every bundled analyzer cites at least one source with an https URL and a quoted line,
and every value a fix proposes is covered by a source that says it supports that
setting.
"""

from __future__ import annotations

import pytest

from scadbuddy.analyzers import sources
from scadbuddy.analyzers.builtin import BUILTIN
from scadbuddy.analyzers.model import Analyzer, Source
from scadbuddy.analyzers.runner import run_checks
from tests.analyzers.conftest import context, geometry_of, silk_slot, tee

ALL_SOURCES = [value for value in vars(sources).values() if isinstance(value, Source)]


@pytest.mark.parametrize("analyzer", BUILTIN, ids=lambda analyzer: analyzer.id)
def test_every_analyzer_cites_a_quoted_source(analyzer: Analyzer) -> None:
    assert analyzer.sources
    for source in analyzer.sources:
        assert source.url.startswith("https://")
        assert source.quote.strip()
        assert analyzer.id in source.supports


@pytest.mark.parametrize("source", ALL_SOURCES, ids=lambda source: source.quote[:30])
def test_every_source_is_pinned_and_says_what_it_supports(source: Source) -> None:
    assert source.supports
    if "github.com/bambulab/BambuStudio" in source.url:
        assert sources.BAMBU_STUDIO_COMMIT in source.url
    if "github.com/eh-homelab/ScadBuddy/blob" in source.url:
        assert sources.SCADBUDDY_COMMIT in source.url


def test_every_proposed_value_is_covered_by_its_sources() -> None:
    ctx = context(geometry=geometry_of(tee()), filaments=[silk_slot()])
    diagnostics, _ = run_checks(ctx)
    changes = [item for row in diagnostics for fix in row.fixes for item in fix.changes]
    assert changes, "the fixtures should raise at least one fix"
    for item in changes:
        assert any(item.setting in source.supports for source in item.sources), item.setting
