"""What crosses ``PrintRun``'s history keeps what the request left out (#1052): a
``project_id`` the request omitted means the remembered project, and an explicit
``null`` means none (`chosen_project`), so the difference must survive the converter."""

from __future__ import annotations

from temporalio.contrib.pydantic import pydantic_data_converter

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices
from scadbuddy.workflows.print_models import PrintRunInput, SourceSpec


def round_trip(request: PrintRunRequest) -> PrintRunRequest:
    arg = PrintRunInput(
        subject="o" * 32,
        slug="demo",
        key="k",
        source=SourceSpec(kind="output", output_id="o" * 32),
        request=request,
    )
    converter = pydantic_data_converter.payload_converter
    [payload] = converter.to_payloads([arg])
    [back] = converter.from_payloads([payload], [PrintRunInput])
    assert isinstance(back, PrintRunInput)
    return back.request


def request(**fields: object) -> PrintRunRequest:
    return PrintRunRequest.model_validate(
        {
            "filament_plan": FilamentPlan(slots=[]).model_dump(),
            "choices": PrintChoices(nozzles=[NozzleChoice(size="0.4")]).model_dump(),
            **fields,
        }
    )


def test_an_omitted_project_stays_omitted() -> None:
    assert "project_id" not in round_trip(request()).model_fields_set


def test_an_explicit_no_project_stays_explicit() -> None:
    back = round_trip(request(project_id=None))
    assert "project_id" in back.model_fields_set and back.project_id is None
