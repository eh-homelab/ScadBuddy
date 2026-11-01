"""The path a run takes as the server reports it (#3818): the dialog reads it, never a guess."""

from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy.bambuddy.choices import ChoicesView, with_lan_printers
from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.filaments import FilamentOptions
from scadbuddy.bambuddy.print_run import (
    PrintRunResult,
    _queued,
    lan_block,
    lan_printers,
    lan_reach,
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


def test_the_reports_are_read_in_one_query() -> None:
    """#4516: on every choices read, the registry's reports are one query, not one per
    row in turn; an ``auto`` row is on the LAN while its report is fresh."""

    def auto(n: int) -> PrinterRow:
        row = _both_links_native()
        return row.model_copy(
            update={"id": f"00000000-0000-4000-8000-0000000000{n:02x}", "preferred_path": "auto"}
        )

    class Rows:
        def list(self) -> list[PrinterRow]:
            return [auto(1), auto(2), auto(3)]

    class Reports:
        reads = 0

        def all(self) -> dict[str, tuple[dict[str, Any], datetime]]:
            self.reads += 1
            now = datetime.now(UTC)
            return {auto(1).id: ({}, now), auto(3).id: ({}, now)}

        def get(self, printer_id: str) -> object:
            raise AssertionError("a report was read on its own")

    reports = Reports()
    listed = asyncio.run(lan_printers(StoredSettings(**HOT_PATH), Rows(), reports))  # type: ignore[arg-type]
    assert listed == [auto(1).id, auto(3).id]
    assert reports.reads == 1


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
    """#3823: on Bambuddy's backends no row is native, and the view says which backends
    keep each one off the LAN, so the dialog never says "not yet" of it."""
    view = ChoicesView(filaments=FilamentOptions(library_file_id=None))
    printers = SimpleNamespace(store=_Rows(), reports=None)
    seen = asyncio.run(with_lan_printers(view, StoredSettings(), printers))  # type: ignore[arg-type]
    assert seen.lan_printers == []
    assert seen.model_dump()["lan_blocked"] == [
        {
            "printer": _both_links_native().id,
            "reasons": ["slicer_backend", "library_backend", "store_backend"],
            "seen_at": None,
        }
    ]


def test_an_unreadable_registry_on_bambuddys_backends_leaves_the_view() -> None:
    """The reasons only add to a Bambuddy-path view: a registry that cannot be read
    leaves it as it was, never failing the dialog's read."""

    class Unreadable:
        def list(self) -> list[PrinterRow]:
            raise RuntimeError("the registry is down")

    view = ChoicesView(filaments=FilamentOptions(library_file_id=None))
    printers = SimpleNamespace(store=Unreadable(), reports=None)
    seen = asyncio.run(with_lan_printers(view, StoredSettings(), printers))  # type: ignore[arg-type]
    assert (seen.lan_printers, seen.lan_blocked) == ([], [])
    with pytest.raises(RuntimeError):
        asyncio.run(with_lan_printers(view, StoredSettings(**HOT_PATH), printers))  # type: ignore[arg-type]


def _lan_only(**update: Any) -> PrinterRow:
    return _both_links_native().model_copy(
        update={"bambuddy_printer_id": None, "preferred_path": "auto", **update}
    )


@pytest.mark.parametrize(
    ("backends", "row", "seen", "reasons"),
    [
        ({}, {}, "fresh", []),
        ({}, {"preferred_path": "native"}, None, []),
        ({"slicer_backend": "bambuddy"}, {}, "fresh", ["slicer_backend"]),
        ({"store_backend": "local"}, {}, "fresh", ["store_backend"]),
        ({}, {"preferred_path": "bambuddy"}, "fresh", ["preferred_path"]),
        ({}, {"lan_host": None}, None, ["no_lan_link"]),
        ({}, {"has_access_code": False}, None, ["no_access_code"]),
        ({}, {}, None, ["no_report"]),
        ({}, {}, "stale", ["stale_report"]),
        (
            {"slicer_backend": "bambuddy", "library_backend": "bambuddy"},
            {},
            None,
            ["slicer_backend", "library_backend", "no_report"],
        ),
    ],
)
def test_lan_block_names_every_reason_and_none_exactly_when_native(
    backends: dict[str, str], row: dict[str, Any], seen: str | None, reasons: list[str]
) -> None:
    now = datetime.now(UTC)
    seen_at = {None: None, "fresh": now, "stale": now - timedelta(hours=1)}[seen]
    settings = StoredSettings(**{**HOT_PATH, **backends})
    printer = _lan_only(**row)
    assert lan_block(settings, printer, seen_at, now=now) == reasons
    path = run_path(printer, seen_at, now=now)
    assert (reasons == []) is hot_path_native(settings, path)


def test_a_stale_reports_time_is_sent_with_it() -> None:
    seen_at = datetime.now(UTC) - timedelta(hours=1)
    row = _lan_only()

    class Rows:
        def list(self) -> list[PrinterRow]:
            return [row]

    class Reports:
        def all(self) -> dict[str, tuple[dict[str, Any], datetime]]:
            return {row.id: ({}, seen_at)}

    native, blocked = asyncio.run(lan_reach(StoredSettings(**HOT_PATH), Rows(), Reports()))  # type: ignore[arg-type]
    assert native == []
    assert [(b.printer, b.reasons, b.seen_at) for b in blocked] == [
        (row.id, ["stale_report"], seen_at)
    ]
