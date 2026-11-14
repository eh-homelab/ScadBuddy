"""The print worker's CRUD routes (#5298): the settings it prints with, for
`crud/print.py` `RemoteSettingsStore`; its print runs (inventory Q10-Q13), for
`RemotePrintRunStore`; how each print ended (Q14), for `RemoteOutcomes`; the records of
its operation kinds (Q9, `crud/operations.py`); and the events it publishes outside a
transaction (Q5), for `RemoteEvents`.

The settings answer holds secrets in plaintext, released only to ``crud.print`` (plan D8):
the Bambuddy key, which the Bambuddy spool provider also uses (`PRINT_SECRETS`). Every
other sealed field is null (`print_projection`). The CRUD router answers
``Cache-Control: no-store``, and each answer is logged with the secrets it released and
the credential it released them to, never their values: at INFO the first time per
credential in this process (of the last `RELEASED_KEPT`), then at DEBUG, since the worker
reads them on every use.

Every run write is natural (plan D5). An insert sent again finds the execution's row by
its workflow ids and answers it. A start-enqueue sets its flag again. A succeed, fail,
fail-lost or lan-stopped sent again finds the row ended (or stopped) and answers it as it
is, so only the write that changed it announces it. A result rewrite sent again finds
the same result there and answers the row, unannounced. Each write runs
the API's `PrintRunStore`, so its ``print.run`` event and activity row commit with it
(D7). The insert prunes past the API's own ``print_run_retention_seconds``. An outcome's
record is natural on its run (``ON CONFLICT (run_id) DO NOTHING``), and its
``print.outcome`` commits with it.

The queue item ScadBuddy's reprint queues is recorded as a reprint of the archive's
subject (`POST /reprints`, #3741), so the Prints page marks it. It is not a send. Natural:
a reprint recorded again keeps the first record.

The archives a subject's prints produced, the queue items a run queued and an output's
last print (Q20, Q21, #5298 Task 28) are `bambuddy/print_links.py` `PrintLinkStore`'s
and `library/output_prints.py` `OutputPrintStore`'s, for `RemotePrintLinks` and
`RemoteOutputPrints` (area ``links``: ``…/links``, ``…/sends``, ``…/last-prints``). Each
write is natural: a link, a send and a library file's link are ``ON CONFLICT DO
NOTHING`` on their keys, a gone item sets a flag, and a last print is set by the
output's key, so a resend after a lost answer leaves what one send left.

A LAN-only printer's job the print watcher read (#4699 phase 4, `print_watch/lan.py`) is
recorded here (`POST /lan-imports`, area ``lan-imports``): the API matches a model and
writes the record and its activity row (`print_watch/lan_store.py` `StoreLanRecorder`).
Natural on the job (the printer, its ``subtask_name`` and start): a record sent again
answers the first.

A printer row a run's first check needs (`bambuddy/print_run.py` `_ensure_row`) is made
or merged here (`POST /printers/ensure-bambuddy`, area ``printer-ensure``, #5298 Task
29's route, taken early by #4625): a merge rewrites every table `printers/store.py`
`PRINTER_REFERENCES` names, and those writes are the API's, never the worker's role's.
Natural: decided under the registry's lock (``pg_advisory_xact_lock`` on
``scadbuddy:printers``), so a call sent again finds the row and writes nothing.

The rest of the registry and the reports (Q22-Q26, area ``printers``, #5298 Task 29) are
`PrinterStore`'s and `PrinterReportStore`'s, for `RemotePrinterStore` and
`RemoteReports`: the reads, a printer's access code, a virtual printer's sync after a
write, and the watch's reports. The access code is answered in plaintext to
``crud.print`` only (plan D8), opened here under the API's KEK, and its answer is logged
with the printer and the credential, never the code. A virtual printer's row and the
listed marks answer what that write changed, and a report's ``put`` the print that write
showed ended, so each takes an ``Idempotency-Key``: a resend answers the first write's
answer. A ``touch`` never moves ``seen_at`` back, so it is natural.

The uploads and slices (Q15, Q17) are `bambuddy/uploads.py` `BambuddyUploadStore`'s, for
`RemoteUploadStore`. Each write but one is natural: a record is ``ON CONFLICT`` on the
key the caller names, a touch, a hash and a forget set or delete by key. A slice start is
a plain insert, so it takes an ``Idempotency-Key`` and runs with its ledger row
(`crud/idempotency.py`): a resend after a lost answer records one start.

The print dialog's background slices (Q16), the projects' printers, the projects
created without their folder yet and the destinations' last touches (Q18) are the same
store's. A background slice's record is a plain insert and its claim answers whether the
sweep took it first, so both take an ``Idempotency-Key``: a resend after a lost answer
records one slice, and a claim it won answers true again, though the slice was retired
since. Every other write sets or deletes by key.

The copy lease (#5298 Task 27, `bambuddy/copy_leases.py`) is the copy lock the worker
holds while it finds, uploads and records a library copy: ``POST /copy-leases/{key}``
takes it for ``token`` (waiting on the API up to ``wait_s``, at most 25 s) or answers 409
``busy``, ``PUT`` renews it or answers 409 ``lease-lost``, and ``DELETE`` releases it.
Each is natural on the token. An upload's record that names a lease is written only
while that lease holds, else 409 ``lease-lost``.

The rack hotends' usage (Q27, area ``rack``, #5298 Task 30) is `rack/usage.py`
`RackUsageStore`'s, for `RemoteRackUsage`: ``…/rack/seen`` (natural, an upsert by
serial), the reads ``…/rack/usage`` (a ``POST``, so the hotends' serials stay out of the
URL), ``…/rack/picked`` and ``…/rack/recorded-archives``, and the writes ``…/rack/picks``,
``…/rack/prints``, ``…/rack/lan-picks``, ``…/rack/settle-lan`` and
``…/rack/settle-lan-ended``. Each of those answers the rows it wrote, which a resend
would not repeat, so each takes an ``Idempotency-Key`` and runs with its ledger row in
one transaction. A LAN start's picks delete the earlier pick's rows and insert the new
ones: that is one route, one transaction here (plan D6), never several calls.

An event the worker posts is logged once by its id, and NOTIFYed only when logged
(`PgNotifyEventBus.publish_once`). The worker may post only the kinds it publishes
outside a transaction (`EVENT_KINDS`); any other is a 403, so it cannot announce what
only the API writes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Query, Request, status
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.api.deps import (
    EventsDep,
    OutputsDep,
    PrintLinksDep,
    PrintRunsDep,
    SettingsStoreDep,
    StateDep,
    UploadsDep,
)
from scadbuddy.auth.gate import caller_of
from scadbuddy.auth.models import Service
from scadbuddy.bambuddy.copy_leases import (
    COPY_LEASE_TTL,
    MAX_TTL,
    MAX_WAIT,
    CopyLease,
    CopyLeases,
    LeaseLostError,
)
from scadbuddy.bambuddy.outcomes import PrintOutcomeStore
from scadbuddy.bambuddy.print_links import LinkedPrint, PrintLink, PrintReprint, PrintSend
from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.progress import PrintProgress
from scadbuddy.bambuddy.runs import PrintRun, PrintRunError
from scadbuddy.bambuddy.stages import Stage
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.bambuddy.uploads import (
    BambuddyUploadStore,
    DestinationKind,
    LibraryCopy,
    PreviewSlice,
    ProjectTarget,
    SlicedCopy,
)
from scadbuddy.core.events import PrintOutcome, decode_event, emit
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.problems import ApiError
from scadbuddy.core.secrets import SealError
from scadbuddy.crud.guard import crud_router
from scadbuddy.crud.idempotency import MaybeIdempotencyKey, Replay, idempotent_write, required_key
from scadbuddy.crud.operations import operations_router
from scadbuddy.crud.print import (
    ACCESS_CODE_UNREADABLE,
    NO_ACCESS_CODE,
    NO_SUCH_PRINTER,
    SERIAL_TAKEN,
)
from scadbuddy.crud.wire import NOT_CONFIGURED
from scadbuddy.library.output_prints import OutputPrintStore
from scadbuddy.library.outputs import LanPlateSend, PlateSend
from scadbuddy.library.settings_store import PRINT_SECRETS, PrintSettings, print_projection
from scadbuddy.print_watch.component import AutoImportsDep
from scadbuddy.print_watch.lan import LanCapture, LanImport
from scadbuddy.print_watch.lan_store import StoreLanRecorder
from scadbuddy.printers.component import PrintersDep
from scadbuddy.printers.lan_models import LanStopped
from scadbuddy.printers.models import BambuddyView, PrinterRow
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import (
    KekMissingError,
    NoAccessCodeError,
    PrinterNotFoundError,
    SerialTakenError,
    VirtualOutcome,
    printer_uuid,
)
from scadbuddy.rack.component import RackUsageDep
from scadbuddy.rack.usage import PickedHotend, RackUsageStore

logger = logging.getLogger(__name__)

router = crud_router("print")
# The `Operation` records of the Bambuddy kinds the worker runs (Q9).
router.routes.extend(operations_router("print").routes)

#: A run the request names that is not kept.
NO_SUCH_RUN = "no-such-run"
#: An event of a kind the print worker does not publish.
EVENT_KIND_REFUSED = "event-kind-refused"
#: The kinds the print worker publishes outside a transaction (inventory Q5):
#: `bambuddy/progress.py` `ProgressObserver` and `bambuddy/follow.py` (``print.progress``,
#: ``print.settled``), `printers/watch.py` (``printer.report``) and `printers/store.py`
#: (``printer.changed``).
EVENT_KINDS = frozenset({"print.progress", "print.settled", "printer.report", "printer.changed"})

#: How many credentials `_released_to` keeps. A credential lives 15 minutes and one print
#: worker holds one at a time, so this is many hours of them; one dropped from it is
#: logged at INFO again, once.
RELEASED_KEPT = 256
#: The credentials this process has released the settings to lately, logged at INFO,
#: newest last. Bounded: a long-lived API would otherwise keep every id it ever saw.
_released_to: OrderedDict[str | None, None] = OrderedDict()


def _first_release(credential_id: str | None) -> bool:
    """Whether ``credential_id`` is not among the last `RELEASED_KEPT`; it is kept now."""
    first = credential_id not in _released_to
    _released_to[credential_id] = None
    _released_to.move_to_end(credential_id)
    while len(_released_to) > RELEASED_KEPT:
        _released_to.popitem(last=False)
    return first


def released(current: PrintSettings) -> list[str]:
    """The names of the secrets in ``current``, for the log."""
    return sorted(name for name in PRINT_SECRETS if getattr(current, name) is not None)


@router.get("/settings", name="crud_print_settings")
async def print_settings(request: Request, state: StateDep) -> PrintSettings:
    snapshot = await asyncio.to_thread(state.settings_store.snapshot)
    current = print_projection(snapshot)
    # The guard admitted only a print `Service`.
    principal = caller_of(request.scope)
    assert isinstance(principal, Service)
    # The worker reads them on every use: INFO once per credential in this process.
    first = _first_release(principal.credential_id)
    logger.log(
        logging.INFO if first else logging.DEBUG,
        "released the print settings to service credential %s, secrets: %s",
        principal.credential_id,
        ", ".join(released(current)) or "none",
    )
    return current


class RunInsert(BaseModel):
    run_id: str
    #: The run subject (`PrintSubject.run_subject`).
    subject: str
    key: str
    slug: str
    workflow_id: str
    workflow_run_id: str
    printer: str | None = None


class RunResult(BaseModel):
    slug: str
    result: PrintRunResult


class RunFailure(BaseModel):
    slug: str
    error: PrintRunError
    unqueued: bool = False


class OutcomeWrite(BaseModel):
    #: The run subject.
    subject: str
    slug: str
    progress: PrintProgress
    #: One run's outcome (`PrintOutcomeStore.record_run`); none, the subject's newest.
    run_id: str | None = None


class Recorded(BaseModel):
    #: The event this write published; none when it was recorded already, or the read
    #: is not its run's print.
    outcome: PrintOutcome | None


class EventPost(BaseModel):
    event: dict[str, Any]


class Logged(BaseModel):
    #: False when the log held the event already: a resend.
    logged: bool


def _no_such_run(run_id: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"No print run {run_id}.", reason=NO_SUCH_RUN)


def _outcomes(runs: PrintRunsDep) -> PrintOutcomeStore:
    if runs.outcomes is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "Print outcomes are not kept here.")
    return runs.outcomes


@router.post("/runs", name="crud_print_insert_run")
async def insert_run(body: RunInsert, runs: PrintRunsDep, settings: SettingsStoreDep) -> PrintRun:
    seconds = (await asyncio.to_thread(settings.load)).print_run_retention_seconds
    return await runs.store.insert_accepted(
        body.run_id,
        subject=PrintSubject.from_run_subject(body.subject),
        key=body.key,
        slug=body.slug,
        workflow_id=body.workflow_id,
        workflow_run_id=body.workflow_run_id,
        retention=timedelta(seconds=seconds) if seconds is not None else None,
        printer=body.printer,
    )


# The fixed paths come before `/runs/{run_id}`, which would take them for a run id.
@router.get("/runs", name="crud_print_get_runs")
async def get_runs(
    runs: PrintRunsDep, ids: Annotated[list[str], Query(default_factory=list)]
) -> dict[str, PrintRun]:
    return await runs.store.get_many(ids)


@router.get("/runs/latest", name="crud_print_latest_run")
async def latest_run(output_id: str, runs: PrintRunsDep) -> PrintRun | None:
    return await runs.store.latest_for_output(output_id)


@router.get("/runs/find", name="crud_print_find_run")
async def find_run(key: str, has_request_id: bool, runs: PrintRunsDep) -> PrintRun | None:
    return await runs.store.find(key, has_request_id=has_request_id)


@router.get("/runs/lan-waiting", name="crud_print_lan_waiting")
async def lan_waiting(
    runs: PrintRunsDep, printers: Annotated[list[str], Query(default_factory=list)]
) -> dict[str, PrintRun]:
    # Registry ids, as `PrintRunStore.lan_waiting` takes them; one that is no UUID would
    # fail its cast to ``uuid[]`` as a 500.
    for printer in printers:
        try:
            UUID(printer)
        except ValueError:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"Not a printer id: {printer!r}."
            ) from None
    return await runs.store.lan_waiting(printers)


@router.get("/runs/running", name="crud_print_running_runs")
async def running_runs(
    runs: PrintRunsDep, output_id: Annotated[list[str], Query(default_factory=list)]
) -> dict[str, list[PrintRun]]:
    return await runs.store.running_for_outputs(output_id)


@router.get("/runs/{run_id}", name="crud_print_get_run")
async def get_run(run_id: str, runs: PrintRunsDep) -> PrintRun:
    run = await runs.store.get(run_id)
    if run is None:
        raise _no_such_run(run_id)
    return run


@router.post(
    "/runs/{run_id}/start-enqueue",
    name="crud_print_start_enqueue",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def start_enqueue(run_id: str, runs: PrintRunsDep) -> None:
    await runs.store.start_enqueue(run_id)


@router.post("/runs/{run_id}/succeed", name="crud_print_succeed_run")
async def succeed_run(run_id: str, body: RunResult, runs: PrintRunsDep) -> PrintRun:
    try:
        return await runs.store.succeed(run_id, body.slug, body.result)
    except LookupError:
        raise _no_such_run(run_id) from None


@router.post("/runs/{run_id}/result", name="crud_print_update_result")
async def update_result(run_id: str, body: RunResult, runs: PrintRunsDep) -> PrintRun:
    try:
        return await runs.store.update_result(run_id, body.slug, body.result)
    except LookupError:
        raise _no_such_run(run_id) from None


@router.post("/runs/{run_id}/fail", name="crud_print_fail_run")
async def fail_run(run_id: str, body: RunFailure, runs: PrintRunsDep) -> PrintRun:
    try:
        return await runs.store.fail(run_id, body.slug, body.error, unqueued=body.unqueued)
    except LookupError:
        raise _no_such_run(run_id) from None


@router.post("/runs/{run_id}/fail-lost", name="crud_print_fail_lost_run")
async def fail_lost_run(run_id: str, runs: PrintRunsDep) -> PrintRun:
    try:
        return await runs.store.fail_lost(run_id)
    except LookupError:
        raise _no_such_run(run_id) from None


@router.post("/runs/{run_id}/lan-stopped", name="crud_print_lan_stopped")
async def lan_stopped(run_id: str, body: LanStopped, runs: PrintRunsDep) -> PrintRun:
    run = await runs.store.record_lan_stopped(run_id, body)
    if run is None:
        raise _no_such_run(run_id)
    return run


@router.post("/outcomes", name="crud_print_record_outcome")
async def record_outcome(body: OutcomeWrite, runs: PrintRunsDep) -> Recorded:
    outcomes = _outcomes(runs)
    if body.run_id is None:
        return Recorded(outcome=await outcomes.record(body.subject, body.slug, body.progress))
    run = await runs.store.get(body.run_id)
    if run is None or run.output_id != body.subject:
        raise _no_such_run(body.run_id)
    return Recorded(outcome=await outcomes.record_run(run, body.slug, body.progress))


@router.get("/outcomes/older-open", name="crud_print_older_open_runs")
async def older_open(subject: str, runs: PrintRunsDep) -> list[PrintRun]:
    return await _outcomes(runs).older_open(subject)


class ReprintWrite(BaseModel):
    """`PrintLinkStore.record_reprint`'s arguments (#3741)."""

    subject: str
    reprint: PrintReprint


