"""A print run's accepted analyzer fixes (#284), checked and applied before it starts.

The run routes (``api/printing.py``, ``api/library_print.py``) take :data:`FixesDep` and
hand ``accept_run`` its ``amend``: the request with every fix it names laid over it
(``analyzers/apply.py``). The analysis reads exactly what ``POST /analyzers/run`` reads
for the same subject and request (``api/analyzers.py`` ``analysis_context``), which is
imported when a run names a fix: that module imports the print routes' own helpers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, status

from scadbuddy.analyzers.apply import accepted_fixes, amend
from scadbuddy.analyzers.component import DecisionsDep
from scadbuddy.analyzers.context import AnalysisRequest
from scadbuddy.analyzers.runner import apply_decisions, run_checks
from scadbuddy.api.deps import (
    CatalogueDep,
    OutputsDep,
    PathsDep,
    SettingsStoreDep,
    StateDep,
    UploadsDep,
    downloads_of,
)
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.core.problems import DATABASE_ERRORS, DATABASE_UNAVAILABLE_PROBLEM, ApiError
from scadbuddy.print_library.component import PrintLibraryDep
from scadbuddy.printers.component import PrintersDep
from scadbuddy.render.read_budget import budget_of
from scadbuddy.spools.component import SpoolsDep

logger = logging.getLogger(__name__)

Amend = Callable[[PrintRunRequest], Awaitable[PrintRunRequest]]


def analysis_request(request: PrintRunRequest) -> AnalysisRequest:
    """The run's request as the analyzers take it: the same printer, plan, choices,
    plates, copies and options."""
    return AnalysisRequest(
        printer_id=request.printer_id,
        printer=request.printer,
        filament_plan=request.filament_plan,
        choices=request.choices,
        plate_id=request.plate_id,
        all_plates=request.all_plates,
        plate_ids=request.plate_ids,
        copies=request.copies,
        options=request.options,
    )


@dataclass(frozen=True)
class Fixes:
    outputs: OutputsDep
    uploads: UploadsDep
    catalogue: CatalogueDep
    store: SettingsStoreDep
    decisions: DecisionsDep
    paths: PathsDep
    state: StateDep
    library: PrintLibraryDep
    printers: PrintersDep
    spools: SpoolsDep

    def for_output(self, output_id: str) -> Amend:
        return lambda request: self._amend(request, output_id=output_id)

    def for_library(self, file_id: int) -> Amend:
        return lambda request: self._amend(request, library_file_id=file_id)

    async def _amend(
        self,
        request: PrintRunRequest,
        *,
        output_id: str | None = None,
        library_file_id: int | None = None,
    ) -> PrintRunRequest:
        if not request.accepted_fixes:
            return request
        from scadbuddy.api.analyzers import AnalysisTarget, analysis_context

        analysis = analysis_request(request)
        context = await analysis_context(
            AnalysisTarget(output_id=output_id, library_file_id=library_file_id),
            analysis,
            self.outputs,
            self.catalogue,
            self.store,
            self.uploads,
            self.paths,
            budget_of(self.state.settings, analysis.read_budget),
            downloads_of(self.state),
            await self.library.serving_native(),
            self.printers,
            self.spools,
        )
        try:
            stored = await asyncio.to_thread(self.decisions.list, scopes=context.scopes())
        except DATABASE_ERRORS as error:
            logger.warning("could not read analyzer decisions for a print", exc_info=error)
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"the analyzer decision store cannot be reached ({type(error).__name__}), so "
                "the accepted fixes this print names cannot be checked",
                type_=DATABASE_UNAVAILABLE_PROBLEM,
            ) from None
        diagnostics, _ = run_checks(context)
        apply_decisions(diagnostics, stored, context)
        fixes = accepted_fixes(request.accepted_fixes, diagnostics)
        return amend(request, [item for fix in fixes for item in fix.changes])


FixesDep = Annotated[Fixes, Depends()]
