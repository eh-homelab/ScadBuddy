"""The path a run takes as the server reports it (#3818): the dialog reads it, never a guess."""

from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import UTC, datetime
from typing import Any

import pytest

from scadbuddy.bambuddy.choices import ChoicesView, with_lan_printers
from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.filaments import FilamentOptions
from scadbuddy.bambuddy.print_run import (
    PrintRunResult,
    _queued,
    lan_printers,
    run_path,
)
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.hot_path import hot_path_native
from scadbuddy.printers.models import PrinterRow
from scadbuddy.workflows.print_activities import PrintDeps


def test_a_queued_run_reports_the_bambuddy_path() -> None:
    outcome = QueueOutcome(sliced_library_file_id=2, queue_item_ids=[7], printer_id=1)
    result = _queued(None, [outcome], 1, None, None, 1, [])
    assert result.path == "bambuddy"
    assert result.model_dump()["path"] == "bambuddy"


def test_a_result_recorded_before_the_path_reads_as_bambuddys() -> None:
    # A stored run's result, from before #3818 carried the path.
    stored = {"library_file_id": 1, "copies": 1, "queue_item_ids": [7]}
    assert PrintRunResult.model_validate(stored).path == "bambuddy"


# #3818 — the path the dialog reads before a run: the server's, never `run_path` copied.


def _both_links_native() -> PrinterRow:
    now = datetime.now(UTC)
    return PrinterRow(
        id="00000000-0000-4000-8000-0000000000c2",
        serial="31B8BP000000C2",
        name="H2C",
        bambuddy_printer_id=1,
        lan_host="192.168.1.62",
        has_access_code=True,
        preferred_path="native",
        created_at=now,
        updated_at=now,
    )


class _Rows:
    """A registry holding one row set to the native path, linked both ways."""

    def __init__(self) -> None:
        self.reads = 0

    def list(self) -> list[PrinterRow]:
        self.reads += 1
        return [_both_links_native()]


class _Unread:
    """A registry component no read may reach while no run is native."""

    @property
    def store(self) -> object:
        raise AssertionError("the registry was read")

    @property
    def reports(self) -> object:
        raise AssertionError("the registry was read")


#: The hot path's stored backends (plan 2026-10-18 D2).
HOT_PATH: dict[str, Any] = {
    "slicer_backend": "native",
    "library_backend": "native",
    "store_backend": "s3",
}


def test_a_native_row_reports_bambuddys_path_on_bambuddys_backends() -> None:
    """D2: the stored backends are the control. On the defaults (prod's
    ``slicer_backend=bambuddy``) a row whose own path is the LAN prints through
    Bambuddy, and the registry is not read for the dialog."""
    settings = StoredSettings()
    row = _both_links_native()
    assert run_path(row, None, now=datetime.now(UTC)) == "native"
    assert hot_path_native(settings, "native") is False
    rows = _Rows()
    assert asyncio.run(lan_printers(settings, rows, None)) == []  # type: ignore[arg-type]
    assert rows.reads == 0


def test_a_native_row_is_listed_on_the_hot_paths_backends() -> None:
    settings = StoredSettings(**HOT_PATH)
    rows = _Rows()
    listed = asyncio.run(lan_printers(settings, rows, None))  # type: ignore[arg-type]
    assert listed == [_both_links_native().id]


@pytest.mark.parametrize(
    ("change", "path", "native"),
    [
        ({}, "native", True),
        ({"store_backend": "bambuddy"}, "native", True),
        ({}, "bambuddy", False),
        ({"store_backend": "local"}, "native", False),
        ({"slicer_backend": "bambuddy"}, "native", False),
        ({"library_backend": "bambuddy"}, "native", False),
    ],
)
def test_hot_path_native_is_the_backends_and_the_rows_path(
    change: dict[str, str], path: Any, native: bool
) -> None:
    assert hot_path_native(StoredSettings(**{**HOT_PATH, **change}), path) is native


def test_the_print_worker_decides_a_run_as_the_dialog_reads_it() -> None:
    default = next(f for f in fields(PrintDeps) if f.name == "native_run").default
    assert default is hot_path_native


def test_the_choices_view_lists_no_lan_printer_while_no_run_is_native() -> None:
    view = ChoicesView(filaments=FilamentOptions(library_file_id=None))
    seen = asyncio.run(with_lan_printers(view, StoredSettings(), _Unread()))  # type: ignore[arg-type]
    assert seen.lan_printers == []
    assert seen.model_dump()["lan_printers"] == []
