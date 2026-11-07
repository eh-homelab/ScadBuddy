"""The print worker's stores over the CRUD routes (#5298). The routes are
`api/crud_print.py`.

`RemoteSettingsStore` is the print worker's `SettingsReader`: the settings read from the
API on every use, as `SettingsStore.load` reads them from the database. It has no pool:
nothing reads the settings tables through it.

`RemotePrintRunStore` is `bambuddy/runs.py` `PrintRunStore` (inventory Q10-Q13) over the
routes, with the same names, signatures and answers: a run the API does not keep is
`LookupError` from a write and ``None`` from a read. Every write is natural (plan D5),
so a call sent again after a lost answer answers as the first did. The insert's
``retention`` is not sent: the route prunes past the API's own setting.

`RemoteOutcomes` is `bambuddy/outcomes.py` `PrintOutcomeStore` (Q14): the follow's
`record`, `record_run` and `older_open`. A record sent again finds its run's outcome
recorded and answers None, as a second reader does over SQL.

`RemoteEvents` is the print worker's `EventBus` (Q5): `publish` hands an event to an
outbox, never blocks and is safe from any thread, as `PgNotifyEventBus.publish` is; a
task on the event loop posts each to the API, which logs and NOTIFYs it once by its id.
The worker does not LISTEN (plan D7), so it hears nothing: `subscribe` and
`add_listener` refuse.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Callable, Collection, Iterable
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.progress import PrintProgress
from scadbuddy.bambuddy.runs import PrintRun, PrintRunError
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.events import Event, PrintOutcome, Subscription
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.pg_events import CLOSE_TIMEOUT, DEFAULT_OUTBOX_SIZE, Outbox
from scadbuddy.crud.client import CrudClient, CrudRefusedError, CrudSyncClient
from scadbuddy.crud.guard import PREFIX
from scadbuddy.library.settings_store import PrintSettings
from scadbuddy.printers.lan_models import LanStopped

logger = logging.getLogger(__name__)

BASE = f"/api/v1{PREFIX}/print"
#: The route's reason for a run it does not keep (`api/crud_print.py` `NO_SUCH_RUN`).
NO_SUCH_RUN = "no-such-run"


class RemoteSettingsStore:
    """Blocking: called through ``asyncio.to_thread``, as the SQL store is."""

    def __init__(self, client: CrudSyncClient) -> None:
        self._client = client

    def load(self, timeout: float | None = None) -> PrintSettings:
        """``timeout`` bounds the whole read, retries included, as it bounds
        `SettingsStore.load`."""
        response = self._client.request("GET", f"{BASE}/settings", timeout=timeout, budget=timeout)
        return PrintSettings.model_validate(response.json())

    def plate_clear_required(self) -> bool:
        return self.load().lan_require_plate_clear

    def close(self) -> None:
        self._client.close()


def _no_such_run(error: CrudRefusedError) -> bool:
    return error.status == 404 and error.problem.get("reason") == NO_SUCH_RUN


def _run(path: str) -> str:
    return f"{BASE}/runs/{quote(path, safe='')}"


class RemotePrintRunStore:
    def __init__(self, client: CrudClient) -> None:
        self._client = client

    async def _read(self, path: str, query: Any = None) -> Any:
        url = f"{path}?{urlencode(query, doseq=True)}" if query else path
        return (await self._client.request("GET", url)).json()

    async def _write(self, run_id: str, action: str, body: Any = None) -> PrintRun:
        try:
            response = await self._client.request("POST", f"{_run(run_id)}/{action}", json=body)
        except CrudRefusedError as error:
            if _no_such_run(error):
                raise LookupError(f"there is no print run {run_id}") from error
            raise
        return PrintRun.model_validate(response.json())

    async def find(self, key: str, *, has_request_id: bool) -> PrintRun | None:
        body = await self._read(
            f"{BASE}/runs/find", {"key": key, "has_request_id": str(has_request_id).lower()}
        )
        return None if body is None else PrintRun.model_validate(body)

    async def get(self, run_id: str) -> PrintRun | None:
        try:
            body = await self._read(_run(run_id))
        except CrudRefusedError as error:
            if _no_such_run(error):
                return None
            raise
        return PrintRun.model_validate(body)

    async def get_many(self, run_ids: list[str]) -> dict[str, PrintRun]:
        if not run_ids:
            return {}
        body = await self._read(f"{BASE}/runs", {"ids": run_ids})
        return {run_id: PrintRun.model_validate(run) for run_id, run in body.items()}

    async def latest_for_output(self, output_id: str) -> PrintRun | None:
        body = await self._read(f"{BASE}/runs/latest", {"output_id": output_id})
        return None if body is None else PrintRun.model_validate(body)

    async def lan_waiting(self, printers: list[str]) -> dict[str, PrintRun]:
        if not printers:
            return {}
        body = await self._read(f"{BASE}/runs/lan-waiting", {"printers": printers})
        return {printer: PrintRun.model_validate(run) for printer, run in body.items()}

    async def running_for(self, output_id: str) -> list[PrintRun]:
        return (await self.running_for_outputs([output_id]))[output_id]

    async def running_for_outputs(self, output_ids: Iterable[str]) -> dict[str, list[PrintRun]]:
        wanted = list(dict.fromkeys(output_ids))
        if not wanted:
            return {}
        body = await self._read(f"{BASE}/runs/running", {"output_id": wanted})
        return {
            output_id: [PrintRun.model_validate(run) for run in runs]
            for output_id, runs in body.items()
        }

    async def insert_accepted(
        self,
        run_id: str,
        *,
        subject: PrintSubject,
        key: str,
        slug: str,
        workflow_id: str,
        workflow_run_id: str,
        retention: timedelta | None,
        printer: str | None = None,
    ) -> PrintRun:
        """``retention`` is not sent: the route prunes past the API's own setting."""
        body = {
            "run_id": run_id,
            "subject": subject.run_subject,
            "key": key,
            "slug": slug,
            "workflow_id": workflow_id,
            "workflow_run_id": workflow_run_id,
            "printer": printer,
        }
        response = await self._client.request("POST", f"{BASE}/runs", json=body)
        return PrintRun.model_validate(response.json())

    async def start_enqueue(self, run_id: str) -> None:
        await self._client.request("POST", f"{_run(run_id)}/start-enqueue")

    async def succeed(self, run_id: str, slug: str, result: PrintRunResult) -> PrintRun:
        return await self._write(
            run_id, "succeed", {"slug": slug, "result": result.model_dump(mode="json")}
        )

    async def update_result(self, run_id: str, slug: str, result: PrintRunResult) -> PrintRun:
        return await self._write(
            run_id, "result", {"slug": slug, "result": result.model_dump(mode="json")}
        )

    async def fail(
        self, run_id: str, slug: str, error: PrintRunError, *, unqueued: bool = False
    ) -> PrintRun:
        body = {"slug": slug, "error": error.model_dump(mode="json"), "unqueued": unqueued}
        return await self._write(run_id, "fail", body)

    async def fail_lost(self, run_id: str) -> PrintRun:
        return await self._write(run_id, "fail-lost")

    async def record_lan_stopped(self, run_id: str, stopped: LanStopped) -> PrintRun | None:
        try:
            return await self._write(run_id, "lan-stopped", stopped.model_dump(mode="json"))
        except LookupError:
            return None


