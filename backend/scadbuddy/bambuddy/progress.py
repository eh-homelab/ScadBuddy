"""Following a print to its queue entries (#89).

A print leaves ScadBuddy by one route, slice then queue: a slice job finishes, and a
queue item per plate carries ``quantity``. The send bar's old pipeline route (#312)
is gone; a record left by it reads as never printed (``OutputMeta``).

**The fix that applies is derived from where it failed, not from the wording.** A
failure's own text is Bambuddy's and is shown verbatim; the suggested action is chosen
by which stage produced it, so it stays right when Bambuddy rewords a message.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import QueueItem, SliceJob
from scadbuddy.core.events import EventBus, PrintEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, PrintRoute

QUEUE_PATH = "/queue"

#: Normalised across both routes. ``unknown`` is a real state: Bambuddy's status
#: vocabularies differ per object and a new value must render as "still going" rather
#: than silently as "done", which would stop the polling on a print that is still live.
Stage = Literal["running", "queued", "done", "failed", "cancelled", "unknown"]

#: Bambuddy's own words for a finished state, per object. Anything outside these is
#: treated as still in flight.
_DONE = {"completed", "complete", "done", "finished", "success", "succeeded", "printed"}
_FAILED = {"failed", "error", "errored"}
_CANCELLED = {"cancelled", "canceled", "aborted"}
_QUEUED = {"queued", "pending", "waiting", "scheduled"}


def stage_of(status: str | None) -> Stage:
    """Map one of Bambuddy's status strings onto the shared vocabulary."""
    if not status:
        return "unknown"
    value = status.strip().lower()
    if value in _DONE:
        return "done"
    if value in _FAILED:
        return "failed"
    if value in _CANCELLED:
        return "cancelled"
    if value in _QUEUED:
        return "queued"
    if value in {"running", "printing", "in_progress", "slicing", "dispatching"}:
        return "running"
    return "unknown"


class CopyProgress(BaseModel):
    """One copy of a print: where it went and what it is doing.

    The queue route has one per plate, because Bambuddy's queue models repeats through
    ``quantity`` rather than through separate rows.
    """

    copy_index: int | None = None
    #: Which plate of an all-plates print this entry is (#200); ``None`` otherwise.
    plate_id: int | None = None
    printer_name: str | None = None
    queue_entry_id: int | None = None
    stage: Stage = "unknown"
    #: Bambuddy's own text, never paraphrased.
    message: str | None = None
    #: Why it is not printing *yet* — Bambuddy's ``waiting_reason``. Waiting is not
    #: failing, and showing it as an error turns every normal queue wait into one.
    waiting_reason: str | None = None


class PrintProgress(BaseModel):
    """What the send bar shows until every copy is queued, failed or cancelled."""

    route: PrintRoute
    stage: Stage = "unknown"
    #: True when nothing further will change without another print. Polling stops here.
    settled: bool = False
    slice_job_id: int | None = None
    queue_item_id: int | None = None
    copies: int = 1
    copies_completed: int = 0
    copies_failed: int = 0
    copies_cancelled: int = 0
    copies_in_progress: int = 0
    #: Bambuddy's own failure text, verbatim.
    error_message: str | None = None
    #: What to do about it, chosen by the stage that failed rather than by the wording.
    fix: str | None = None
    copies_detail: list[CopyProgress] = Field(default_factory=list)
    bambuddy_url: str


#: The fix per failing stage. Each is an action the user can actually take from the
#: dialog, and each is tied to *where* the failure happened, not to what it said.
SLICE_FIX = (
    "Bambuddy could not slice this plate. Change the plate or print settings, or fix the "
    "model, and print again."
)
QUEUED_THEN_FAILED_FIX = (
    "The queue entry was created and then refused. Check the filament mapping and the "
    "loaded spools, then retry it from Bambuddy's queue."
)