@router.post("/reprints", name="crud_print_record_reprint", status_code=status.HTTP_204_NO_CONTENT)
async def record_reprint(body: ReprintWrite, links: PrintLinksDep) -> None:
    """The queue item ScadBuddy's reprint of an archive queued (#3741), so the Prints
    page marks it. Natural: a reprint recorded again keeps the first record."""
    try:
        subject = PrintSubject.parse(body.subject)
    except ValueError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    await links.record_reprint(subject, body.reprint)


# -- links, sends and an output's last print (Q20, Q21) ----------------------------


def _subject(key: str) -> PrintSubject:
    try:
        return PrintSubject.parse(key)
    except ValueError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error


class LinkWrite(BaseModel):
    """`PrintLinkStore.record`'s arguments."""

    subject: str
    link: PrintLink


class SendsWrite(BaseModel):
    """`PrintLinkStore.record_sends`'s arguments."""

    subject: str
    sends: list[PrintSend]


class LibraryLink(BaseModel):
    """`PrintLinkStore.link_library`'s arguments but the queue item."""

    archive_id: int
    name: str | None = None


class LastPrintWrite(BaseModel):
    """`OutputPrintStore.record`'s arguments but the output."""

    queue_item_id: int | None = None
    slice_job_id: int | None = None
    project_id: int | None = None
    plates: list[PlateSend] = Field(default_factory=list)


