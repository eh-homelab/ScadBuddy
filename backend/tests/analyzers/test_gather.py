"""The analysis's inputs as gather reads them, without the app or a database."""

from __future__ import annotations

from scadbuddy.analyzers.context import AnalysisContext, AnalysisRequest
from scadbuddy.analyzers.gather import _read_choices
from scadbuddy.bambuddy.models import NozzleChoice
from scadbuddy.library.settings_store import ModelPrintChoices, StoredSettings


def test_a_remembered_pair_of_two_flows_is_analysed_as_one() -> None:
    """#4398 review: the analysis falls back to the remembered choices, as the dialog
    reopens on them, and so with one flow (#4272), not the pair stored before it."""
    context = AnalysisContext(slug="m", params={}, request=AnalysisRequest())
    mixed = [NozzleChoice(size="0.4", flow="high_flow"), NozzleChoice(size="0.4")]
    _read_choices(context, StoredSettings(), ModelPrintChoices(nozzles=mixed), None)
    assert context.choices is not None
    assert [nozzle.flow for nozzle in context.choices.nozzles] == ["high_flow", "high_flow"]
