"""The LAN-link gate, the stored-report status and the command dependency of the printer
routes that talk to a printer over its LAN link (#7276, plan 2026-11-25 Task 6, R-7276-4,
P14, P27, P30). Nothing here reaches Bambuddy."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import Response, status
from pydantic import BaseModel
from temporalio.client import Client

from scadbuddy.bambuddy.models import PrinterStatus
from scadbuddy.core.problems import ApiError
from scadbuddy.printers.kprofiles import kprofile_table_view, kprofile_write_view
from scadbuddy.printers.lan_command_models import (
    LanCommandOutcome,
    LanCommandPending,
    LanCommandRun,
)
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.reports import PrinterReportStore, report_status
from scadbuddy.printers.store import PrinterStore, printer_uuid
from scadbuddy.workflows.lan_command import (
    NOT_LAN_LINKED,
    LanCommandAnswer,
    execute_lan_commands,
    lan_command_exists,
    lan_command_run,
)


def is_lan_linked(row: PrinterRow) -> bool:
    """A physical printer with its host, pinned certificate and access code (P14: the
    printer view's ``lan_linked`` is this)."""
    return row.lan_linked


async def lan_row(printers: PrinterStore | None, printer_id: str, *, feature: str) -> PrinterRow:
    """``printer_id``'s row when ScadBuddy can reach it over the LAN: 404 for none, 409
    ``not_lan_linked`` for a row that is not LAN-linked."""
    row = None
    if printers is not None and printer_uuid(printer_id) is not None:
        row = await asyncio.to_thread(printers.get, printer_id)
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no printer {printer_id}")
    if not is_lan_linked(row):
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{row.name} is not linked over the LAN, so ScadBuddy cannot reach its {feature}."
            " Link it in Settings > Printers.",
            code=NOT_LAN_LINKED,
        )
    return row


async def live_status(
    reports: PrinterReportStore, row: PrinterRow, *, now: datetime
) -> PrinterStatus:
    """``row``'s status from its stored report; with none, offline with no trays."""
    report = await asyncio.to_thread(reports.get, row.id)
    if report is None:
        return PrinterStatus(id=row.bambuddy_printer_id or 0, name=row.name, connected=False)
    return report_status(row, report, now=now)


async def external_count(reports: PrinterReportStore, row: PrinterRow) -> int:
    """How many external holders the stored report lists (``vt_tray``); 1 with no report."""
    report = await asyncio.to_thread(reports.get, row.id)
    if report is None:
        return 1
    holders = report[0].get("vt_tray")
    return len(holders) if isinstance(holders, list) else 1


CommandView = Callable[[LanCommandRun, LanCommandOutcome], BaseModel]
"""A route's 200 body from the run it sent and the run's outcome."""

#: Each command's view, by the command's name (P37): the route that sends it and the command
#: read route answer the same body. A run whose commands do not all map to one view answers
#: its `LanCommandOutcome`.
COMMAND_VIEWS: dict[str, CommandView] = {
    "extrusion_cali_get": kprofile_table_view,
    "extrusion_cali_set": kprofile_write_view,
    "extrusion_cali_del": kprofile_write_view,
}


def command_view(run: LanCommandRun | None, outcome: LanCommandOutcome) -> BaseModel:
    """``outcome`` as the route that sent ``run`` answers it (`COMMAND_VIEWS`)."""
    if run is None:
        return outcome
    views = {COMMAND_VIEWS.get(command.name) for command in run.commands}
    if len(views) != 1:
        return outcome
    [view] = views
    return outcome if view is None else view(run, outcome)


def command_response(answer: LanCommandAnswer, run: LanCommandRun | None = None) -> Response:
    """The one HTTP answer of a LAN command run: 200 with its outcome as ``run``'s route
    answers it (`command_view`), or 202 with the run's workflow id while it is open (P27). A
    write that may have been sent is ``confirmed: false`` in a 200 (P30); the failures are
    `ApiError`s raised before this."""
    if isinstance(answer, LanCommandPending):
        body: BaseModel = answer
        code = status.HTTP_202_ACCEPTED
    else:
        body = command_view(run, answer)
        code = status.HTTP_200_OK
    return Response(content=body.model_dump_json(), status_code=code, media_type="application/json")


@dataclass(frozen=True)
class LanCommands:
    """Runs a request's commands on the ``bambuddy`` queue (`execute_lan_commands`)."""

    client_getter: Callable[[], Client]
    task_queue: str

    async def run(
        self, printer_id: str, run: LanCommandRun, *, key: str | None = None
    ) -> LanCommandAnswer:
        return await execute_lan_commands(
            self.client_getter(), self.task_queue, printer_id, run, key=key
        )

    async def exists(self, printer_id: str, run: LanCommandRun, key: str | None) -> bool:
        """Whether ``key`` already started ``run``'s commands (P38)."""
        return await lan_command_exists(self.client_getter(), printer_id, run, key)

    async def started(self, command_id: str) -> LanCommandRun:
        """The commands the run ``command_id`` was started with."""
        return await lan_command_run(self.client_getter(), command_id)