class LanLastPrintWrite(BaseModel):
    """`OutputPrintStore.record_lan`'s arguments but the output."""

    run_id: str
    printer: str | None = None
    plates: list[LanPlateSend] = Field(default_factory=list)


class LanProgressWrite(BaseModel):
    """`OutputPrintStore.record_lan_progress`'s arguments but the output."""

    run_id: str
    stage: Stage
    settled: bool


@router.post("/links", name="crud_print_record_link", status_code=status.HTTP_204_NO_CONTENT)
async def record_link(body: LinkWrite, links: PrintLinksDep) -> None:
    """Natural: a second sighting of the archive keeps the first link."""
    await links.record(_subject(body.subject), body.link)


@router.get("/links", name="crud_print_links_for")
async def links_for(subject: str, links: PrintLinksDep) -> list[PrintLink]:
    return await links.for_subject(_subject(subject))


@router.get("/links/{archive_id}", name="crud_print_linked")
async def linked(archive_id: int, links: PrintLinksDep) -> LinkedPrint | None:
    return await links.linked(archive_id)


@router.post("/sends", name="crud_print_record_sends", status_code=status.HTTP_204_NO_CONTENT)
async def record_sends(body: SendsWrite, links: PrintLinksDep) -> None:
    """Natural: a queue item recorded again keeps its first record."""
    await links.record_sends(_subject(body.subject), body.sends)


