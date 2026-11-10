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

`RemotePrinterStore` is the worker's `PrinterStore` with `ensure_bambuddy_written` over
the route (#5298 Task 29's first route, taken early by #4625): a merge it makes runs on
the API, which may write every table `PRINTER_REFERENCES` names. `link_bambuddy`, the
other method that merges, refuses: it runs on the API only. Its remaining methods stay on
the worker's SQL until Task 29.

`RemoteReprints` records the queue item ScadBuddy's reprint queued (#3741), so the Prints
page marks it; sent again, the first record stays.

`RemoteUploadStore` is `bambuddy/uploads.py` `BambuddyUploadStore` for the methods the
print worker calls: its uploads and slices (Q15), its slice starts (Q17), the print
dialog's background slices (Q16), the projects' printers, the projects without their
folder yet and the destinations' touches (Q18) over the routes, with the same answers. A
slice start and a background slice's record are each sent with an ``Idempotency-Key`` of
their own, which the client sends again on each retry: the route records each once. A
claim's key is `crud/render.py` `WriteKeys`'s: the same on a retried activity, so a
claim the last attempt won answers true again. The copy lock (Q19) stays a session lock
on the pool until the copy lease. The methods only the API's sweeps and routes call are
refused here (`API_ONLY`), so none reaches SQL from the worker by mistake.

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
import uuid
from collections import deque
from collections.abc import Callable, Collection, Iterable
from datetime import datetime, timedelta
from typing import Any, NoReturn
from urllib.parse import quote, urlencode

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.print_links import PrintReprint
from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.progress import PrintProgress
from scadbuddy.bambuddy.runs import PrintRun, PrintRunError
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.bambuddy.uploads import (
    BambuddyUploadStore,
    DestinationKind,
    LibraryCopy,
    PreviewSlice,
    ProjectTarget,
    SlicedCopy,
)
from scadbuddy.core.events import Event, PrintOutcome, Subscription
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.pg_events import CLOSE_TIMEOUT, DEFAULT_OUTBOX_SIZE, Outbox
from scadbuddy.crud.client import CrudClient, CrudRefusedError, CrudSyncClient
from scadbuddy.crud.guard import PREFIX
from scadbuddy.crud.idempotency import KEY_PROBLEM
from scadbuddy.crud.render import WriteKeys
from scadbuddy.library.settings_store import PrintSettings
from scadbuddy.printers.lan_models import LanStopped
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.store import PrinterStore

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


class RemotePrinterStore(PrinterStore):
    """Blocking: called through ``asyncio.to_thread``, as the SQL store is. The row is
    made or merged on the API, which announces a merge."""

    def __init__(self, client: CrudSyncClient, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client = client

    def ensure_bambuddy_written(
        self,
        bambuddy_printer_id: int,
        *,
        serial: str | None,
        name: str,
        model: str | None,
        listed: set[int] | frozenset[int] | None = None,
    ) -> tuple[PrinterRow, bool]:
        body = {
            "bambuddy_printer_id": bambuddy_printer_id,
            "serial": serial,
            "name": name,
            "model": model,
            "listed": None if listed is None else sorted(listed),
        }
        answer = self._client.request("POST", f"{BASE}/printers/ensure-bambuddy", json=body).json()
        return PrinterRow.model_validate(answer["row"]), bool(answer["written"])

    def link_bambuddy(self, printer_id: str, bambuddy_printer_id: int) -> PrinterRow:
        """The API's alone: its merge rewrites tables the worker's role cannot."""
        raise NotImplementedError("link_bambuddy runs on the API, not in a worker")


def _no_such_run(error: CrudRefusedError) -> bool:
    return error.status == 404 and error.problem.get("reason") == NO_SUCH_RUN


def _run(path: str) -> str:
    return f"{BASE}/runs/{quote(path, safe='')}"


class RemotePrintRunStore:
    #: The route prunes past the API's own setting (`PrintRuns.uses_retention`).
    uses_retention = False

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


#: The reprint record's retry budget and the timeout of each of its attempts (#5833).
#: The record is best effort and the item is queued already, so it does not take the
#: client's `UNAVAILABLE_BUDGET` (30 s) or its 10 s attempt timeout. The budget bounds
#: only when a new attempt may start, so an API out of reach holds the reprint's reply
#: for at most about two of these: the budget, plus one attempt that starts just before
#: it ends.
REPRINT_RECORD_BUDGET = 3.0


class RemoteReprints:
    """`print_links.ReprintRecorder` over the API's CRUD route (#3741, #5295): the queue
    item ScadBuddy's reprint queued, recorded for the archive's subject. Sent again, the
    first record stays (``ON CONFLICT (queue_item_id) DO NOTHING``). Each attempt times
    out after `REPRINT_RECORD_BUDGET`, and no attempt starts after that budget, so it
    waits at most about twice that for the API (#5833)."""

    def __init__(self, client: CrudClient) -> None:
        self._client = client

    async def record_reprint(self, subject: PrintSubject, reprint: PrintReprint) -> None:
        body = {"subject": subject.key, "reprint": reprint.model_dump(mode="json")}
        await self._client.request(
            "POST",
            f"{BASE}/reprints",
            json=body,
            timeout=REPRINT_RECORD_BUDGET,
            budget=REPRINT_RECORD_BUDGET,
        )


class ApiOnlyError(RuntimeError):
    """A `BambuddyUploadStore` method only the API calls, called on the print worker."""


#: The `BambuddyUploadStore` methods only the API's sweeps and routes call: the print
#: worker has no route for them (#5298).
API_ONLY = (
    "outputs_for_files",
    "native_slice_hashes",
    "relocate",
    "copies_by_use",
    "used_since",
    "slices_on",
    "recorded_as",
    "lost_starts",
    "finish_lost_start",
    "slice_jobs_after",
    "prune_slice_starts",
    "slice_jobs_recorded",
    "delete_outputs",
    "previews_to_sweep",
    "take_preview",
    "forget_preview",
    "project_targets",
    "forget_project_target",
    "forget_all_project_targets",
    "destination_touches",
)


class RemoteUploadStore(BambuddyUploadStore):
    """``pool`` serves only `copy_lock`, a session lock that the copy lease replaces
    (#5298 Task 27)."""

    def __init__(self, client: CrudClient, pool: ConnectionPool[Connection[DictRow]]) -> None:
        super().__init__(pool)
        self._client = client
        self._claims = WriteKeys()

    async def _get(self, path: str, query: Any) -> Any:
        url = f"{BASE}{path}?{urlencode(query, doseq=True)}"
        return (await self._client.request("GET", url)).json()

    async def _send(self, path: str, body: Any = None, method: str = "POST") -> None:
        await self._client.request(method, f"{BASE}{path}", json=body)

    async def for_output(self, output_id: str, *, native: bool = False) -> list[LibraryCopy]:
        return (await self.for_outputs([output_id], native=native))[output_id]

    async def for_outputs(
        self, output_ids: Iterable[str], *, native: bool = False
    ) -> dict[str, list[LibraryCopy]]:
        wanted = list(dict.fromkeys(output_ids))
        if not wanted:
            return {}
        body = await self._get("/uploads", {"output_id": wanted, "native": str(native).lower()})
        return {
            output_id: [LibraryCopy.model_validate(copy) for copy in copies]
            for output_id, copies in body.items()
        }

    async def record(self, output_id: str, copy: LibraryCopy) -> None:
        await self._send(
            "/uploads", {"output_id": output_id, "upload": copy.model_dump(mode="json")}
        )

    async def same_content(
        self, folder_id: int | None, target_key: str, content_key: str
    ) -> list[tuple[str, LibraryCopy]]:
        query: dict[str, Any] = {"target_key": target_key, "content_key": content_key}
        if folder_id is not None:
            query["folder_id"] = folder_id
        body = await self._get("/uploads/same-content", query)
        return [(row["output_id"], LibraryCopy.model_validate(row["upload"])) for row in body]

    async def shared(self, library_file_ids: Iterable[int], output_id: str) -> set[int]:
        ids = list(library_file_ids)
        if not ids:
            return set()
        return set(await self._get("/uploads/shared", {"id": ids, "output_id": output_id}))

    async def recorded(self, library_file_ids: Iterable[int]) -> set[int]:
        ids = list(library_file_ids)
        if not ids:
            return set()
        return set(await self._get("/uploads/recorded", {"id": ids}))

    async def recorded_files(self, library_file_ids: Iterable[int]) -> set[int]:
        ids = list(library_file_ids)
        if not ids:
            return set()
        return set(await self._get("/uploads/recorded-files", {"id": ids}))

    async def touch(self, output_id: str, library_file_id: int) -> None:
        await self._send(
            "/uploads/touch", {"output_id": output_id, "library_file_id": library_file_id}
        )

    async def forget(self, output_id: str, library_file_id: int) -> None:
        await self._send(
            "/uploads/forget", {"output_id": output_id, "library_file_id": library_file_id}
        )

    async def forget_files(self, library_file_ids: Iterable[int]) -> None:
        ids = list(library_file_ids)
        if ids:
            await self._send("/uploads/forget-files", {"ids": ids})

    async def sent_between(self, output_id: str) -> tuple[datetime, datetime] | None:
        body = await self._get("/uploads/sent-between", {"output_id": output_id})
        if body is None:
            return None
        first = datetime.fromisoformat(body["first_upload"])
        return first, datetime.fromisoformat(body["last_slice"])

    async def record_sliced(self, output_id: str, library_file_id: int, sliced: SlicedCopy) -> None:
        await self._send(
            "/slices",
            {
                "output_id": output_id,
                "library_file_id": library_file_id,
                "sliced": sliced.model_dump(mode="json"),
            },
        )

    async def slices_of(
        self, output_id: str, library_file_id: int, preset_key: str
    ) -> list[SlicedCopy]:
        body = await self._get(
            "/slices",
            {"output_id": output_id, "library_file_id": library_file_id, "preset_key": preset_key},
        )
        return [SlicedCopy.model_validate(row) for row in body]

    async def forget_slice(self, output_id: str, sliced_id: int) -> None:
        await self._send(f"/slices/{sliced_id}/forget?{urlencode({'output_id': output_id})}")

    async def forget_gone_slice(self, sliced_id: int) -> None:
        await self._send(f"/slices/{sliced_id}/gone")

    async def record_slice_hash(self, output_id: str, sliced_id: int, file_hash: str) -> None:
        await self._send(
            f"/slices/{sliced_id}/hash",
            {"output_id": output_id, "file_hash": file_hash},
            method="PUT",
        )

    async def record_slice_start(self, library_file_id: int, job_id: int | None) -> None:
        # A plain insert: one key per call, which the client sends again on each retry.
        await self._client.request(
            "POST",
            f"{BASE}/slice-starts",
            json={"library_file_id": library_file_id, "job_id": job_id},
            idempotency_key=uuid.uuid4().hex,
        )

    async def record_preview(self, preview: PreviewSlice) -> None:
        # A plain insert: one key per call, which the client sends again on each retry.
        await self._client.request(
            "POST",
            f"{BASE}/previews",
            json=preview.model_dump(mode="json"),
            idempotency_key=uuid.uuid4().hex,
        )

    async def preview(self, job_id: int, subject: str) -> PreviewSlice | None:
        body = await self._get("/previews/find", {"job_id": job_id, "subject": subject})
        return None if body is None else PreviewSlice.model_validate(body)

    async def previews_for(self, library_file_id: int, preset_key: str) -> list[PreviewSlice]:
        body = await self._get(
            "/previews", {"library_file_id": library_file_id, "preset_key": preset_key}
        )
        return [PreviewSlice.model_validate(row) for row in body]

    async def preview_sliced(self, row_id: int, sliced_file_id: int, name: str | None) -> None:
        await self._send(
            f"/previews/{row_id}/sliced",
            {"sliced_file_id": sliced_file_id, "name": name},
            method="PUT",
        )

    async def retire_preview(self, row_id: int) -> None:
        await self._send(f"/previews/{row_id}/retire")

    async def claim_preview(self, row_id: int) -> bool:
        url = f"{BASE}/previews/{row_id}/claim"
        try:
            response = await self._client.request("POST", url, idempotency_key=self._claims.next())
        except CrudRefusedError as error:
            if error.status != 422 or error.problem.get("type") != KEY_PROBLEM:
                raise
            # The last attempt of this activity claimed another slice under this
            # ordinal (`WriteKeys`): this claim is one of its own.
            response = await self._client.request("POST", url, idempotency_key=uuid.uuid4().hex)
        return bool(response.json()["claimed"])

    async def project_target(self, project_id: int) -> ProjectTarget | None:
        response = await self._client.request("GET", f"{BASE}/project-targets/{project_id}")
        body = response.json()
        return None if body is None else ProjectTarget.model_validate(body)

    async def remember_project_target(self, project_id: int, target: ProjectTarget) -> None:
        await self._send(
            f"/project-targets/{project_id}", target.model_dump(mode="json"), method="PUT"
        )

    async def folderless(self, name: str, parent_id: int | None) -> list[int]:
        query: dict[str, Any] = {"name": name}
        if parent_id is not None:
            query["parent_id"] = parent_id
        return [int(project_id) for project_id in await self._get("/folderless", query)]

    async def remember_folderless(self, project_id: int, name: str, parent_id: int | None) -> None:
        await self._send(
            f"/folderless/{project_id}", {"name": name, "parent_id": parent_id}, method="PUT"
        )

    async def forget_folderless(self, project_id: int) -> None:
        await self._send(f"/folderless/{project_id}", method="DELETE")

    async def touch_destination(self, kind: DestinationKind, target_id: int) -> None:
        await self._send("/destination-touches", {"kind": kind, "target_id": target_id})


def _api_only(name: str) -> Callable[..., NoReturn]:
    def refused(self: RemoteUploadStore, *args: Any, **kwargs: Any) -> NoReturn:
        raise ApiOnlyError(f"BambuddyUploadStore.{name} is the API's; the worker has no route")

    refused.__name__ = name
    return refused


for _name in API_ONLY:
    setattr(RemoteUploadStore, _name, _api_only(_name))


#: How long one event's post may take, retries included (`RemoteEvents._drain`).
EVENT_POST_BUDGET = 5.0

#: An event and the body that posts it, encoded once by `RemoteEvents.publish`.
Posted = tuple[Event, dict[str, Any]]


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
        self._early: deque[Posted] = deque()
        self._outbox: Outbox[Posted] | None = None
        self._outbox_dropped = 0
        self._closed = False
        self._drainer: asyncio.Task[None] | None = None

    async def start(self) -> None:
        outbox: Outbox[Posted] = Outbox(maxsize=self.outbox_size, loop=asyncio.get_running_loop())
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
        # Encoded once, here: the drain posts this body as it is.
        item = (event, {"event": event.model_dump(mode="json")})
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

    async def _drain(self, outbox: Outbox[Posted]) -> None:
        while (item := await outbox.get()) is not None:
            if outbox.dropped != self._outbox_dropped:
                lost, self._outbox_dropped = outbox.dropped - self._outbox_dropped, outbox.dropped
                logger.error("the event outbox overflowed", extra={"dropped": lost})
                self.metrics.events_dropped.labels("outbox_full").inc(lost)
            event, body = item
            try:
                # The event's own id makes a resend one row (`publish_once`).
                # A short budget: in an outage the drain drops this one and moves on,
                # rather than hold every queued event behind it.
                await self._client.request(
                    "POST",
                    f"{BASE}/events",
                    json=body,
                    timeout=EVENT_POST_BUDGET,
                    budget=EVENT_POST_BUDGET,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("could not publish an event", extra={"kind": event.kind})
                self.metrics.events_dropped.labels("error").inc()
                continue
            self.metrics.events_published.inc()