class RemoteOutcomes:
    def __init__(self, client: CrudClient) -> None:
        self._client = client

    async def _record(
        self, subject: str, slug: str, progress: PrintProgress, run_id: str | None
    ) -> PrintOutcome | None:
        # As the SQL store: a read that is not settled records nothing, and asks nothing.
        if not progress.settled:
            return None
        body = {
            "subject": subject,
            "slug": slug,
            "progress": progress.model_dump(mode="json"),
            "run_id": run_id,
        }
        try:
            response = await self._client.request("POST", f"{BASE}/outcomes", json=body)
        except CrudRefusedError as error:
            # The run is no longer kept: there is nothing to record against.
            if _no_such_run(error):
                return None
            raise
        recorded = response.json()["outcome"]
        return None if recorded is None else PrintOutcome.model_validate(recorded)

    async def record(
        self, run_subject: str, slug: str, progress: PrintProgress
    ) -> PrintOutcome | None:
        return await self._record(run_subject, slug, progress, None)

    async def record_run(
        self, run: PrintRun, slug: str, progress: PrintProgress
    ) -> PrintOutcome | None:
        return await self._record(run.output_id, slug, progress, run.id)

    async def older_open(self, run_subject: str) -> list[PrintRun]:
        response = await self._client.request(
            "GET", f"{BASE}/outcomes/older-open?{urlencode({'subject': run_subject})}"
        )
        return [PrintRun.model_validate(run) for run in response.json()]