@router.get("/sends", name="crud_print_sends_for")
async def sends_for(
    subject: str, links: PrintLinksDep, run_id: str | None = None
) -> list[PrintSend]:
    """The subject's sends in the order recorded; only ``run_id``'s when given."""
    if run_id is None:
        return await links.sends_for(_subject(subject))
    return await links.run_sends(_subject(subject), run_id)


@router.post(
    "/sends/{queue_item_id}/library-link",
    name="crud_print_link_library",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def link_library(queue_item_id: int, body: LibraryLink, links: PrintLinksDep) -> None:
    """Natural: the first archive the item reported is kept."""
    await links.link_library(queue_item_id, body.archive_id, body.name)


@router.post(
    "/sends/{queue_item_id}/library-gone",
    name="crud_print_library_gone",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def library_gone(queue_item_id: int, links: PrintLinksDep) -> None:
    """Natural: it sets a flag."""
    await links.library_gone(queue_item_id)


def _last_prints(outputs: OutputsDep) -> OutputPrintStore:
    """The API's own `OutputStore.prints` (`api/deps.py`)."""
    if outputs.prints is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "this API keeps no last prints")
    return outputs.prints


LastPrintsDep = Annotated[OutputPrintStore, Depends(_last_prints)]


@router.put(
    "/last-prints/{output_id}",
    name="crud_print_record_last_print",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def record_last_print(output_id: str, body: LastPrintWrite, prints: LastPrintsDep) -> None:
    """Natural: set by the output's key, so a resend leaves what one send left."""
    await asyncio.to_thread(
        prints.record,
        output_id,
        queue_item_id=body.queue_item_id,
        slice_job_id=body.slice_job_id,
        project_id=body.project_id,
        plates=body.plates,
    )


@router.put(
    "/last-prints/{output_id}/lan",
    name="crud_print_record_lan_last_print",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def record_lan_last_print(
    output_id: str, body: LanLastPrintWrite, prints: LastPrintsDep
) -> None:
    """Natural: recorded again for the same run it keeps the stage written since."""
    await asyncio.to_thread(
        prints.record_lan,
        output_id,
        run_id=body.run_id,
        printer=body.printer,
        plates=body.plates,
    )


@router.put(
    "/last-prints/{output_id}/lan-progress",
    name="crud_print_record_lan_progress",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def record_lan_progress(
    output_id: str, body: LanProgressWrite, prints: LastPrintsDep
) -> None:
    """Natural: it sets the run's stage and whether it settled."""
    await asyncio.to_thread(
        prints.record_lan_progress,
        output_id,
        body.run_id,
        stage=body.stage,
        settled=body.settled,
    )


class EnsureBambuddy(BaseModel):
    """`PrinterStore.ensure_bambuddy_written`'s arguments."""

    bambuddy_printer_id: int
    serial: str | None = None
    name: str
    model: str | None = None
    listed: list[int] | None = None


class EnsuredPrinter(BaseModel):
    row: PrinterRow
    written: bool


@router.post("/printers/ensure-bambuddy", name="crud_print_ensure_bambuddy")
async def ensure_bambuddy(body: EnsureBambuddy, printers: PrintersDep) -> EnsuredPrinter:
    row, written = await asyncio.to_thread(
        printers.store.ensure_bambuddy_written,
        body.bambuddy_printer_id,
        serial=body.serial,
        name=body.name,
        model=body.model,
        listed=None if body.listed is None else frozenset(body.listed),
    )
    return EnsuredPrinter(row=row, written=written)


@router.post("/lan-imports", name="crud_print_record_lan_import")
async def record_lan_import(
    body: LanCapture, state: StateDep, auto_imports: AutoImportsDep
) -> LanImport:
    """A LAN-only printer's job, as the print worker read it (#4699 phase 4): matched to a
    model, or put in the inbox, and recorded with its activity row. Natural on the job
    (the printer, ``subtask_name`` and ``started_at``): sent again, it answers the record
    as it is and writes nothing."""
    if printer_uuid(body.job.printer_id) is None:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, f"no printer {body.job.printer_id}")
    recorder = StoreLanRecorder(auto_imports.lan, state.catalogue.slugs, state.catalogue.exists)
    return await recorder.record(body)


# -- the registry's reads, the access code, the virtual printers' sync and the reports
# (Q22-Q26, area ``printers``) -----------------------------------------------------------


class AccessCode(BaseModel):
    code: str


class VirtualWrite(BaseModel):
    """`PrinterStore.upsert_virtual`'s arguments but the VP's id, which the path names."""

    serial: str | None = None
    name: str
    model: str | None = None
    mode: str
    forwards_to: str | None = None
    view: BambuddyView
    may_adopt: bool = True


class VirtualSynced(BaseModel):
    row: PrinterRow
    outcome: VirtualOutcome
    changed: bool


class Listed(BaseModel):
    listed: list[int]


class ListedChanged(BaseModel):
    #: The rows whose ``bambuddy_active`` this write changed.
    changed: list[str]


class ReportWrite(BaseModel):
    report: dict[str, Any]
    seen_at: datetime


class EndedPrint(BaseModel):
    """`printers/reports.py` `PrintEnd`."""

    subtask_name: str
    timed: bool


class ReportWritten(BaseModel):
    #: The print this write showed ended, if any; a resend answers the first write's.
    ended: EndedPrint | None
    #: The print this write showed start (`printers/reports.py` `started_print`), for
    #: the print watcher's LAN nudge (#4699 phase 3); a resend answers the first write's.
    started: str | None = None


class Seen(BaseModel):
    seen_at: datetime


class SeenReport(BaseModel):
    report: dict[str, Any]
    seen_at: datetime


VIRTUAL_ROUTE = "PUT /printers/virtual/{vp_id}"
LISTED_ROUTE = "POST /printers/virtual-listed"
REPORT_ROUTE = "PUT /reports/{printer_id}"


@router.get("/printers", name="crud_print_printers")
async def list_printers(printers: PrintersDep) -> list[PrinterRow]:
    return await asyncio.to_thread(printers.store.list)


@router.get("/printers/by-serial", name="crud_print_printer_by_serial")
async def printer_by_serial(serial: str, printers: PrintersDep) -> PrinterRow | None:
    return await asyncio.to_thread(printers.store.by_serial, serial)


@router.get(
    "/printers/by-bambuddy-id/{bambuddy_printer_id}", name="crud_print_printer_by_bambuddy_id"
)
async def printer_by_bambuddy_id(
    bambuddy_printer_id: int, printers: PrintersDep
) -> PrinterRow | None:
    return await asyncio.to_thread(printers.store.by_bambuddy_id, bambuddy_printer_id)


@router.get("/printers/{printer_id}", name="crud_print_printer")
async def get_printer(printer_id: str, printers: PrintersDep) -> PrinterRow | None:
    return await asyncio.to_thread(printers.store.get, printer_id)


@router.get("/printers/{printer_id}/access-code", name="crud_print_access_code")
async def access_code(printer_id: str, request: Request, printers: PrintersDep) -> AccessCode:
    """The printer's LAN access code in plaintext, to ``crud.print`` only (plan D8): the
    watch and a LAN start dial the printer with it. The answer is ``no-store`` (the
    CRUD router's headers), and the log names the printer and the credential, never the
    code."""
    principal = caller_of(request.scope)
    assert isinstance(principal, Service)
    try:
        code = await asyncio.to_thread(printers.store.open_access_code, printer_id)
    except PrinterNotFoundError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, "There is no such printer.", reason=NO_SUCH_PRINTER
        ) from None
    except NoAccessCodeError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "This printer has no LAN access code.",
            reason=NO_ACCESS_CODE,
        ) from None
    except KekMissingError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The API has no SCADBUDDY_SECRET_KEY_FILE, so it cannot open an access code.",
            reason=NOT_CONFIGURED,
        ) from None
    except SealError:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "This printer's access code was sealed under another key; link it again.",
            reason=ACCESS_CODE_UNREADABLE,
        ) from None
    logger.info(
        "released printer %s's access code to service credential %s",
        printer_id,
        principal.credential_id,
    )
    return AccessCode(code=code)


