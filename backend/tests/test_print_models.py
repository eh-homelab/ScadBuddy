"""What crosses ``PrintRun``'s history keeps what the request left out (#1052): a
``project_id`` the request omitted means the remembered project, and an explicit
``null`` means none (`chosen_project`), so the difference must survive the converter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from temporalio.contrib.pydantic import pydantic_data_converter

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.print_run import PreparedPlates, PrintRunRequest, run_path
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices
from scadbuddy.bambuddy.runs import run_key
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.status import STALE_AFTER
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


# ── #3823 Task 8: the run names a registry printer and chooses its path ─────────────

PRINTER = "00000000-0000-4000-8000-0000000000aa"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def row(**fields: object) -> PrinterRow:
    return PrinterRow.model_validate(
        {
            "id": PRINTER,
            "serial": "01P00A0000000AA",
            "name": "Garage",
            "created_at": NOW,
            "updated_at": NOW,
            **fields,
        }
    )


def test_the_printer_crosses_the_history() -> None:
    assert round_trip(request(printer=PRINTER)).printer == PRINTER


def test_the_printer_is_part_of_the_key_only_without_a_bambuddy_id() -> None:
    """#4442 M1: beside ``printer_id``, which decides, ``printer`` adds nothing, so the
    dialog's body (which has sent it since Task 10) keys as it did before the deploy."""
    plain = run_key("o" * 32, request(printer_id=1))
    assert run_key("o" * 32, request(printer_id=1, printer=PRINTER)) == plain
    assert run_key("o" * 32, request(printer_id=2, printer=PRINTER)) != plain
    alone = run_key("o" * 32, request(printer=PRINTER))
    other = run_key("o" * 32, request(printer="00000000-0000-4000-8000-0000000000bb"))
    assert len({plain, alone, other}) == 3


def test_a_printer_that_is_not_a_registry_id_is_refused() -> None:
    with pytest.raises(ValidationError):
        request(printer="garage")


@pytest.mark.parametrize(
    ("fields", "seen", "path"),
    [
        ({"preferred_path": "bambuddy", "lan_host": "10.0.0.9"}, NOW, "bambuddy"),
        ({"preferred_path": "native"}, None, "native"),
        ({"preferred_path": "auto", "lan_host": "10.0.0.9"}, NOW - timedelta(seconds=5), "native"),
        ({"preferred_path": "auto", "lan_host": "10.0.0.9"}, NOW - STALE_AFTER, "bambuddy"),
        ({"preferred_path": "auto", "lan_host": "10.0.0.9"}, None, "bambuddy"),
        ({"preferred_path": "auto"}, NOW, "bambuddy"),
    ],
    ids=["bambuddy", "native", "auto-fresh", "auto-stale", "auto-unheard", "auto-no-lan"],
)
def test_the_path_follows_the_row_and_its_report(
    fields: dict[str, object], seen: datetime | None, path: str
) -> None:
    assert run_path(row(**fields), seen, now=NOW) == path


def test_a_run_planned_before_the_registry_decodes_as_bambuddys() -> None:
    old = {"plate_ids": [1], "printer_id": 1}
    prepared = PreparedPlates.model_validate(old)
    assert (prepared.printer, prepared.path) == ("", "bambuddy")
    converter = pydantic_data_converter.payload_converter
    [payload] = converter.to_payloads([prepared])
    [back] = converter.from_payloads([payload], [PreparedPlates])
    assert back == prepared