def from_queue(
    item: QueueItem | None,
    *,
    slice_job: SliceJob | None = None,
    slice_job_id: int | None = None,
    bambuddy_url: str,
) -> PrintProgress:
    """The slice-and-queue route, in the same shape.

    The slice is reported first because it is where this route actually fails: a queue
    item only exists once the plate has sliced, so an unfinished or failed slice is the
    whole state there is.
    """
    if slice_job is not None and slice_job.failure is not None:
        return PrintProgress(
            route="slice_queue",
            stage="failed",
            settled=True,
            slice_job_id=slice_job_id,
            error_message=slice_job.failure,
            fix=SLICE_FIX,
            bambuddy_url=bambuddy_url,
        )
    if item is None:
        running = slice_job is not None and not slice_job.finished
        return PrintProgress(
            route="slice_queue",
            stage="running" if running else "unknown",
            slice_job_id=slice_job_id,
            bambuddy_url=bambuddy_url,
        )

    stage = stage_of(item.status)
    copy = CopyProgress(
        printer_name=item.printer_name,
        queue_entry_id=item.id,
        stage=stage,
        message=item.error_message,
        waiting_reason=item.waiting_reason,
    )
    return PrintProgress(
        route="slice_queue",
        stage=stage,
        settled=stage in ("done", "failed", "cancelled"),
        slice_job_id=slice_job_id,
        queue_item_id=item.id,
        copies_completed=1 if stage == "done" else 0,
        copies_failed=1 if stage == "failed" else 0,
        copies_cancelled=1 if stage == "cancelled" else 0,
        copies_in_progress=1 if stage in ("queued", "running", "unknown") else 0,
        error_message=item.error_message,
        fix=QUEUED_THEN_FAILED_FIX if stage == "failed" else None,
        copies_detail=[copy],
        bambuddy_url=bambuddy_url,
    )


async def _queued_progress(
    client: BambuddyClient, slice_job_id: int | None, queue_item_id: int | None, url: str
) -> PrintProgress:
    """One slice job and the queue item it became, read off Bambuddy."""
    slice_job = None
    if slice_job_id is not None:
        slice_job = await client.slice_job(slice_job_id)
    item = None
    if queue_item_id is not None:
        try:
            item = await client.queue_item(queue_item_id)
        except ApiError as error:
            if error.status != 404:
                raise
            # Bambuddy drops a queue entry once it has been dispatched and archived;
            # that is not a failure, and reporting one would contradict the print
            # the user can see running.
            return PrintProgress(
                route="slice_queue",
                stage="done",
                settled=True,
                slice_job_id=slice_job_id,
                queue_item_id=queue_item_id,
                copies_completed=1,
                bambuddy_url=url,
            )
    return from_queue(item, slice_job=slice_job, slice_job_id=slice_job_id, bambuddy_url=url)


#: How far along an unsettled plate is; a slicing plate already reads as ``running``.
_UNSETTLED_RANK: dict[Stage, int] = {"unknown": 0, "queued": 1, "running": 2}


def from_plates(
    plates: list[PrintProgress],
    plate_ids: list[int],
    *,
    slice_job_id: int | None,
    queue_item_id: int | None,
    bambuddy_url: str,
) -> PrintProgress:
    """Every plate of an all-plates print as one progress (#200).

    Each plate is its own slice job and queue item, so each is read on its own and the
    counters summed. The print is settled only once every plate is, and the first
    failing plate supplies the error and the fix. Each plate's entries are tagged with
    its ``plate_id`` so the panel can say which plate is doing what.
    """
    detail: list[CopyProgress] = []
    for plate_id, plate in zip(plate_ids, plates, strict=True):
        copies = plate.copies_detail or [
            CopyProgress(queue_entry_id=plate.queue_item_id, stage=plate.stage)
        ]
        detail += [copy.model_copy(update={"plate_id": plate_id}) for copy in copies]
    failed = next((plate for plate in plates if plate.stage == "failed"), None)
    # Plates settle out of order across printers, so the print reads as its most advanced
    # unsettled plate: one running (or still slicing) outranks one waiting in the queue.
    unsettled = max(
        (plate for plate in plates if not plate.settled),
        key=lambda plate: _UNSETTLED_RANK.get(plate.stage, 0),
        default=None,
    )
    if failed is not None:
        stage: Stage = "failed"
    elif unsettled is not None:
        stage = unsettled.stage
    elif any(plate.stage == "cancelled" for plate in plates):
        # A cancelled plate did not print, so the print as a whole did not finish.
        stage = "cancelled"
    else:
        stage = "done"
    return PrintProgress(
        route="slice_queue",
        stage=stage,
        settled=unsettled is None,
        slice_job_id=slice_job_id,
        queue_item_id=queue_item_id,
        copies=len(plates),
        copies_completed=sum(plate.copies_completed for plate in plates),
        copies_failed=sum(plate.copies_failed for plate in plates),
        copies_cancelled=sum(plate.copies_cancelled for plate in plates),
        copies_in_progress=sum(plate.copies_in_progress for plate in plates),
        error_message=failed.error_message if failed else None,
        fix=failed.fix if failed else None,
        copies_detail=detail,
        bambuddy_url=bambuddy_url,
    )