@router.put("/printers/virtual/{vp_id}", name="crud_print_upsert_virtual")
async def upsert_virtual(
    vp_id: int,
    body: VirtualWrite,
    printers: PrintersDep,
    idempotency_key: MaybeIdempotencyKey = None,
) -> VirtualSynced:
    """A virtual printer's row, under the registry's lock. Once per ``Idempotency-Key``:
    a resend answers what the first write did, not ``already_linked``."""
    key = required_key(idempotency_key)
    store = printers.store

    def write(conn: Connection[DictRow]) -> dict[str, Any]:
        row, outcome, changed = store.upsert_virtual_in(
            conn,
            vp_id,
            serial=body.serial,
            name=body.name,
            model=body.model,
            mode=body.mode,
            forwards_to=body.forwards_to,
            view=body.view,
            may_adopt=body.may_adopt,
        )
        return {"row": row.model_dump(mode="json"), "outcome": outcome, "changed": changed}

    try:
        answer = await asyncio.to_thread(
            _once_on,
            store.pool,
            key,
            VIRTUAL_ROUTE,
            {"vp_id": vp_id, **body.model_dump(mode="json")},
            write,
        )
    except SerialTakenError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "Another printer row holds this virtual printer's serial.",
            reason=SERIAL_TAKEN,
            existing_id=error.existing_id,
        ) from None
    return VirtualSynced.model_validate(answer)


@router.post("/printers/virtual-listed", name="crud_print_mark_virtual_listed")
async def mark_virtual_listed(
    body: Listed, printers: PrintersDep, idempotency_key: MaybeIdempotencyKey = None
) -> ListedChanged:
    """Which virtual printers Bambuddy lists now. Once per ``Idempotency-Key``: a resend
    answers the rows the first write changed."""
    key = required_key(idempotency_key)
    store = printers.store
    answer = await asyncio.to_thread(
        _once_on,
        store.pool,
        key,
        LISTED_ROUTE,
        body.model_dump(mode="json"),
        lambda conn: {"changed": list(store.mark_virtual_listed_in(conn, body.listed))},
    )
    return ListedChanged.model_validate(answer)


@router.get("/reports", name="crud_print_reports")
async def all_reports(printers: PrintersDep) -> dict[str, SeenReport]:
    seen = await asyncio.to_thread(printers.reports.all)
    return {
        printer_id: SeenReport(report=report, seen_at=seen_at)
        for printer_id, (report, seen_at) in seen.items()
    }


@router.get("/reports/{printer_id}", name="crud_print_report")
async def get_report(printer_id: str, printers: PrintersDep) -> SeenReport | None:
    seen = await asyncio.to_thread(printers.reports.get, printer_id)
    return None if seen is None else SeenReport(report=seen[0], seen_at=seen[1])


@router.put("/reports/{printer_id}", name="crud_print_put_report")
async def put_report(
    printer_id: str,
    body: ReportWrite,
    printers: PrintersDep,
    idempotency_key: MaybeIdempotencyKey = None,
) -> ReportWritten:
    """The watch's write. Only the write that shows a print ended answers that end, and
    only the one that shows a print start answers that start, so once per
    ``Idempotency-Key``: a resend after a lost answer answers the same end and start."""
    # One ledger row per report written: the watch puts about once a `watch.py`
    # `write_every` (1 s) while a printer reports, and at once on a shown change, so up
    # to tens of thousands of rows a day per printer. `crud/idempotency.py` `prune` drops
    # each after `RETENTION` (24 h), which bounds them.
    key = required_key(idempotency_key)
    reports = printers.reports

    def write(conn: Connection[DictRow]) -> dict[str, Any]:
        advance = PrinterReportStore.advance_in(conn, printer_id, body.report, body.seen_at)
        ended = advance.ended
        return {
            "ended": None if ended is None else dataclasses.asdict(ended),
            "started": advance.started,
        }

    answer = await asyncio.to_thread(
        _once_on,
        reports.pool,
        key,
        REPORT_ROUTE,
        {"printer_id": printer_id, **body.model_dump(mode="json")},
        write,
    )
    return ReportWritten.model_validate(answer)


@router.post(
    "/reports/{printer_id}/touch",
    name="crud_print_touch_report",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def touch_report(printer_id: str, body: Seen, printers: PrintersDep) -> None:
    """Natural: ``seen_at`` never moves back."""
    await asyncio.to_thread(printers.reports.touch, printer_id, body.seen_at)


@router.post("/events", name="crud_print_publish_event")
async def publish_event(body: EventPost, events: EventsDep) -> Logged:
    kind = body.event.get("kind")
    # Not a string (a list, an object) is no kind: refused, never hashed.
    if not isinstance(kind, str) or kind not in EVENT_KINDS:
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            f"The print worker does not publish {kind!r} events.",
            reason=EVENT_KIND_REFUSED,
        )
    try:
        event = decode_event(json.dumps(body.event))
    except ValueError as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Not a {kind} event: {error}"
        ) from None
    if isinstance(events, PgNotifyEventBus):
        return Logged(logged=await events.publish_once(event))
    # A test's in-process bus keeps no log to find a resend in.
    emit(events, event)
    return Logged(logged=True)


# -- uploads and slices (inventory Q15, Q17) ----------------------------------------


class CopyWrite(BaseModel):
    output_id: str
    upload: LibraryCopy
    #: The copy lease it was uploaded under: written only while that holds.
    lease: CopyLease | None = None


