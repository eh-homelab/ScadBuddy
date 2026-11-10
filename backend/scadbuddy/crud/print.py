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

`RemotePrinterStore` is `printers/store.py` `PrinterStore` (Q22-Q25, #5298 Task 29) for
the methods the print worker calls: the reads (`get`, `by_serial`, `by_bambuddy_id`,
`list`), the access code (`open_access_code`, opened on the API and answered to
``crud.print`` only, plan D8), a Bambuddy printer's row (`ensure_bambuddy_written`, taken
early by #4625: a merge it makes runs on the API, which may write every table
`PRINTER_REFERENCES` names) and a virtual-printer write's sync (`upsert_virtual`,
`mark_virtual_listed`, from `bambuddy/virtual_printers.py`). Those two answer what the
write changed, which a resend would not repeat, so each takes an ``Idempotency-Key``
of its own per call, sent again on that call's retries. `RemoteReports` is
`printers/reports.py` `PrinterReportStore` (Q26), for the watch, the native follow and
the LAN path. Neither holds a pool, and the store
refuses the methods only the API calls (`PRINTERS_API_ONLY`), `link_bambuddy` among them.

`RemoteLanImports` records a LAN-only printer's job the print watcher read (#4699 phase
4); sent again, the first record answers.

`RemoteRackUsage` is `rack/usage.py` `RackUsageStore` (Q27, #5298 Task 30) for the
methods the print worker calls: the hotends a run sees, their usage, a Bambuddy run's
picks, the follow's settle of its archives, a LAN start's picks and the settles of a LAN
start, from the follow (`lan_settle_hook`), ``lan_wait`` and the printer's watch. It holds
no pool. A LAN start's picks replace an earlier pick of that start (deletes and inserts),
so they are one route, one transaction on the API (plan D6). ``seen`` is natural. Every
other write answers the rows it wrote, which a resend would not repeat, so each takes an
``Idempotency-Key``. A queue item's picks and a run's settle (`settle_lan`, whose body a
retried attempt sends again unchanged) take `WriteKeys` ``rack`` inside an activity: a
retried attempt's same write answers as the lost one did, and one whose body differs is
refused 422 and sent again under a new key, as `RemoteBlobIndex` does. The others take a
new UUID per call, which the client sends again on its retries: a LAN start's picks,
which a retried attempt ranks again (the newer pick must replace the earlier one, never
replay it), a settled archive's rows, whose body carries the settle's ``now()``, and the
watch's `settle_lan_ended`, whose one long activity repeats `WriteKeys`' keys across
attempts. The watch's call is bounded by `WATCH_CALL_BUDGET`. ``spool_history`` is the
API's (the rack usage route).

`RemoteSpoolStore`, `RemoteNativeSpools`, `RemoteHeldPresets` and `RemoteNativeSlices`
are `spools/store.py` `SpoolStore`, `spools/native.py` `NativeSpoolStore`,
`slicing/held_presets.py` `HeldPresetStore` and `slicing/store.py` `NativeSliceStore`
(Q28-Q30, #5298 Task 31) for the methods the print worker calls: a native run's overlay
and the native provider's recache after a print (``overlay``, ``replace_records``,
``failed``), the shelf, a LAN start's uses and their settle (``list``, ``any``,
``record_uses``, ``settle_uses``), the kept presets a native run reads (``rows``,
``documents``) and a native slice's ``start``. None holds a pool, and none is called in
the printer watch. Each SQL write is one transaction, under the spools' advisory lock,
the uses' row locks or the slice key's lock, so each is one route run whole on the API
(plan D6). ``failed``, ``record_uses`` and ``start`` are natural (an upsert, ``ON CONFLICT
DO NOTHING``, and the key's newest row). ``replace_records`` and ``settle_uses`` take an
``Idempotency-Key`` of their own, a new UUID per call, sent again on the client's
retries: a refresh's body carries its time, and a settle runs in the follow's one long
activity. The refresh, the tray links and the shelf's edits are the API's.

`RemoteReprints` records the queue item ScadBuddy's reprint queued (#3741), so the Prints
page marks it; sent again, the first record stays.

`RemoteUploadStore` is `bambuddy/uploads.py` `BambuddyUploadStore` for the methods the
print worker calls: its uploads and slices (Q15), its slice starts (Q17), the print
dialog's background slices (Q16), the projects' printers, the projects without their
folder yet and the destinations' touches (Q18) over the routes, with the same answers. A
slice start and a background slice's record are each sent with an ``Idempotency-Key`` of
their own, which the client sends again on each retry: the route records each once. A
claim's key is `crud/render.py` `WriteKeys`'s: the same on a retried activity, so a
claim the last attempt won answers true again. The copy lock (Q19) is the copy lease
over ``…/copy-leases/{key}`` (`RemoteCopyLeases`, #5298 Task 27), and a record made
under it names the lease, which the route checks: `LeaseLostError` once another holder
took it. So the store holds no pool: nothing it does reaches SQL from the worker. The
methods only the API's sweeps and routes call are refused here (`API_ONLY`), and so is
``pool``.

`RemotePrintLinks` is `bambuddy/print_links.py` `PrintLinkStore` (Q20) for the methods
the print worker calls: the links its progress reads, operations and settle hook make
and read, the sends a run records, and a library file's links and gone items, over the
routes with the same answers. `RemoteOutputPrints` is `library/output_prints.py`
`OutputPrintStore` (Q21): an output's last print, as a run records it and a LAN print's
follow moves it. Every write of both is natural (plan D5). Neither holds a pool, and
each refuses the methods only the API calls (`LINKS_API_ONLY`, `OUTPUT_PRINTS_API_ONLY`).
``pending_library`` is among them: only the prints list's library linking pass
(`linking.link_library_prints`, on the API) claims those rows.

`RemoteEvents` is the print worker's `EventBus` (Q5): `publish` hands an event to an
outbox, never blocks and is safe from any thread, as `PgNotifyEventBus.publish` is; a
task on the event loop posts each to the API, which logs and NOTIFYs it once by its id.
The worker does not LISTEN (plan D7), so it hears nothing: `subscribe` and
`add_listener` refuse.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from collections import deque
from collections.abc import (
    AsyncIterator,
    Callable,
    Collection,
    Iterable,
    Mapping,
    Sequence,
)
from datetime import datetime, timedelta
from typing import Any, NoReturn
from urllib.parse import quote, urlencode
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.copy_leases import CopyLease, LeaseLostError, hold_lease
from scadbuddy.bambuddy.models import LocalPreset, LocalPresetDetail
from scadbuddy.bambuddy.print_links import (
    LinkedPrint,
    PrintLink,
    PrintLinkStore,
    PrintReprint,
    PrintSend,
)
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
from scadbuddy.core.events import Event, PrintOutcome, Subscription
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.pg_events import CLOSE_TIMEOUT, DEFAULT_OUTBOX_SIZE, Outbox
from scadbuddy.core.secrets import Kek, SealError
from scadbuddy.crud.client import CrudClient, CrudRefusedError, CrudSyncClient
from scadbuddy.crud.guard import PREFIX
from scadbuddy.crud.idempotency import KEY_PROBLEM
from scadbuddy.crud.render import WriteKeys
from scadbuddy.crud.wire import NOT_CONFIGURED
from scadbuddy.library.output_prints import OutputPrintStore
from scadbuddy.library.outputs import LanPlateSend, PlateSend
from scadbuddy.library.settings_store import PrintSettings
from scadbuddy.print_watch.lan import LanCapture, LanImport
from scadbuddy.printers.lan_models import LanStopped
from scadbuddy.printers.models import BambuddyView, PrinterRow
from scadbuddy.printers.reports import Advance, PrintEnd, PrinterReportStore
from scadbuddy.printers.store import (
    KekMissingError,
    NoAccessCodeError,
    PrinterNotFoundError,
    PrinterStore,
    SerialTakenError,
    VirtualOutcome,
    printer_uuid,
)
from scadbuddy.rack.rank import Usage
from scadbuddy.rack.usage import PickedHotend, RackUsageStore
from scadbuddy.slicing.held_presets import HeldPresetStore, HeldRow
from scadbuddy.slicing.store import NativeSlice, NativeSliceStore, SliceSubjectError
from scadbuddy.spools.models import Overlay, SeenTray, SpoolSnapshot
from scadbuddy.spools.native import NativeSpool, NativeSpoolStore, SpoolUse
from scadbuddy.spools.store import SpoolStore

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


class RemoteLanImports:
    """`print_watch.lan.LanRecorder` over the API's CRUD route (#4699 phase 4, #5295): the
    API matches a model and writes the record. Natural on the job, so a resend after a
    lost answer answers the first record."""

    def __init__(self, client: CrudClient) -> None:
        self._client = client

    async def record(self, capture: LanCapture) -> LanImport:
        answer = await self._client.request(
            "POST", f"{BASE}/lan-imports", json=capture.model_dump(mode="json")
        )
        return LanImport.model_validate(answer.json())


class ApiOnlyError(RuntimeError):
    """A store method only the API calls, called on the print worker."""


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
    # The import routes' link pass for one archive (#4698, `linking.link_archive`).
    "slice_owners",
    "unhashed_near",
)


#: The routes' reasons: a lease another holder keeps, and a lease its token no longer
#: holds (`api/crud_print.py`).
LEASE_BUSY = "busy"
LEASE_LOST = "lease-lost"
#: How much longer than its wait an acquire's request may take, in seconds.
LEASE_REQUEST_SLACK = 10.0


def _lease_url(key: str) -> str:
    return f"{BASE}/copy-leases/{quote(key, safe='')}"


def _refused_as(error: CrudRefusedError, reason: str) -> bool:
    return error.status == 409 and error.problem.get("reason") == reason


class RemoteCopyLeases:
    """`copy_leases.py` `LeaseCalls` over the routes. Each call is natural on its
    token, so the client sends it again after a lost answer. An acquire waits on the
    API, so its request may take ``wait`` plus `LEASE_REQUEST_SLACK`."""

    def __init__(self, client: CrudClient) -> None:
        self._client = client

    async def acquire(self, key: str, token: str, ttl: float, wait: float) -> datetime | None:
        try:
            response = await self._client.request(
                "POST",
                _lease_url(key),
                json={"token": token, "ttl_s": ttl, "wait_s": wait},
                timeout=wait + LEASE_REQUEST_SLACK,
            )
        except CrudRefusedError as error:
            if _refused_as(error, LEASE_BUSY):
                return None
            raise
        return datetime.fromisoformat(response.json()["expires_at"])

    async def renew(self, key: str, token: str, ttl: float) -> datetime | None:
        try:
            response = await self._client.request(
                "PUT", _lease_url(key), json={"token": token, "ttl_s": ttl}
            )
        except CrudRefusedError as error:
            if _refused_as(error, LEASE_LOST):
                return None
            raise
        return datetime.fromisoformat(response.json()["expires_at"])

    async def release(self, key: str, token: str) -> None:
        await self._client.request("DELETE", f"{_lease_url(key)}?{urlencode({'token': token})}")


class RemoteUploadStore(BambuddyUploadStore):
    """No pool: every method the worker calls is a route, the copy lock the copy
    lease (#5298 Task 27)."""

    def __init__(self, client: CrudClient) -> None:
        self._client = client
        self._claims = WriteKeys("previews")
        self._leases = RemoteCopyLeases(client)

    @property
    def pool(self) -> ConnectionPool[Connection[DictRow]]:
        raise ApiOnlyError("BambuddyUploadStore.pool is the API's; the worker has none")

    @contextlib.asynccontextmanager
    async def copy_lock(self, key: str) -> AsyncIterator[CopyLease]:
        async with hold_lease(self._leases, key) as lease:
            yield lease

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

    async def record(
        self, output_id: str, copy: LibraryCopy, lease: CopyLease | None = None
    ) -> None:
        body: dict[str, Any] = {"output_id": output_id, "upload": copy.model_dump(mode="json")}
        if lease is not None:
            body["lease"] = lease.model_dump()
        try:
            await self._send("/uploads", body)
        except CrudRefusedError as error:
            if _refused_as(error, LEASE_LOST):
                raise LeaseLostError(str(error)) from error
            raise

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


def _api_only(store: str, name: str) -> Callable[..., NoReturn]:
    def refused(self: Any, *args: Any, **kwargs: Any) -> NoReturn:
        raise ApiOnlyError(f"{store}.{name} is the API's; the worker has no route")

    refused.__name__ = name
    return refused


def _refuse(cls: type, store: str, names: Iterable[str]) -> None:
    for name in names:
        setattr(cls, name, _api_only(store, name))


_refuse(RemoteUploadStore, "BambuddyUploadStore", API_ONLY)


#: The `PrintLinkStore` methods only the API calls: the prints list and detail
#: (`api/print_history.py`, its library linking pass `linking.link_library_prints`
#: among them), the jobs' badges (`api/printing.py`, `api/farm.py`), an output's
#: delete, and Import print (#4698: `api/print_imports.py`, the farm's unlinked marks, a
#: print's import record and a model's delete). The print worker has no route for them
#: (#5298 Task 28).
LINKS_API_ONLY = (
    "page",
    "delete_outputs",
    "send",
    "lan_start",
    "record_lan_start",
    "jobs",
    "pending_library",
    "delete_models",
    "record_import",
    "linked_among",
    "import_of",
    "import_lock",
)
#: The `PrintLinkStore` methods that only call others of its own, each of which the
#: worker sends over a route: ``output_for`` (``linked``), ``linked_queue_items``
#: (``for_subject``) and ``last_run`` (``sends_for``).
LINKS_COMPOSED = ("output_for", "linked_queue_items", "last_run")


class RemotePrintLinks(PrintLinkStore):
    """No pool: every method the worker calls is a route under ``…/print/links`` or
    ``…/print/sends`` (area ``links``); the reprint's record is `RemoteReprints`'
    route (area ``reprints``)."""

    def __init__(self, client: CrudClient) -> None:
        self._client = client
        self._reprints = RemoteReprints(client)

    async def _read(self, path: str, query: Any = None) -> Any:
        url = f"{BASE}{path}?{urlencode(query, doseq=True)}" if query else f"{BASE}{path}"
        return (await self._client.request("GET", url)).json()

    async def _post(self, path: str, body: Any = None) -> None:
        await self._client.request("POST", f"{BASE}{path}", json=body)

    async def record(self, subject: PrintSubject, link: PrintLink) -> None:
        await self._post("/links", {"subject": subject.key, "link": link.model_dump(mode="json")})

    async def for_subject(self, subject: PrintSubject) -> list[PrintLink]:
        body = await self._read("/links", {"subject": subject.key})
        return [PrintLink.model_validate(row) for row in body]

    async def linked(self, archive_id: int) -> LinkedPrint | None:
        body = await self._read(f"/links/{archive_id}")
        return None if body is None else LinkedPrint.model_validate(body)

    async def record_sends(self, subject: PrintSubject, sends: Sequence[PrintSend]) -> None:
        if not sends:
            return
        await self._post(
            "/sends",
            {"subject": subject.key, "sends": [send.model_dump(mode="json") for send in sends]},
        )

    async def record_reprint(self, subject: PrintSubject, reprint: PrintReprint) -> None:
        await self._reprints.record_reprint(subject, reprint)

    async def sends_for(self, subject: PrintSubject) -> list[PrintSend]:
        body = await self._read("/sends", {"subject": subject.key})
        return [PrintSend.model_validate(row) for row in body]

    async def run_sends(self, subject: PrintSubject, run_id: str) -> list[PrintSend]:
        body = await self._read("/sends", {"subject": subject.key, "run_id": run_id})
        return [PrintSend.model_validate(row) for row in body]

    async def link_library(self, queue_item_id: int, archive_id: int, name: str | None) -> None:
        await self._post(
            f"/sends/{queue_item_id}/library-link", {"archive_id": archive_id, "name": name}
        )

    async def library_gone(self, queue_item_id: int) -> None:
        await self._post(f"/sends/{queue_item_id}/library-gone")


_refuse(RemotePrintLinks, "PrintLinkStore", LINKS_API_ONLY)


#: The `OutputPrintStore` methods only the API calls: `OutputStore`'s overlay of the
#: rows and an output's delete (#5298 Task 28).
OUTPUT_PRINTS_API_ONLY = ("for_outputs", "delete")


def _last_print(output_id: str) -> str:
    return f"{BASE}/last-prints/{quote(output_id, safe='')}"


class RemoteOutputPrints(OutputPrintStore):
    """No pool: each write is a ``PUT`` under ``…/print/last-prints/{output_id}`` (area
    ``links``), set by the output's key. Blocking, as the SQL store is: callers on the
    loop use a thread."""

    def __init__(self, client: CrudSyncClient) -> None:
        self._client = client

    def record(
        self,
        output_id: str,
        *,
        queue_item_id: int | None,
        slice_job_id: int | None,
        project_id: int | None,
        plates: list[PlateSend],
    ) -> None:
        body = {
            "queue_item_id": queue_item_id,
            "slice_job_id": slice_job_id,
            "project_id": project_id,
            "plates": [plate.model_dump(mode="json") for plate in plates],
        }
        self._client.request("PUT", _last_print(output_id), json=body)

    def record_lan(
        self,
        output_id: str,
        *,
        run_id: str,
        printer: str | None,
        plates: list[LanPlateSend],
    ) -> None:
        body = {
            "run_id": run_id,
            "printer": printer,
            "plates": [plate.model_dump(mode="json") for plate in plates],
        }
        self._client.request("PUT", f"{_last_print(output_id)}/lan", json=body)

    def record_lan_progress(
        self, output_id: str, run_id: str, *, stage: Stage, settled: bool
    ) -> None:
        body = {"run_id": run_id, "stage": stage, "settled": settled}
        self._client.request("PUT", f"{_last_print(output_id)}/lan-progress", json=body)


_refuse(RemoteOutputPrints, "OutputPrintStore", OUTPUT_PRINTS_API_ONLY)


#: The routes' reasons for an access code it cannot answer (`api/crud_print.py`): no
#: printer, a printer with no code, and a code sealed under another key.
NO_SUCH_PRINTER = "no-such-printer"
NO_ACCESS_CODE = "no-access-code"
ACCESS_CODE_UNREADABLE = "access-code-unreadable"
#: The route's reason for a virtual printer whose serial another row keeps.
SERIAL_TAKEN = "serial-taken"
#: What a worker's `KekMissingError` says when the API cannot open an access code.
API_NEEDS_KEK = "LAN printing needs SCADBUDDY_SECRET_KEY_FILE on the API"


class RemotePrinterStore(PrinterStore):
    """Blocking: called through ``asyncio.to_thread``, as the SQL store is. No pool:
    every method the worker calls is a route under ``…/print/printers`` (areas
    ``printer-ensure`` and ``printers``). A row is made or merged on the API, which
    announces a merge. ``kek`` is the worker's own: the API opens the access code (plan
    D8), but the worker still needs its KEK for a virtual printer's sealed request
    (`bambuddy/virtual_printers.py`) and a LAN print's transfer tokens (`RemoteBlobs`),
    so `bambuddy/print_run.py` `_registry_path` still reads it."""

    def __init__(
        self, client: CrudSyncClient, kek: Kek | None, *, previous_kek: Kek | None = None
    ) -> None:
        self._client = client
        self.kek = kek
        self.previous_kek = previous_kek
        #: The API announces a merge; the worker announces nothing of the registry's.
        self.events = None

    @property
    def pool(self) -> ConnectionPool[Connection[DictRow]]:
        raise ApiOnlyError("PrinterStore.pool is the API's; the worker has none")

    def _row_or_none(self, path: str, timeout: float | None = None) -> PrinterRow | None:
        body = self._client.request(
            "GET", f"{BASE}/printers{path}", timeout=timeout, budget=timeout
        ).json()
        return None if body is None else PrinterRow.model_validate(body)

    def get(self, printer_id: str, timeout: float | None = None) -> PrinterRow | None:
        """``timeout`` bounds the whole read, retries included, as it bounds the SQL
        store's (the print watcher's nudge, #6037)."""
        if printer_uuid(printer_id) is None:
            return None
        return self._row_or_none(f"/{quote(printer_id, safe='')}", timeout)

    def by_serial(self, serial: str) -> PrinterRow | None:
        return self._row_or_none(f"/by-serial?{urlencode({'serial': serial})}")

    def by_bambuddy_id(self, bambuddy_printer_id: int) -> PrinterRow | None:
        return self._row_or_none(f"/by-bambuddy-id/{int(bambuddy_printer_id)}")

    def list(self) -> list[PrinterRow]:
        body = self._client.request("GET", f"{BASE}/printers").json()
        return [PrinterRow.model_validate(row) for row in body]

    def open_access_code(self, printer_id: str) -> str:
        """The access code, opened on the API (plan D8). Each refusal is raised as the
        SQL store raises it; no exception here carries the code."""
        if printer_uuid(printer_id) is None:
            raise PrinterNotFoundError(printer_id)
        url = f"{BASE}/printers/{quote(printer_id, safe='')}/access-code"
        try:
            response = self._client.request("GET", url)
        except CrudRefusedError as error:
            reason = error.problem.get("reason")
            if error.status == 404 and reason == NO_ACCESS_CODE:
                raise NoAccessCodeError(printer_id) from None
            if error.status == 404 and reason == NO_SUCH_PRINTER:
                raise PrinterNotFoundError(printer_id) from None
            if reason == NOT_CONFIGURED:
                raise KekMissingError(API_NEEDS_KEK) from None
            if _refused_as(error, ACCESS_CODE_UNREADABLE):
                raise SealError("the access code was sealed under another key") from None
            raise
        return str(response.json()["code"])

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

    def _keyed(self, method: str, path: str, body: Any) -> Any:
        """A write whose answer a resend would not repeat, under an ``Idempotency-Key``
        (plan D5) of its own: a new UUID per call, which the client sends again on each
        retry of that call, as `RemoteUploadStore.record_slice_start` does. Not
        `WriteKeys`': an upsert's body carries the view's fresh ``seen_at``, so a retried
        activity's key would almost never replay; and a replayed listed mark would skip a
        write the registry may need again after a change made between two attempts."""
        response = self._client.request(
            method, f"{BASE}{path}", json=body, idempotency_key=uuid.uuid4().hex
        )
        return response.json()

    def upsert_virtual(
        self,
        vp_id: int,
        *,
        serial: str | None,
        name: str,
        model: str | None,
        mode: str,
        forwards_to: str | None,
        view: BambuddyView,
        may_adopt: bool = True,
    ) -> tuple[PrinterRow, VirtualOutcome, bool]:
        body = {
            "serial": serial,
            "name": name,
            "model": model,
            "mode": mode,
            "forwards_to": forwards_to,
            "view": view.model_dump(mode="json"),
            "may_adopt": may_adopt,
        }
        try:
            answer = self._keyed("PUT", f"/printers/virtual/{int(vp_id)}", body)
        except CrudRefusedError as error:
            if _refused_as(error, SERIAL_TAKEN):
                raise SerialTakenError(str(error.problem.get("existing_id", ""))) from error
            raise
        row = PrinterRow.model_validate(answer["row"])
        return row, answer["outcome"], bool(answer["changed"])

    def mark_virtual_listed(self, listed: Iterable[int]) -> tuple[str, ...]:
        answer = self._keyed("POST", "/printers/virtual-listed", {"listed": sorted(set(listed))})
        return tuple(str(printer_id) for printer_id in answer["changed"])


#: The `PrinterStore` methods only the API calls: Settings > Printers' routes, the
#: Bambuddy sync (`printers/bambuddy_sync.py` `sync_printers`, from the API's route and
#: housekeeping), the strip (`bambuddy/printers_now.py`) and the LAN link. The print
#: worker has no route for them (#5298 Task 29). ``link_bambuddy`` merges rows, as
#: ``ensure_bambuddy_written`` may, so it runs with the API's privileges only.
PRINTERS_API_ONLY = (
    "create",
    "patch",
    "link_bambuddy",
    "mark_bambuddy_active",
    "unlink_bambuddy",
    "link_lan",
    "unlink_lan",
    "create_linked",
    "record_bambuddy_view",
    "set_lan_sync_error",
    "delete",
)
#: The `PrinterStore` methods that only call others of its own, each of which the worker
#: sends over a route: ``require`` (``get``) and ``ensure_bambuddy``
#: (``ensure_bambuddy_written``).
PRINTERS_COMPOSED = ("require", "ensure_bambuddy")

_refuse(RemotePrinterStore, "PrinterStore", PRINTERS_API_ONLY)


#: The retry budget of each CRUD call the printer watch makes between two heartbeats (a
#: report's `put` or `touch`, a start's `settle_lan_ended`), and each try's timeout. A
#: call ends within about their sum (a try started just before the budget ends runs its
#: timeout), so a report's write and its settle take at most about 8 s, well inside the
#: watch's 30 s heartbeat timeout (`workflows/printer_watch.py` `WATCH_HEARTBEAT`) with
#: its 10 s quiet wait (`HEARD_EVERY`). Each is advisory there: a report not written is
#: written at the next quiet tick, and a missed settle is closed by ``lan_wait``, the
#: run's follow or the pick's age.
WATCH_CALL_BUDGET = 2.0
WATCH_CALL_TIMEOUT = 2.0


def _report(printer_id: str) -> str:
    return f"{BASE}/reports/{quote(printer_id, safe='')}"


class RemoteReports(PrinterReportStore):
    """`printers/reports.py` `PrinterReportStore` (Q26) over ``…/print/reports`` (area
    ``printers``). Blocking, as the SQL store is: callers on the loop use a thread. No
    pool. An `advance` (and `put`, its end alone) answers the print that ended and the
    print that started on that write only, so each is sent with an ``Idempotency-Key``
    of its own, a new UUID, which the client sends again on each retry: the route
    answers a resend the first write's end and start. The watch writes from one
    loop for as long as its connection lasts, so an activity's ordinal would not name
    one write. `touch` is natural: it never moves ``seen_at`` back. Only the watch writes
    (`advance`, `touch`), between two of its heartbeats, so each write is bounded by
    `WATCH_CALL_BUDGET` and `WATCH_CALL_TIMEOUT`, not the client's own budget."""

    def __init__(self, client: CrudSyncClient) -> None:
        self._client = client

    @property
    def pool(self) -> ConnectionPool[Connection[DictRow]]:
        raise ApiOnlyError("PrinterReportStore.pool is the API's; the worker has none")

    def put(
        self,
        printer_id: str,
        report: dict[str, Any],
        seen_at: datetime,
        *,
        idempotency_key: str | None = None,
    ) -> PrintEnd | None:
        return self.advance(printer_id, report, seen_at, idempotency_key=idempotency_key).ended

    def advance(
        self,
        printer_id: str,
        report: dict[str, Any],
        seen_at: datetime,
        *,
        idempotency_key: str | None = None,
    ) -> Advance:
        """``idempotency_key`` names the write across calls: the watch sends a report whose
        answer was lost again under the same key, so the route answers the end and the
        start the first write showed (`printers/watch.py`). None is a new key."""
        if printer_uuid(printer_id) is None:
            return Advance()
        answer = self._client.request(
            "PUT",
            _report(printer_id),
            json={"report": report, "seen_at": seen_at.isoformat()},
            idempotency_key=idempotency_key or uuid.uuid4().hex,
            timeout=WATCH_CALL_TIMEOUT,
            budget=WATCH_CALL_BUDGET,
        ).json()
        ended = answer["ended"]
        return Advance(
            ended=None if ended is None else PrintEnd(**ended), started=answer.get("started")
        )

    def touch(self, printer_id: str, seen_at: datetime) -> None:
        if printer_uuid(printer_id) is None:
            return
        self._client.request(
            "POST",
            f"{_report(printer_id)}/touch",
            json={"seen_at": seen_at.isoformat()},
            timeout=WATCH_CALL_TIMEOUT,
            budget=WATCH_CALL_BUDGET,
        )

    def get(self, printer_id: str) -> tuple[dict[str, Any], datetime] | None:
        if printer_uuid(printer_id) is None:
            return None
        body = self._client.request("GET", _report(printer_id)).json()
        return None if body is None else (body["report"], datetime.fromisoformat(body["seen_at"]))

    def all(self) -> dict[str, tuple[dict[str, Any], datetime]]:
        body = self._client.request("GET", f"{BASE}/reports").json()
        return {
            printer_id: (seen["report"], datetime.fromisoformat(seen["seen_at"]))
            for printer_id, seen in body.items()
        }


#: The `RackUsageStore` methods only the API calls: the rack usage route's spool history
#: (`api/printing.py`), and the ``*_in`` writes, which run on the API's connection.
RACK_API_ONLY = (
    "spool_history",
    "record_picks_in",
    "record_prints_in",
    "record_lan_picks_in",
    "settle_lan_in",
    "settle_lan_ended_in",
)


def _unique(values: Iterable[Any]) -> list[Any]:
    return sorted({value for value in values if value})


class RemoteRackUsage(RackUsageStore):
    """`RackUsageStore` (Q27) over ``…/print/rack`` (area ``rack``, #5298 Task 30). Async,
    as the SQL store is. No pool and no migration: the API owns the tables."""

    def __init__(self, client: CrudClient) -> None:
        self._client = client
        self._keys = WriteKeys("rack")

    @property
    def pool(self) -> ConnectionPool[Connection[DictRow]]:
        raise ApiOnlyError("RackUsageStore.pool is the API's; the worker has none")

    def close(self) -> None:
        """Nothing to close: the CRUD client is the worker's."""

    async def _keyed(self, path: str, body: Any) -> Any:
        """A write that answers the rows it wrote, under `WriteKeys`' key. A key an
        earlier attempt of this activity spent on another write is refused 422, and this
        write is sent again under a new one."""
        url = f"{BASE}/rack{path}"
        try:
            response = await self._client.request(
                "POST", url, json=body, idempotency_key=self._keys.next()
            )
        except CrudRefusedError as error:
            if error.status != 422 or error.problem.get("type") != KEY_PROBLEM:
                raise
            response = await self._client.request(
                "POST", url, json=body, idempotency_key=uuid.uuid4().hex
            )
        return response.json()

    async def _ids(self, path: str, ids: Iterable[int]) -> set[int]:
        unique = sorted(set(ids))
        if not unique:
            return set()
        url = f"{BASE}/rack{path}?{urlencode({'id': unique}, doseq=True)}"
        return {int(found) for found in (await self._client.request("GET", url)).json()}

    async def seen(self, printer: str | None, serials: Iterable[str]) -> None:
        """Natural: an upsert by serial."""
        unique = _unique(serials)
        if unique:
            await self._client.request(
                "POST", f"{BASE}/rack/seen", json={"printer": printer, "serials": unique}
            )

    async def usage(self, serials: Iterable[str]) -> dict[str, Usage]:
        """A ``POST`` read: the serials stay out of the URL, so out of access logs (spec
        §7)."""
        unique = _unique(serials)
        if not unique:
            return {}
        response = await self._client.request(
            "POST", f"{BASE}/rack/usage", json={"serials": unique}
        )
        return {
            serial: Usage(
                prints=row["prints"],
                print_seconds=row["print_seconds"],
                grams=row["grams"],
                first_seen_at=_when(row["first_seen_at"]),
                pending=row["pending"],
                last_used_at=_when(row["last_used_at"]),
            )
            for serial, row in response.json().items()
        }

    async def record_picks(
        self,
        queue_item_id: int,
        printer: str | None,
        picks: Sequence[PickedHotend],
        *,
        copies: int = 1,
    ) -> int:
        if not picks:
            return 0
        body = {
            "queue_item_id": queue_item_id,
            "printer": printer,
            "picks": [pick.model_dump(mode="json") for pick in picks],
            "copies": copies,
        }
        return int((await self._keyed("/picks", body))["written"])

    async def picked_items(self, queue_item_ids: Iterable[int]) -> set[int]:
        return await self._ids("/picked", queue_item_ids)

    async def recorded_archives(self, archive_ids: Iterable[int]) -> set[int]:
        return await self._ids("/recorded-archives", archive_ids)

    async def record_prints(
        self,
        *,
        archive_id: int,
        queue_item_id: int,
        settled_at: datetime,
        print_seconds: int | None,
        grams: float | None,
    ) -> int:
        body = {
            "archive_id": archive_id,
            "queue_item_id": queue_item_id,
            "settled_at": settled_at.isoformat(),
            "print_seconds": print_seconds,
            "grams": grams,
        }
        # A new UUID per call, which the client sends again on its retries: the body
        # carries the settle's ``now()``, so a retried attempt's write is never the same
        # request and an activity's key would only cost a 422 round trip.
        response = await self._client.request(
            "POST", f"{BASE}/rack/prints", json=body, idempotency_key=uuid.uuid4().hex
        )
        return int(response.json()["written"])

    async def record_lan_picks(
        self,
        run_id: str,
        subtask_name: str,
        printer: str | None,
        picks: Sequence[PickedHotend],
    ) -> int:
        if not picks:
            return 0
        body = {
            "run_id": run_id,
            "subtask_name": subtask_name,
            "printer": printer,
            "picks": [pick.model_dump(mode="json") for pick in picks],
        }
        response = await self._client.request(
            "POST", f"{BASE}/rack/lan-picks", json=body, idempotency_key=uuid.uuid4().hex
        )
        return int(response.json()["written"])

    async def settle_lan(
        self, run_id: str, subtask_name: str | None = None, *, timed: bool = True
    ) -> int:
        body = {"run_id": run_id, "subtask_name": subtask_name, "timed": timed}
        return int((await self._keyed("/settle-lan", body))["written"])

    async def settle_lan_ended(
        self, printer: str, subtask_name: str, ended_at: datetime, *, timed: bool = True
    ) -> int:
        body = {
            "printer": printer,
            "subtask_name": subtask_name,
            "ended_at": ended_at.isoformat(),
            "timed": timed,
        }
        # Only the watch calls this, between two heartbeats: a new UUID per call (its one
        # long activity repeats `WriteKeys`' keys across attempts), sent again on the
        # client's retries within `WATCH_CALL_BUDGET`.
        response = await self._client.request(
            "POST",
            f"{BASE}/rack/settle-lan-ended",
            json=body,
            idempotency_key=uuid.uuid4().hex,
            timeout=WATCH_CALL_TIMEOUT,
            budget=WATCH_CALL_BUDGET,
        )
        return int(response.json()["written"])


def _when(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


_refuse(RemoteRackUsage, "RackUsageStore", RACK_API_ONLY)


# -- spools, native spools, held presets, native slices (Q28-Q30, #5298 Task 31) ---------

#: The route's reason for a refresh's write that ran past its deadline
#: (`api/crud_print.py`): it rolled back, as `SpoolStore.replace_records`' does.
PAST_DEADLINE = "past-deadline"
#: The route's reason for a slice start of a copy another subject's row holds.
SLICE_SUBJECT = "slice-subject"

#: The `SpoolStore` methods only the API calls: the refresh's ``seen`` half and the
#: providers' list, on the ``library`` worker (`spools/activities.py`), a person's tray
#: links (`api/tray_spools.py`), and the ``_in`` write, which runs on the API's connection.
SPOOLS_API_ONLY = ("seen", "refreshes", "link", "unlink", "replace_records_in")


class RemoteSpoolStore(SpoolStore):
    """`spools/store.py` `SpoolStore` (Q28) over ``…/print/spools`` (area ``spools``).
    Async, as the SQL store is. No pool. The print worker calls it for a native run's
    overlay (`spools/overlay.py`) and the native provider's recache after a print took
    its grams (`spools/spool_providers.py` `recache`). Every SQL write is one transaction
    under the store's advisory lock, so each is one route, run whole on the API (plan
    D6)."""

    def __init__(self, client: CrudClient) -> None:
        self._client = client

    @property
    def pool(self) -> ConnectionPool[Connection[DictRow]]:
        raise ApiOnlyError("SpoolStore.pool is the API's; the worker has none")

    async def replace_records(
        self,
        provider: str,
        snapshots: Sequence[SpoolSnapshot],
        at: datetime,
        *,
        timeout: float | None = None,
    ) -> None:
        """Under an ``Idempotency-Key`` of its own, a new UUID per call, which the client
        sends again on its retries (plan D5): the body carries the refresh's ``at``, so a
        retried activity's `WriteKeys` key would never replay, and a refresh is a whole
        new answer, never a replay of an earlier one. ``timeout`` bounds the call, retries
        included, and the API's write, as it bounds the SQL store's."""
        body = {
            "provider": provider,
            "snapshots": [snapshot.model_dump(mode="json") for snapshot in snapshots],
            "at": at.isoformat(),
            "timeout": timeout,
        }
        try:
            await self._client.request(
                "POST",
                f"{BASE}/spools/records",
                json=body,
                idempotency_key=uuid.uuid4().hex,
                budget=timeout,
            )
        except CrudRefusedError as error:
            if _refused_as(error, PAST_DEADLINE):
                raise TimeoutError(f"the refresh of {provider} ran past its deadline") from None
            raise

    async def failed(self, provider: str, at: datetime, failure: str) -> None:
        """Natural: an upsert of the provider's failure."""
        await self._client.request(
            "POST",
            f"{BASE}/spools/refreshes/{quote(provider, safe='')}/failed",
            json={"at": at.isoformat(), "failure": failure},
        )

    async def overlay(
        self,
        printer_id: UUID,
        trays: Sequence[SeenTray],
        *,
        now: datetime,
        providers: Sequence[str],
        located: Mapping[str, str] | None = None,
    ) -> Overlay:
        """A ``POST`` read: the trays' RFID keys stay out of the URL (spec §7)."""
        body = {
            "printer_id": str(printer_id),
            "trays": [tray.model_dump(mode="json") for tray in trays],
            "now": now.isoformat(),
            "providers": list(providers),
            "located": dict(located or {}),
        }
        response = await self._client.request("POST", f"{BASE}/spools/overlay", json=body)
        return Overlay.model_validate(response.json())


_refuse(RemoteSpoolStore, "SpoolStore", SPOOLS_API_ONLY)


#: The `NativeSpoolStore` methods only the API calls: the shelf's routes
#: (`api/native_spools.py`), and the ``_in`` settle, which runs on the API's connection.
NATIVE_SPOOLS_API_ONLY = ("get", "create", "update", "delete", "settle_uses_in")


class RemoteNativeSpools(NativeSpoolStore):
    """`spools/native.py` `NativeSpoolStore` (Q29) over ``…/print/native-spools`` (area
    ``spools``). Blocking, as the SQL store is: callers on the loop use a thread. No
    pool. The worker reads the shelf (the native provider), saves what a LAN start uses
    and settles a run's uses once its print settles."""

    def __init__(self, client: CrudSyncClient) -> None:
        self._client = client

    @property
    def pool(self) -> ConnectionPool[Connection[DictRow]]:
        raise ApiOnlyError("NativeSpoolStore.pool is the API's; the worker has none")

    def list(self, *, include_archived: bool = False) -> list[NativeSpool]:
        query = urlencode({"include_archived": "true" if include_archived else "false"})
        body = self._client.request("GET", f"{BASE}/native-spools?{query}").json()
        return [NativeSpool.model_validate(row) for row in body]

    def any(self) -> bool:
        return bool(self._client.request("GET", f"{BASE}/native-spools/any").json()["any"])

    def record_uses(self, run_id: str, subtask_name: str, uses: Sequence[SpoolUse]) -> None:
        """Natural: ``ON CONFLICT DO NOTHING`` on the start's slot, so a resend keeps the
        rows one send wrote."""
        if not uses:
            return
        body = {
            "run_id": run_id,
            "subtask_name": subtask_name,
            "uses": [use.model_dump(mode="json") for use in uses],
        }
        self._client.request("POST", f"{BASE}/native-spools/uses", json=body)

    def settle_uses(self, run_id: str, stages: Sequence[str]) -> set[UUID]:
        """One route, one transaction on the API, its row locks included (plan D6). The
        answer is the spools this settle changed, which a resend would not repeat (the
        rows read settled then), so an ``Idempotency-Key`` of its own, a new UUID per
        call, sent again on the client's retries: the follow's read hook settles a run
        from one long activity, whose ordinals would not name one settle."""
        response = self._client.request(
            "POST",
            f"{BASE}/native-spools/uses/settle",
            json={"run_id": run_id, "stages": list(stages)},
            idempotency_key=uuid.uuid4().hex,
        )
        return {UUID(spool_id) for spool_id in response.json()["changed"]}


_refuse(RemoteNativeSpools, "NativeSpoolStore", NATIVE_SPOOLS_API_ONLY)


#: The `HeldPresetStore` methods only the API calls: the copy's refresh, on the
#: ``library`` worker (`slicing/held_presets.py` `refresh_held_presets`), and its clear at
#: the API's start (`slicing/component.py`).
HELD_PRESETS_API_ONLY = ("stamps", "forget_unlisted", "keep", "clear")


class RemoteHeldPresets(HeldPresetStore):
    """`slicing/held_presets.py` `HeldPresetStore` (Q30) over ``…/print/held-presets``
    (area ``spools``): the kept copy a native run's catalogue and presets read. Reads
    only. Async, as the SQL store is. No pool."""

    def __init__(self, client: CrudClient) -> None:
        self._client = client

    async def rows(self) -> list[HeldRow]:
        body = (await self._client.request("GET", f"{BASE}/held-presets")).json()
        return [
            HeldRow(preset=LocalPreset.model_validate(row["preset"]), sha256=row["sha256"])
            for row in body
        ]

    async def documents(self, ids: Collection[int]) -> dict[int, LocalPresetDetail]:
        unique = sorted(set(ids))
        if not unique:
            return {}
        url = f"{BASE}/held-presets/documents?{urlencode({'id': unique}, doseq=True)}"
        body = (await self._client.request("GET", url)).json()
        return {int(found): LocalPresetDetail.model_validate(doc) for found, doc in body.items()}


_refuse(RemoteHeldPresets, "HeldPresetStore", HELD_PRESETS_API_ONLY)


#: The `NativeSliceStore` methods only the API calls: the `SliceWorkflow`'s activities
#: on the ``library`` worker (`workflows/slicing_activities.py`), the dialog's read
#: (`slicing/preview.py`) and the sweeps (`slicing/start.py`, from `main.py`).
NATIVE_SLICES_API_ONLY = (
    "running",
    "prepared",
    "finish",
    "lost",
    "stop_lost",
    "slicing",
    "get",
    "reusable",
    "sweep",
)


class RemoteNativeSlices(NativeSliceStore):
    """`slicing/store.py` `NativeSliceStore` (Q30) over ``…/print/native-slices`` (area
    ``spools``) for the one method the print worker calls: a native slice's ``start``
    (`slicing/start.py` `start_native_slice`, from the dialog's background slice).
    Async, as the SQL store is. No pool. The start runs whole on the API, under its lock
    on the workflow id (plan D6), and is natural on that id: a start sent again answers
    the key's newest running or completed row, which the first send left."""

    def __init__(self, client: CrudClient) -> None:
        self._client = client

    async def start(
        self,
        *,
        workflow_id: str,
        subject: str,
        library_file_id: int,
        plate_id: int,
        preset_key: str,
        fresh: bool = False,
    ) -> NativeSlice:
        body = {
            "workflow_id": workflow_id,
            "subject": subject,
            "library_file_id": library_file_id,
            "plate_id": plate_id,
            "preset_key": preset_key,
            "fresh": fresh,
        }
        try:
            response = await self._client.request("POST", f"{BASE}/native-slices/start", json=body)
        except CrudRefusedError as error:
            if _refused_as(error, SLICE_SUBJECT):
                raise SliceSubjectError(str(error.problem.get("detail", ""))) from None
            raise
        return NativeSlice.model_validate(response.json())


_refuse(RemoteNativeSlices, "NativeSliceStore", NATIVE_SLICES_API_ONLY)


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