async def progress_for(client: BambuddyClient, meta: OutputMeta) -> PrintProgress | None:
    """Read the progress of whatever this output last printed, or ``None``.

    ``None`` means the output has never been printed — not an error, and not something
    to retry.

    A read that 404s is reported as such rather than swallowed: an id ScadBuddy recorded
    and Bambuddy no longer has is a real thing to tell the user, not a blank panel.
    """
    route = meta.print_route
    if route is None and meta.queue_item_id is not None:
        # Records written before #89 carry no route; a queue item is this route's.
        route = "slice_queue"
    if route != "slice_queue":
        return None
    url = client.config.web_url(QUEUE_PATH)

    if len(meta.plates) > 1:
        # Polled together. A failing read cancels the other plates' reads, and the
        # caller sees that read's own error rather than an ExceptionGroup.
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(
                        _queued_progress(client, plate.slice_job_id, plate.queue_item_id, url)
                    )
                    for plate in meta.plates
                ]
        except ExceptionGroup as grouped:
            # The group is in completion order; the earliest failing plate is the
            # one reported, so the same failures always surface the same error.
            failures = [
                error
                for task in tasks
                if task.done() and not task.cancelled() and (error := task.exception()) is not None
            ]
            raise (failures or grouped.exceptions)[0] from None
        return from_plates(
            [task.result() for task in tasks],
            [plate.plate_id for plate in meta.plates],
            slice_job_id=meta.slice_job_id,
            queue_item_id=meta.queue_item_id,
            bambuddy_url=url,
        )
    return await _queued_progress(client, meta.slice_job_id, meta.queue_item_id, url)


#: Outputs whose last observed progress :class:`ProgressObserver` remembers.
OBSERVED_OUTPUTS = 256


class ProgressObserver:
    """Turns the progress reads the backend makes into ``print.*`` events.

    Until the per-print watcher (#268) exists, the only time the backend sees a
    print move is when someone asks: the progress route or a run. Each
    read is compared with the last one seen for that output, so a poll that finds
    nothing new publishes nothing, and ``print.settled`` is published once, on the
    read that first finds the print settled.
    """

    def __init__(self, events: EventBus | None, *, capacity: int = OBSERVED_OUTPUTS) -> None:
        self.events = events
        self.capacity = capacity
        #: Per output: the last progress seen, as JSON, and whether it was settled.
        self._seen: OrderedDict[str, tuple[str, bool]] = OrderedDict()
        self._lock = threading.Lock()

    def started(self, meta: OutputMeta) -> None:
        """A print of ``meta`` was just started: whatever was seen before is stale."""
        with self._lock:
            self._seen.pop(meta.id, None)
        emit(self.events, PrintEvent(kind="print.progress", output_id=meta.id, slug=meta.slug))

    def observe(self, meta: OutputMeta, progress: PrintProgress | None) -> None:
        if progress is None:
            return
        fingerprint = progress.model_dump_json()
        # Compare, decide and record under one hold of the lock, so two reads of the
        # same output racing each other (two tabs polling) cannot both decide they are
        # the first to see it settled. Only the publishing happens outside it.
        kinds: list[Literal["print.progress", "print.settled"]] = []
        with self._lock:
            previous = self._seen.pop(meta.id, None)
            self._seen[meta.id] = (fingerprint, progress.settled)
            while len(self._seen) > self.capacity:
                self._seen.popitem(last=False)
            if previous is None or previous[0] != fingerprint:
                kinds.append("print.progress")
                was_settled = previous is not None and previous[1]
                if progress.settled and not was_settled:
                    kinds.append("print.settled")
        for kind in kinds:
            emit(self.events, PrintEvent(kind=kind, output_id=meta.id, slug=meta.slug))