class CopyKey(BaseModel):
    output_id: str
    library_file_id: int


class FileIds(BaseModel):
    ids: list[int]


class HeldCopy(BaseModel):
    """A copy that holds the content asked for, and the output it is recorded under."""

    output_id: str
    upload: LibraryCopy


class SentBetween(BaseModel):
    first_upload: datetime
    last_slice: datetime


class SliceWrite(BaseModel):
    output_id: str
    library_file_id: int
    sliced: SlicedCopy


class SliceHash(BaseModel):
    output_id: str
    file_hash: str


class SliceStart(BaseModel):
    library_file_id: int
    job_id: int | None = None


SLICE_START_ROUTE = "POST /slice-starts"


@router.get("/uploads", name="crud_print_uploads")
async def uploads_for(
    uploads: UploadsDep,
    output_id: Annotated[list[str], Query(default_factory=list)],
    native: bool = False,
) -> dict[str, list[LibraryCopy]]:
    return await uploads.for_outputs(output_id, native=native)


@router.post("/uploads", name="crud_print_record_upload", status_code=status.HTTP_204_NO_CONTENT)
async def record_upload(body: CopyWrite, uploads: UploadsDep) -> None:
    try:
        await uploads.record(body.output_id, body.upload, body.lease)
    except LeaseLostError as error:
        raise _lease_lost() from error


#: A lease another holder keeps for all of the wait, and one its token no longer holds.
LEASE_BUSY = "busy"
LEASE_LOST = "lease-lost"


def _lease_lost() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT, "The copy lease is not held by this token.", reason=LEASE_LOST
    )


class LeaseAsk(BaseModel):
    token: UUID
    ttl_s: float = Field(gt=0, le=MAX_TTL)
    wait_s: float = Field(default=0, ge=0, le=MAX_WAIT)


class LeaseRenew(BaseModel):
    token: UUID
    ttl_s: float = Field(default=COPY_LEASE_TTL, gt=0, le=MAX_TTL)


class LeaseHeld(BaseModel):
    token: UUID
    expires_at: datetime


@router.post("/copy-leases/{key:path}", name="crud_print_acquire_copy_lease")
async def acquire_copy_lease(key: str, body: LeaseAsk, uploads: UploadsDep) -> LeaseHeld:
    expires = await CopyLeases(uploads.pool).acquire(key, str(body.token), body.ttl_s, body.wait_s)
    if expires is None:
        raise ApiError(
            status.HTTP_409_CONFLICT, "Another holder keeps the copy lease.", reason=LEASE_BUSY
        )
    return LeaseHeld(token=body.token, expires_at=expires)


@router.put("/copy-leases/{key:path}", name="crud_print_renew_copy_lease")
async def renew_copy_lease(key: str, body: LeaseRenew, uploads: UploadsDep) -> LeaseHeld:
    expires = await CopyLeases(uploads.pool).renew(key, str(body.token), body.ttl_s)
    if expires is None:
        raise _lease_lost()
    return LeaseHeld(token=body.token, expires_at=expires)