class WorkerHearsNoEventsError(RuntimeError):
    """The print worker does not LISTEN (#5298, plan D7)."""


class RemoteEvents:
    """See the module docstring. `start` on the event loop before the worker serves;
    what is published before it waits in a bounded buffer, as on `PgNotifyEventBus`."""

    def __init__(
        self,
        client: CrudClient,
        *,
        metrics: Metrics | None = None,
        outbox_size: int = DEFAULT_OUTBOX_SIZE,
    ) -> None:
        self._client = client
        self.metrics = metrics if metrics is not None else Metrics()
        self.outbox_size = outbox_size
        self._lock = threading.Lock()
        self._early: deque[tuple[Event, str]] = deque()
        self._outbox: Outbox | None = None
        self._outbox_dropped = 0
        self._closed = False
        self._drainer: asyncio.Task[None] | None = None

    async def start(self) -> None:
        outbox = Outbox(maxsize=self.outbox_size, loop=asyncio.get_running_loop())
        with self._lock:
            self._outbox = outbox
            early, self._early = list(self._early), deque()
        for item in early:
            outbox.offer(item)
        self._drainer = asyncio.create_task(self._drain(outbox))

    async def aclose(self) -> None:
        with self._lock:
            self._closed = True
            outbox = self._outbox
        if outbox is not None:
            outbox.close()  # the drainer posts what is queued, then ends
        if self._drainer is not None:
            try:
                await asyncio.wait_for(self._drainer, CLOSE_TIMEOUT)
            except (TimeoutError, asyncio.CancelledError):
                logger.warning("closed the event outbox with events still unsent")
            self._drainer = None

    def publish(self, event: Event) -> None:
        """Queue ``event`` for the API. Non-blocking and thread-safe; a failure to post
        it is logged and counted, never raised."""
        item = (event, event.model_dump_json())
        with self._lock:
            if self._closed:
                logger.warning(
                    "an event was published after the outbox closed", extra={"kind": event.kind}
                )
                return
            outbox = self._outbox
            if outbox is None:
                if len(self._early) >= self.outbox_size:
                    self._early.popleft()
                    self.metrics.events_dropped.labels("outbox_full").inc()
                self._early.append(item)
                return
        outbox.offer(item)

    def subscribe(
        self, *, kinds: Collection[str] | None = None, maxsize: int | None = None
    ) -> Subscription:
        raise WorkerHearsNoEventsError("the print worker does not LISTEN (#5298)")

    def add_listener(self, listener: Callable[[Event], None]) -> Callable[[], None]:
        raise WorkerHearsNoEventsError("the print worker does not LISTEN (#5298)")

    async def _drain(self, outbox: Outbox) -> None:
        while (item := await outbox.get()) is not None:
            if outbox.dropped != self._outbox_dropped:
                lost, self._outbox_dropped = outbox.dropped - self._outbox_dropped, outbox.dropped
                logger.error("the event outbox overflowed", extra={"dropped": lost})
                self.metrics.events_dropped.labels("outbox_full").inc(lost)
            event, _ = item
            try:
                # The event's own id makes a resend one row (`publish_once`).
                await self._client.request(
                    "POST", f"{BASE}/events", json={"event": event.model_dump(mode="json")}
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("could not publish an event", extra={"kind": event.kind})
                self.metrics.events_dropped.labels("error").inc()
                continue
            self.metrics.events_published.inc()
