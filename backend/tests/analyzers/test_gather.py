"""The analysis's inputs as gather reads them, without the app; the output's 3MF from
the blob store with a database."""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from scadbuddy.analyzers.context import AnalysisContext, AnalysisRequest
from scadbuddy.analyzers.gather import _read_choices, gather_context
from scadbuddy.analyzers.runner import run_checks
from scadbuddy.bambuddy.models import NozzleChoice
from scadbuddy.library.outputs import MODEL_NAME
from scadbuddy.library.settings_store import ModelPrintChoices, StoredSettings
from tests.support.output_blobs import blob_output


@pytest.mark.requires_postgres
async def test_gather_model_3mf_temp_file_lives_through_analysis(
    tmp_path: Path, pg_conninfo: str
) -> None:
    """#7256: with the 3MF only in the blob store, gather hands the analyzers its
    temporary file, which lives until the caller's stack closes."""
    async with blob_output(tmp_path, pg_conninfo) as output:
        (output.directory / MODEL_NAME).unlink()
        async with AsyncExitStack() as stack:
            context = await gather_context(
                outputs=output.store,
                settings=StoredSettings(),
                slug=output.meta.slug,
                params={},
                request=AnalysisRequest(),
                meta=output.meta,
                stack=stack,
            )
            path = context.model_3mf
            assert path is not None and path.is_file()
            assert path.parent != output.directory
            assert context.geometry is not None
            run_checks(context)
            assert path.is_file()
        assert not path.exists()


def test_a_remembered_pair_of_two_flows_is_analysed_as_one() -> None:
    """#4398 review: the analysis falls back to the remembered choices, as the dialog
    reopens on them, and so with one flow (#4272), not the pair stored before it."""
    context = AnalysisContext(slug="m", params={}, request=AnalysisRequest())
    mixed = [NozzleChoice(size="0.4", flow="high_flow"), NozzleChoice(size="0.4")]
    _read_choices(context, StoredSettings(), ModelPrintChoices(nozzles=mixed), None)
    assert context.choices is not None
    assert [nozzle.flow for nozzle in context.choices.nozzles] == ["high_flow", "high_flow"]