@router.delete(
    "/copy-leases/{key:path}",
    name="crud_print_release_copy_lease",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def release_copy_lease(key: str, token: UUID, uploads: UploadsDep) -> None:
    await CopyLeases(uploads.pool).release(key, str(token))


@router.get("/uploads/same-content", name="crud_print_same_content")
async def same_content(
    uploads: UploadsDep, target_key: str, content_key: str, folder_id: int | None = None
) -> list[HeldCopy]:
    found = await uploads.same_content(folder_id, target_key, content_key)
    return [HeldCopy(output_id=output_id, upload=copy) for output_id, copy in found]


@router.get("/uploads/shared", name="crud_print_shared_uploads")
async def shared_uploads(
    uploads: UploadsDep, output_id: str, id: Annotated[list[int], Query(default_factory=list)]
) -> list[int]:
    return sorted(await uploads.shared(id, output_id))


@router.get("/uploads/recorded", name="crud_print_recorded_uploads")
async def recorded_uploads(
    uploads: UploadsDep, id: Annotated[list[int], Query(default_factory=list)]
) -> list[int]:
    return sorted(await uploads.recorded(id))


@router.get("/uploads/recorded-files", name="crud_print_recorded_files")
async def recorded_files(
    uploads: UploadsDep, id: Annotated[list[int], Query(default_factory=list)]
) -> list[int]:
    return sorted(await uploads.recorded_files(id))


@router.get("/uploads/sent-between", name="crud_print_sent_between")
async def sent_between(output_id: str, uploads: UploadsDep) -> SentBetween | None:
    window = await uploads.sent_between(output_id)
    return None if window is None else SentBetween(first_upload=window[0], last_slice=window[1])


@router.post(
    "/uploads/touch", name="crud_print_touch_upload", status_code=status.HTTP_204_NO_CONTENT
)
async def touch_upload(body: CopyKey, uploads: UploadsDep) -> None:
    await uploads.touch(body.output_id, body.library_file_id)


@router.post(
    "/uploads/forget", name="crud_print_forget_upload", status_code=status.HTTP_204_NO_CONTENT
)
async def forget_upload(body: CopyKey, uploads: UploadsDep) -> None:
    await uploads.forget(body.output_id, body.library_file_id)


@router.post(
    "/uploads/forget-files",
    name="crud_print_forget_upload_files",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def forget_upload_files(body: FileIds, uploads: UploadsDep) -> None:
    await uploads.forget_files(body.ids)


@router.post("/slices", name="crud_print_record_slice", status_code=status.HTTP_204_NO_CONTENT)
async def record_slice(body: SliceWrite, uploads: UploadsDep) -> None:
    await uploads.record_sliced(body.output_id, body.library_file_id, body.sliced)


@router.get("/slices", name="crud_print_slices_of")
async def slices_of(
    output_id: str, library_file_id: int, preset_key: str, uploads: UploadsDep
) -> list[SlicedCopy]:
    return await uploads.slices_of(output_id, library_file_id, preset_key)


@router.post(
    "/slices/{sliced_id}/forget",
    name="crud_print_forget_slice",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def forget_slice(sliced_id: int, output_id: str, uploads: UploadsDep) -> None:
    """One subject's slice in ``sliced_id``."""
    await uploads.forget_slice(output_id, sliced_id)


@router.post(
    "/slices/{sliced_id}/gone",
    name="crud_print_forget_gone_slice",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def forget_gone_slice(sliced_id: int, uploads: UploadsDep) -> None:
    """Every subject's slice in ``sliced_id``: Bambuddy no longer has the file."""
    await uploads.forget_gone_slice(sliced_id)


@router.put(
    "/slices/{sliced_id}/hash",
    name="crud_print_record_slice_hash",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def record_slice_hash(sliced_id: int, body: SliceHash, uploads: UploadsDep) -> None:
    await uploads.record_slice_hash(body.output_id, sliced_id, body.file_hash)


def _once(
    uploads: BambuddyUploadStore,
    key: str,
    route: str,
    body: dict[str, Any],
    write: Callable[[Connection[DictRow]], Any],
) -> Any:
    """``write``'s JSON answer, once per Idempotency-Key ``key``: the write and its
    ledger row in one transaction (plan D5)."""
    return _once_on(uploads.pool, key, route, body, write)


def _once_on(
    pool: ConnectionPool[Connection[DictRow]],
    key: str,
    route: str,
    body: dict[str, Any],
    write: Callable[[Connection[DictRow]], Any],
) -> Any:
    """`_once` on ``pool``."""
    with pool.connection() as conn:
        return idempotent_write(
            conn, "print", key, route, body, lambda: Replay(200, write(conn))
        ).body


@router.post(
    "/slice-starts", name="crud_print_record_slice_start", status_code=status.HTTP_204_NO_CONTENT
)
async def record_slice_start(
    body: SliceStart, uploads: UploadsDep, idempotency_key: MaybeIdempotencyKey = None
) -> None:
    """A plain insert: once per ``Idempotency-Key``, so a resend records one start."""
    key = required_key(idempotency_key)
    await asyncio.to_thread(
        _once,
        uploads,
        key,
        SLICE_START_ROUTE,
        body.model_dump(mode="json"),
        lambda conn: BambuddyUploadStore.record_slice_start_in(
            conn, body.library_file_id, body.job_id
        ),
    )


# -- background slices, project targets, folderless projects, touches (Q16, Q18) ----


class PreviewSliced(BaseModel):
    sliced_file_id: int
    name: str | None = None


class Claimed(BaseModel):
    #: False when the sweep took the slice first.
    claimed: bool


class Folderless(BaseModel):
    name: str
    parent_id: int | None = None


class Touch(BaseModel):
    kind: DestinationKind
    target_id: int


PREVIEW_ROUTE = "POST /previews"
CLAIM_ROUTE = "POST /previews/{row_id}/claim"


@router.post("/previews", name="crud_print_record_preview", status_code=status.HTTP_204_NO_CONTENT)
async def record_preview(
    body: PreviewSlice, uploads: UploadsDep, idempotency_key: MaybeIdempotencyKey = None
) -> None:
    """A plain insert: once per ``Idempotency-Key``, so a resend records one slice."""
    key = required_key(idempotency_key)
    await asyncio.to_thread(
        _once,
        uploads,
        key,
        PREVIEW_ROUTE,
        body.model_dump(mode="json"),
        lambda conn: BambuddyUploadStore.record_preview_in(conn, body),
    )


@router.get("/previews", name="crud_print_previews_for")
async def previews_for(
    library_file_id: int, preset_key: str, uploads: UploadsDep
) -> list[PreviewSlice]:
    return await uploads.previews_for(library_file_id, preset_key)


@router.get("/previews/find", name="crud_print_find_preview")
async def find_preview(job_id: int, subject: str, uploads: UploadsDep) -> PreviewSlice | None:
    return await uploads.preview(job_id, subject)


@router.put(
    "/previews/{row_id}/sliced",
    name="crud_print_preview_sliced",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def preview_sliced(row_id: int, body: PreviewSliced, uploads: UploadsDep) -> None:
    await uploads.preview_sliced(row_id, body.sliced_file_id, body.name)


@router.post(
    "/previews/{row_id}/retire",
    name="crud_print_retire_preview",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def retire_preview(row_id: int, uploads: UploadsDep) -> None:
    await uploads.retire_preview(row_id)


@router.post("/previews/{row_id}/claim", name="crud_print_claim_preview")
async def claim_preview(
    row_id: int, uploads: UploadsDep, idempotency_key: MaybeIdempotencyKey = None
) -> Claimed:
    """Once per ``Idempotency-Key``: a claim won answers true to its resend, though the
    slice was retired since, so the run queues the file it holds."""
    key = required_key(idempotency_key)
    answer = await asyncio.to_thread(
        _once,
        uploads,
        key,
        CLAIM_ROUTE,
        {"row_id": row_id},
        lambda conn: {"claimed": BambuddyUploadStore.claim_preview_in(conn, row_id)},
    )
    return Claimed.model_validate(answer)


@router.get("/project-targets/{project_id}", name="crud_print_project_target")
async def project_target(project_id: int, uploads: UploadsDep) -> ProjectTarget | None:
    return await uploads.project_target(project_id)


@router.put(
    "/project-targets/{project_id}",
    name="crud_print_remember_project_target",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remember_project_target(
    project_id: int, body: ProjectTarget, uploads: UploadsDep
) -> None:
    await uploads.remember_project_target(project_id, body)


@router.get("/folderless", name="crud_print_folderless")
async def folderless(name: str, uploads: UploadsDep, parent_id: int | None = None) -> list[int]:
    return await uploads.folderless(name, parent_id)


@router.put(
    "/folderless/{project_id}",
    name="crud_print_remember_folderless",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remember_folderless(project_id: int, body: Folderless, uploads: UploadsDep) -> None:
    await uploads.remember_folderless(project_id, body.name, body.parent_id)


@router.delete(
    "/folderless/{project_id}",
    name="crud_print_forget_folderless",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def forget_folderless(project_id: int, uploads: UploadsDep) -> None:
    await uploads.forget_folderless(project_id)


@router.post(
    "/destination-touches",
    name="crud_print_touch_destination",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def touch_destination(body: Touch, uploads: UploadsDep) -> None:
    await uploads.touch_destination(body.kind, body.target_id)


# -- the rack hotends' usage (Q27, area ``rack``, #5298 Task 30) -------------------------


#: The most serials one rack call takes. A rack holds six hotends (`rack/rank.py`
#: `POSITIONS`, positions 1-6) and the callers send one rack's at a time; 64 is far above
#: that, and bounds the ``IN`` list a buggy caller could send.
RACK_SERIALS_MAX = 64
#: The longest serial taken: a hotend's serial is a short code.
RACK_SERIAL_CHARS = 128
RackSerial = Annotated[str, Field(max_length=RACK_SERIAL_CHARS)]


class RackSeen(BaseModel):
    #: The registry printer (#5167); none for a printer the registry does not hold.
    printer: UUID | None = None
    serials: list[RackSerial] = Field(max_length=RACK_SERIALS_MAX)


class RackSerials(BaseModel):
    serials: list[RackSerial] = Field(max_length=RACK_SERIALS_MAX)


class RackUsageRow(BaseModel):
    """`rack/rank.py` `Usage`."""

    prints: int
    print_seconds: int
    grams: float
    first_seen_at: datetime | None
    pending: int
    last_used_at: datetime | None


class RackPicks(BaseModel):
    queue_item_id: int
    printer: UUID | None = None
    picks: list[PickedHotend]
    copies: int = 1


class RackPrints(BaseModel):
    archive_id: int
    queue_item_id: int
    settled_at: datetime
    print_seconds: int | None = None
    grams: float | None = None


class RackLanPicks(BaseModel):
    run_id: str
    subtask_name: str
    printer: UUID | None = None
    #: A start with no attributable pick saves none (`save_lan_picks`), so none is sent.
    picks: list[PickedHotend] = Field(min_length=1)


class RackSettleLan(BaseModel):
    run_id: str
    subtask_name: str | None = None
    timed: bool = True


class RackSettleLanEnded(BaseModel):
    printer: UUID
    subtask_name: str
    ended_at: datetime
    timed: bool = True


class RackWritten(BaseModel):
    #: The rows the write wrote; a resend under its key answers the first write's.
    written: int


RACK_PICKS_ROUTE = "POST /rack/picks"
RACK_PRINTS_ROUTE = "POST /rack/prints"
RACK_LAN_PICKS_ROUTE = "POST /rack/lan-picks"
RACK_SETTLE_LAN_ROUTE = "POST /rack/settle-lan"
RACK_SETTLE_LAN_ENDED_ROUTE = "POST /rack/settle-lan-ended"


async def _rack_once(
    rack: RackUsageStore,
    key: str | None,
    route: str,
    body: BaseModel,
    write: Callable[[Connection[DictRow]], int],
) -> RackWritten:
    """``write`` once per ``Idempotency-Key``, with its ledger row in its transaction on
    the rack store's pool (plan D5): a resend answers the rows the first write wrote."""
    required = required_key(key)

    def once() -> Any:
        return _once_on(
            rack.pool,
            required,
            route,
            body.model_dump(mode="json"),
            lambda conn: {"written": write(conn)},
        )

    return RackWritten.model_validate(await asyncio.to_thread(once))


@router.post("/rack/seen", name="crud_print_rack_seen", status_code=status.HTTP_204_NO_CONTENT)
async def rack_seen(body: RackSeen, rack: RackUsageDep) -> None:
    """Natural: an upsert by serial, which never moves ``first_seen_at``."""
    printer = None if body.printer is None else str(body.printer)
    await rack.seen(printer, body.serials)


@router.post("/rack/usage", name="crud_print_rack_usage")
async def rack_usage(body: RackSerials, rack: RackUsageDep) -> dict[str, RackUsageRow]:
    """A read, sent as a ``POST`` so the serials stay out of the URL (spec §7)."""
    usage = await rack.usage(body.serials)
    return {
        serial: RackUsageRow.model_validate(dataclasses.asdict(used))
        for serial, used in usage.items()
    }


@router.get("/rack/picked", name="crud_print_rack_picked")
async def rack_picked(
    rack: RackUsageDep, id: Annotated[list[int], Query(default_factory=list)]
) -> list[int]:
    return sorted(await rack.picked_items(id))


@router.get("/rack/recorded-archives", name="crud_print_rack_recorded_archives")
async def rack_recorded_archives(
    rack: RackUsageDep, id: Annotated[list[int], Query(default_factory=list)]
) -> list[int]:
    return sorted(await rack.recorded_archives(id))


@router.post("/rack/picks", name="crud_print_rack_picks")
async def rack_picks(
    body: RackPicks, rack: RackUsageDep, idempotency_key: MaybeIdempotencyKey = None
) -> RackWritten:
    """A Bambuddy queue item's picks and their spools, in one transaction. ``ON CONFLICT
    DO NOTHING`` keeps the state, but the answer counts the rows written, so once per
    ``Idempotency-Key``."""
    printer = None if body.printer is None else str(body.printer)
    return await _rack_once(
        rack,
        idempotency_key,
        RACK_PICKS_ROUTE,
        body,
        lambda conn: RackUsageStore.record_picks_in(
            conn, body.queue_item_id, printer, body.picks, body.copies
        ),
    )


@router.post("/rack/prints", name="crud_print_rack_prints")
async def rack_prints(
    body: RackPrints, rack: RackUsageDep, idempotency_key: MaybeIdempotencyKey = None
) -> RackWritten:
    """A settled archive's print rows: once per ``Idempotency-Key``, as `rack_picks`."""
    return await _rack_once(
        rack,
        idempotency_key,
        RACK_PRINTS_ROUTE,
        body,
        lambda conn: RackUsageStore.record_prints_in(
            conn,
            body.archive_id,
            body.queue_item_id,
            body.settled_at,
            body.print_seconds,
            body.grams,
        ),
    )


@router.post("/rack/lan-picks", name="crud_print_rack_lan_picks")
async def rack_lan_picks(
    body: RackLanPicks, rack: RackUsageDep, idempotency_key: MaybeIdempotencyKey = None
) -> RackWritten:
    """A LAN start's picks: the earlier pick's rows deleted and the new ones inserted in
    one transaction on the API, with the ledger row (plans D5, D6). Once per
    ``Idempotency-Key``: a resend after a lost answer neither moves ``picked_at`` nor
    writes again."""
    printer = None if body.printer is None else str(body.printer)
    return await _rack_once(
        rack,
        idempotency_key,
        RACK_LAN_PICKS_ROUTE,
        body,
        lambda conn: RackUsageStore.record_lan_picks_in(
            conn, body.run_id, body.subtask_name, printer, body.picks
        ),
    )


@router.post("/rack/settle-lan", name="crud_print_rack_settle_lan")
async def rack_settle_lan(
    body: RackSettleLan, rack: RackUsageDep, idempotency_key: MaybeIdempotencyKey = None
) -> RackWritten:
    """A LAN run's open picks settled, as `RackUsageStore.settle_lan`: once per
    ``Idempotency-Key``."""
    return await _rack_once(
        rack,
        idempotency_key,
        RACK_SETTLE_LAN_ROUTE,
        body,
        lambda conn: RackUsageStore.settle_lan_in(conn, body.run_id, body.subtask_name, body.timed),
    )


@router.post("/rack/settle-lan-ended", name="crud_print_rack_settle_lan_ended")
async def rack_settle_lan_ended(
    body: RackSettleLanEnded, rack: RackUsageDep, idempotency_key: MaybeIdempotencyKey = None
) -> RackWritten:
    """A LAN start's open picks settled as its printer's report ends it, as
    `RackUsageStore.settle_lan_ended`: once per ``Idempotency-Key``."""
    return await _rack_once(
        rack,
        idempotency_key,
        RACK_SETTLE_LAN_ENDED_ROUTE,
        body,
        lambda conn: RackUsageStore.settle_lan_ended_in(
            conn, str(body.printer), body.subtask_name, body.ended_at, body.timed
        ),
    )
