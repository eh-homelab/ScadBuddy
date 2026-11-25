"""A type's profile fetches (#6625, plan 2026-11-18 Decisions 1, 7 to 11): the runs, the
files the profile-fetch worker uploads for one, and the import that makes them the
type's presets.

The worker runs a browser on untrusted web content and holds no database secret and no
Bambuddy key (#5295). It uploads each exported preset as raw bytes named by their sha256
(`store_file`); this process checks the bytes are a filament preset, strips the site's
G-code (`clean_preset`), keeps the result as a blob held by the run
(``blob_refs`` holder `HOLDER`, released with the run by its delete trigger), and records
the key and the preset's name in the run's ``files``. The import (`import_run`) makes each
held file the type's ScadBuddy preset of its key (#7176, plan
2026-11-23-scadbuddy-filament-presets.md Decisions 3, 12, 13): the type's own nozzle range
wins over 3DFP's (`apply_type_temps`, #7142), a key with no fetched preset gets a new
``3dfp`` one, and one whose document changed gets a new version, its changed keys named in
the run's notes. It merges their ``sb-<id>`` refs over the type's preset rows of the
fetched keys, reads the K values the type's printers hold for an exact filament match
through Bambuddy, and ends the run. Bambuddy gets no import: after the commit the mirror
sends each written preset (`filament/preset_mirror.py`), and a mirror failure never fails
the import. Every write of the end is in one transaction with the idempotency ledger's
row. One run's import runs once at a time: it holds a lease on the
run row (``import_lease_until``, `_claim`). A second request for the run (the worker's
client retrying a slow one) polls with short statements, holding no pooled connection,
then answers the first one's outcome without writing again, or claims the lease once
it has expired (R-PR3).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from uuid import UUID

from fastapi import status
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.models import KProfile
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import Replay, idempotent, record, stored
from scadbuddy.filament.models import TypeKValue, TypePreset
from scadbuddy.filament.preset_mirror import mirror_pending
from scadbuddy.filament.profile_fill import fill_quietly
from scadbuddy.filament.sb_presets import (
    FilamentPresetStore,
    create_on,
    document_bytes,
    keep_document,
    lock_presets_on,
    version_on,
)
from scadbuddy.filament.types import SpoolTypeStore, list_edit
from scadbuddy.orders.attachments import keep_file, read_blob
from scadbuddy.profile_fetch.wire import (
    FETCH_WORKFLOW,
    MAX_FILE_BYTES,
    ExportFailure,
    ExportTarget,
    FetchAsk,
    ImportOutcome,
    MissingKey,
    fetch_workflow_id,
)
from scadbuddy.profile_fetch.workflow import (
    EXECUTION_TIMEOUT,
    IMPORT_REQUEST_TIMEOUT,
    LONGEST_RUN,
    NO_WORKER,
    TOO_LONG,
)
from scadbuddy.slicing.own_presets import sb_ref
from scadbuddy.workflows.pollers import queue_pollers

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

logger = logging.getLogger(__name__)

Conn = Connection[DictRow]
Status = Literal["running", "imported", "partial", "failed"]

#: The ``blob_refs`` holder of a run's files (the migration's delete trigger drops them).
HOLDER: Final = "profile_fetch"
#: The ledger route of an import.
IMPORT_ROUTE: Final = "profile-fetch.import"
#: How long an import waits for the mirror of what it wrote (#7176); the Schedule's next
#: pull sends what is left. The mirror's calls (`preset_mirror._send`), each up to the
#: client's 30 s (`bambuddy/client.py` `DEFAULT_TIMEOUT`), per preset the run wrote (one
#: per printer model and nozzle): a rewrite is one ``PUT`` and reads no listing; a create
#: is one ``POST``, after the listing (`_Listing`, read at most once per run, only before a
#: create or a lost create's match); a rewrite that answers 404 is that ``PUT``, then a
#: create; a preset whose last create's answer was lost first reads the listing and one
#: ``GET /local-presets/{id}`` per preset of its name above that create's floor
#: (`_Listing.made_after`, usually one), then a rewrite of the match, or a create when none
#: holds its document. So 120 s holds four calls at their limit, for example: four
#: rewrites; three creates (listing + three ``POST``); a rewrite that answers 404 then
#: creates (``PUT`` + listing + ``POST``) and one more rewrite; a lost create's match
#: (listing + ``GET`` + ``PUT``) and one more rewrite; or a lost create's match whose
#: rewrite answers 404 (listing + ``GET`` + ``PUT`` + ``POST``). At the speed Bambuddy
#: answers it holds many more. It is a cut-off, not a sum of the worst case: a run past it
#: stops and the Schedule's next pull sends the rest, so it is not raised. It leaves 7 of
#: `profile_fetch/workflow.py` `IMPORT_REQUEST_TIMEOUT`'s 9 minutes to the K-table reads, 14
#: of them at 30 s each.
IMPORT_MIRROR_TIMEOUT: Final = 120.0
#: How often a request that finds the run's import lease held looks again (R-PR3).
LEASE_POLL_SECONDS: float = 2.0
PROBLEMS: Final = "https://scadbuddy.dev/problems/profile-fetch/"
RUN_NOT_FOUND: Final = "run_not_found"
NOT_RUNNING: Final = "not_running"
#: The longest preset name a file may carry.
MAX_NAME: Final = 200
#: The reason of a target with no preset and no failure that names it.
NO_FILE: Final = "the profile-fetch worker sent no file for it"
#: The reason of a gap no run has given a reason for.
NO_PRESET: Final = "no preset for this printer and nozzle yet"
#: The deepest a preset file may nest (the document itself is level 1).
MAX_DEPTH: Final = 32
#: The ``error`` of a run whose workflow Temporal did not start.
NOT_STARTED: Final = "the fetch could not be started"
#: The longest a start (the route's, or one of an invoice confirm's) waits for Temporal
#: to take a fetch's workflow.
START_TIMEOUT_SECONDS: Final = 10.0
#: How long a run may wait for a worker before the API asks Temporal whether one polls
#: the profile-fetch queue (`end_unserved`).
NO_POLLER_GRACE: Final = timedelta(minutes=10)
#: The longest that read may take; a slower Temporal leaves the run alone.
PROBE_TIMEOUT_SECONDS: Final = 5.0
#: How long the API keeps that read's answer for one queue (`PollerCache`).
POLLERS_CACHE_SECONDS: Final = 30.0
#: The keys only a printer or a process preset has.
SETTINGS_IDS: Final = frozenset({"printer_settings_id", "print_settings_id"})


class ProfileFetch(BaseModel):
    """One fetch of a type's slicer profiles from 3dfilamentprofiles.com."""

    id: UUID
    type_id: UUID
    tdfp_id: int
    status: Status
    targets: list[ExportTarget]
    missing: list[MissingKey]
    k_values: int
    notes: list[str]
    error: str | None
    started_at: datetime
    finished_at: datetime | None


_COLUMNS = ", ".join(ProfileFetch.model_fields)


class ProfileFetchStarted(BaseModel):
    """``POST /filament/types/{id}/profile-fetch``: the run, and whether it was one
    already running (``joined``)."""

    run: ProfileFetch
    joined: bool


class ProfileFetchStatus(BaseModel):
    """``GET /filament/types/{id}/profile-fetch``: the type's last run, if any, and the
    targets it has no preset for (`ProfileFetches.gaps`)."""

    run: ProfileFetch | None
    gaps: list[MissingKey]


class FetchRefusedError(Exception):
    """A fetch that cannot start: ``code`` is ``type_not_found``, ``no_tdfp_id`` (the
    type has no 3DFP id) or ``no_printer_models`` (no printer has a model)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProfileFetches:
    """``filament_profile_fetches`` over the settings store's pool. ``nozzles`` is the
    ``profile_fetch_nozzles`` setting, asked on each use. Synchronous."""

    def __init__(self, pool: ConnectionPool[Conn], nozzles: Callable[[], Sequence[str]]) -> None:
        self._pool = pool
        self._nozzles = nozzles

    def open(self, type_id: UUID, *, actor: str) -> tuple[ProfileFetch, bool]:
        """A new running fetch of the type, or the one already running (``True``: it
        joined it). Under the type's row lock, so two opens make one run."""
        # The setting takes a connection of its own: read it before taking ours.
        nozzles = list(self._nozzles())
        with self._pool.connection() as conn, conn.transaction():
            kind = conn.execute(
                "SELECT tdfp_id FROM filament_spool_types WHERE id = %s FOR UPDATE", (type_id,)
            ).fetchone()
            if kind is None:
                raise FetchRefusedError("type_not_found", f"no spool type {type_id}")
            running = conn.execute(
                f"SELECT {_COLUMNS} FROM filament_profile_fetches"
                " WHERE type_id = %s AND status = 'running'",
                (type_id,),
            ).fetchone()
            if running is not None:
                return ProfileFetch.model_validate(running), True
            if kind["tdfp_id"] is None:
                raise FetchRefusedError("no_tdfp_id", "the type has no 3dfilamentprofiles id")
            targets = self._targets(conn, nozzles)
            if not targets:
                raise FetchRefusedError(
                    "no_printer_models", "no printer has a model to fetch presets for"
                )
            row = conn.execute(
                "INSERT INTO filament_profile_fetches (type_id, tdfp_id, targets, started_by)"
                f" VALUES (%s, %s, %s, %s) RETURNING {_COLUMNS}",
                (
                    type_id,
                    kind["tdfp_id"],
                    Jsonb([t.model_dump() for t in targets]),
                    actor,
                ),
            ).fetchone()
        assert row is not None
        return ProfileFetch.model_validate(row), False

    def get(self, run_id: UUID) -> ProfileFetch | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM filament_profile_fetches WHERE id = %s", (run_id,)
            ).fetchone()
        return None if row is None else ProfileFetch.model_validate(row)

    def latest(self, type_id: UUID) -> ProfileFetch | None:
        with self._pool.connection() as conn:
            row = self._latest(conn, type_id)
        return None if row is None else ProfileFetch.model_validate(row)

    def gaps(self, type_id: UUID) -> list[MissingKey]:
        """The targets as they stand now (the printers' models, the nozzle setting) that
        the type has no preset row for, each with the reason its last fetch gave, if any."""
        nozzles = list(self._nozzles())
        with self._pool.connection() as conn:
            targets = self._targets(conn, nozzles)
            have = {
                (row["printer_model"], row["nozzle_diameter"])
                for row in conn.execute(
                    "SELECT printer_model, nozzle_diameter FROM filament_spool_type_presets"
                    " WHERE type_id = %s",
                    (type_id,),
                ).fetchall()
            }
            last = self._latest(conn, type_id)
        reasons = {
            (m["printer_model"], m["nozzle_diameter"]): m["reason"]
            for m in (last["missing"] if last is not None else [])
        }
        return [
            MissingKey(
                printer_model=target.printer_model,
                nozzle_diameter=nozzle,
                reason=reasons.get((target.printer_model, nozzle), NO_PRESET),
            )
            for target in targets
            for nozzle in target.nozzles
            if (target.printer_model, nozzle) not in have
        ]

    def fail_unstarted(self, run_id: UUID) -> None:
        """The run ends ``failed`` with `NOT_STARTED` when it still runs: Temporal did
        not take its start, so no workflow will end it, and a later start must open a
        new run rather than join this one."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE filament_profile_fetches SET status = 'failed', error = %s,"
                " finished_at = now() WHERE id = %s AND status = 'running'",
                (NOT_STARTED, run_id),
            )

    def running_age(self, run_id: UUID) -> timedelta | None:
        """How long ago the run started, by the database's clock; None once it ended."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT now() - started_at AS age FROM filament_profile_fetches"
                " WHERE id = %s AND status = 'running'",
                (run_id,),
            ).fetchone()
        return None if row is None else row["age"]

    def fail_unserved(self, run_id: UUID, reason: str) -> bool:
        """The run ends ``failed`` with ``reason`` when it still runs (`end_unserved`);
        whether this call ended it."""
        with self._pool.connection() as conn:
            ended = conn.execute(
                "UPDATE filament_profile_fetches SET status = 'failed', error = %s,"
                " finished_at = now() WHERE id = %s AND status = 'running'",
                (reason, run_id),
            )
            return ended.rowcount == 1

    @staticmethod
    def _targets(conn: Conn, nozzles: list[str]) -> list[ExportTarget]:
        rows = conn.execute(
            "SELECT DISTINCT model FROM printers WHERE model IS NOT NULL ORDER BY model"
        ).fetchall()
        return [ExportTarget(printer_model=row["model"], nozzles=nozzles) for row in rows]

    @staticmethod
    def _latest(conn: Conn, type_id: UUID) -> dict[str, Any] | None:
        return conn.execute(
            f"SELECT {_COLUMNS} FROM filament_profile_fetches WHERE type_id = %s"
            " ORDER BY started_at DESC, id DESC LIMIT 1",
            (type_id,),
        ).fetchone()


# -- starting a fetch ---------------------------------------------------------------------


class Starter(Protocol):
    """Starts a run's workflow (`FetchStarter`; a test's fake)."""

    async def start(self, ask: FetchAsk) -> None: ...


class FetchStarter:
    """`FETCH_WORKFLOW` on the profile-fetch worker's queue, one per run: a retry of a
    run's start joins its own workflow, and a new run never joins an ended run's."""

    def __init__(self, client: Client, task_queue: str) -> None:
        self.client = client
        self.task_queue = task_queue

    async def start(self, ask: FetchAsk) -> None:
        await self.client.start_workflow(
            FETCH_WORKFLOW,
            ask,
            id=fetch_workflow_id(ask.run_id),
            task_queue=self.task_queue,
            execution_timeout=EXECUTION_TIMEOUT,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )


class QueueProbe(Protocol):
    """What `end_unserved` asks Temporal: whether any worker polls the profile-fetch
    queue, and the cancel of a run's workflow (`TemporalQueueProbe`; a test's fake)."""

    async def has_pollers(self) -> bool: ...

    async def cancel(self, run_id: str) -> None: ...


class PollerCache:
    """DescribeTaskQueue's answer per queue, kept `POLLERS_CACHE_SECONDS`: each read of
    a fetch's status (a page polls it) would otherwise ask every partition of the
    workflow task queue and of the activity one: `2 * TASK_QUEUE_PARTITIONS` calls, sent
    at once (`workflows/pollers.py`)."""

    def __init__(
        self,
        seconds: float = POLLERS_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._seconds = seconds
        self._clock = clock
        self._answers: dict[tuple[str, str], tuple[float, bool]] = {}

    def get(self, key: tuple[str, str]) -> bool | None:
        kept = self._answers.get(key)
        if kept is None or self._clock() - kept[0] >= self._seconds:
            return None
        return kept[1]

    def put(self, key: tuple[str, str], pollers: bool) -> None:
        self._answers[key] = (self._clock(), pollers)


#: The app's cache: `probe` makes a new probe for each read.
POLLERS: Final = PollerCache()


class TemporalQueueProbe:
    """Temporal's DescribeTaskQueue for the queue's workflow and its activity task
    queues: a poller on either is a worker that will take the run. The answer is kept
    in ``cache`` (the app's `POLLERS` by default); a failed read is not kept."""

    def __init__(self, client: Client, task_queue: str, cache: PollerCache = POLLERS) -> None:
        self.client = client
        self.task_queue = task_queue
        self.cache = cache

    async def has_pollers(self) -> bool:
        key = (self.client.namespace, self.task_queue)
        kept = self.cache.get(key)
        if kept is not None:
            return kept
        pollers = await self._read()
        self.cache.put(key, pollers)
        return pollers

    async def cancel(self, run_id: str) -> None:
        """Ask Temporal to cancel the run's workflow (`fetch_workflow_id`)."""
        await self.client.get_workflow_handle(fetch_workflow_id(run_id)).cancel()

    async def _read(self) -> bool:
        # Every partition's pollers: a live worker can be absent from the root's (#7350).
        # Both types at once, so a miss waits for one round of reads, not two (#7425).
        answers = await asyncio.gather(
            queue_pollers(self.client, self.task_queue, TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW),
            queue_pollers(self.client, self.task_queue, TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY),
        )
        return any(answers)


def starter(state: AppState) -> Starter:
    """The app's starter: the operations' client, on the profile-fetch worker's queue."""
    from scadbuddy.operations.component import OPERATIONS

    ops = state.components.get(OPERATIONS)
    return FetchStarter(ops.client, state.settings.temporal_task_queue_profile_fetch)


def probe(state: AppState) -> QueueProbe:
    """The app's probe: the operations' client, on the profile-fetch worker's queue."""
    from scadbuddy.operations.component import OPERATIONS

    ops = state.components.get(OPERATIONS)
    return TemporalQueueProbe(ops.client, state.settings.temporal_task_queue_profile_fetch)


async def end_unserved(state: AppState, run: ProfileFetch) -> ProfileFetch:
    """The run, ended ``failed`` when no worker will serve it: with `NO_WORKER` when it has
    run longer than `NO_POLLER_GRACE` and no worker polls the profile-fetch queue, or,
    with `TOO_LONG` as a last resort, when it has run longer than `workflow.LONGEST_RUN`
    (the workflow's execution timeout plus an hour). The workflow runs on that queue too, so with no
    worker deployed nothing else would ever end it. A worker that polls (a busy one)
    never fails a queued run before then, and a Temporal that does not answer leaves the
    run as it is. A run it ends has its workflow cancelled, best effort."""
    if run.status != "running":
        return run
    runs = fetches(state)
    age = await asyncio.to_thread(runs.running_age, run.id)
    if age is None:
        return await asyncio.to_thread(runs.get, run.id) or run
    if age < NO_POLLER_GRACE:
        return run
    if age <= LONGEST_RUN:
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                served = await probe(state).has_pollers()
        except Exception:
            logger.warning("could not read the pollers of the profile-fetch queue", exc_info=True)
            return run
        if served:
            return run
    reason = NO_WORKER if age <= LONGEST_RUN else TOO_LONG
    if await asyncio.to_thread(runs.fail_unserved, run.id, reason):
        logger.warning("profile fetch %s ended failed: %s", run.id, reason)
        await _cancel_workflow(state, run.id)
    return await asyncio.to_thread(runs.get, run.id) or run


async def _cancel_workflow(state: AppState, run_id: UUID) -> None:
    """Best effort: ask Temporal to cancel the workflow of a run `end_unserved` ended, so
    a worker that takes it later does not export for an ended run. An error or a
    Temporal that does not answer within `PROBE_TIMEOUT_SECONDS` is logged and ignored:
    the workflow then meets 409 ``not_running`` (build Decision 51)."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await probe(state).cancel(str(run_id))
    except Exception:
        logger.warning("could not cancel the workflow of profile fetch %s", run_id, exc_info=True)


def fetches(state: AppState) -> ProfileFetches:
    """The app's runs, with ``profile_fetch_nozzles`` read on each use (a live setting)."""
    store = state.settings_store

    def nozzles() -> list[str]:
        value: list[str] = store.read({"profile_fetch_nozzles"})["profile_fetch_nozzles"]
        return value

    return ProfileFetches(store.pool, nozzles)


class FetchNotStartedError(Exception):
    """Temporal did not take the start; the run ended ``failed`` (`NOT_STARTED`)."""


async def start_fetch(
    state: AppState, type_id: UUID, *, actor: str, timeout: float | None = None
) -> ProfileFetchStarted:
    """Open the type's run (`ProfileFetches.open`) and start its workflow. A run already
    running is joined, and its workflow is not started again, unless no worker serves it
    (`end_unserved` ends it, and a new run is opened). `FetchRefusedError` when
    the type cannot be fetched; `FetchNotStartedError` when Temporal did not take the
    start (or not within ``timeout`` seconds), after the run is ended ``failed``."""
    runs = fetches(state)
    run, joined = await asyncio.to_thread(runs.open, type_id, actor=actor)
    if joined and (await end_unserved(state, run)).status != "running":
        # No worker serves the run it would join: that run ended, and this start opens
        # a new one.
        run, joined = await asyncio.to_thread(runs.open, type_id, actor=actor)
    if not joined:
        ask = FetchAsk(
            run_id=str(run.id), type_id=str(type_id), tdfp_id=run.tdfp_id, targets=run.targets
        )
        try:
            async with asyncio.timeout(timeout):
                await starter(state).start(ask)
        except Exception as exc:
            await asyncio.to_thread(runs.fail_unstarted, run.id)
            raise FetchNotStartedError(NOT_STARTED) from exc
    return ProfileFetchStarted(run=run, joined=joined)


async def start_profile_fetches(state: AppState, type_ids: Sequence[UUID], *, actor: str) -> None:
    """Start a fetch for each type that has a 3DFP id (an invoice confirm's new types,
    plan Decision 12), all at once, each start bounded by `START_TIMEOUT_SECONDS`. A type
    without one is skipped; no printer model is logged once; any other refusal or failure
    is logged, never raised, so it never fails the caller."""
    refusals = await asyncio.gather(*(_start_logged(state, t, actor=actor) for t in type_ids))
    if "no_printer_models" in refusals:
        logger.warning(
            "did not start the profile fetch of %d spool type(s): "
            "no printer has a model to fetch presets for",
            refusals.count("no_printer_models"),
        )


async def _start_logged(state: AppState, type_id: UUID, *, actor: str) -> str | None:
    """`start_fetch` for `start_profile_fetches`: the refusal's code that the caller logs
    (``no_printer_models``), else ``None`` once any other outcome is logged."""
    try:
        await start_fetch(state, type_id, actor=actor, timeout=START_TIMEOUT_SECONDS)
    except FetchRefusedError as refused:
        if refused.code == "no_printer_models":
            return refused.code
        if refused.code != "no_tdfp_id":
            logger.warning("did not start the profile fetch of spool type %s: %s", type_id, refused)
    except Exception:
        logger.warning("could not start the profile fetch of spool type %s", type_id, exc_info=True)
    return None


# -- the worker's routes ------------------------------------------------------------------


def _problem(status_code: int, code: str, detail: str) -> ApiError:
    return ApiError(status_code, detail, type_=PROBLEMS + code, code=code)


def _run(conn: Conn, run_id: UUID, *, lock: bool = False, running: bool = True) -> Any:
    """The run's row: 404 for none, and 409 for one that has ended when ``running``."""
    row = conn.execute(
        "SELECT id, type_id, tdfp_id, status, targets, files, missing, k_values,"
        " notes, error, started_by FROM filament_profile_fetches WHERE id = %s"
        + (" FOR UPDATE" if lock else ""),
        (run_id,),
    ).fetchone()
    if row is None:
        raise _problem(status.HTTP_404_NOT_FOUND, RUN_NOT_FOUND, f"no profile fetch {run_id}")
    if running and row["status"] != "running":
        raise _problem(
            status.HTTP_409_CONFLICT, NOT_RUNNING, "This profile fetch is no longer running."
        )
    return row


def _keys(targets: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(t["printer_model"], n) for t in targets for n in t["nozzles"]]


def _check_target(row: Any, printer_model: str, nozzle: str) -> None:
    if (printer_model, nozzle) not in _keys(row["targets"]):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the run does not fetch {printer_model} with a {nozzle} mm nozzle",
        )


def _unfit(detail: str) -> ApiError:
    return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def _no_constant(name: str) -> Any:
    raise ValueError(f"a preset file holds {name}, which is not JSON")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"a preset file holds {text}, which is not a finite number")
    return value


def _depth(doc: Any) -> int:
    """How deep ``doc`` nests (a scalar is 0, ``{}`` is 1), without recursion."""
    deepest = 0
    stack = [(doc, 1)]
    while stack:
        value, level = stack.pop()
        if isinstance(value, dict):
            value = list(value.values())
        if isinstance(value, list):
            deepest = max(deepest, level)
            stack.extend((item, level + 1) for item in value)
    return deepest


def clean_preset(body: bytes) -> tuple[str, bytes]:
    """A site's preset file as Bambuddy may take it: its ``name``, and the document
    without a top-level key whose name ends in ``gcode`` or ``post_process`` (the printer
    runs that G-code; the slicer then uses its base's, or its built-in default), as JSON
    again. 422 unless it is a filament preset: a JSON object, at most `MAX_DEPTH` levels
    deep and with no NaN, Infinity or number too large for a float, with a ``name`` of 1 to
    `MAX_NAME` characters, a ``type`` that is absent or ``filament``, and no
    ``printer_settings_id`` or ``print_settings_id`` (a printer or process preset's).
    ``inherits``, when present, is a string, and may be empty (#7029): 3DFP exports
    flattened presets, every option written out, with ``"inherits": ""`` (all 72 files
    exported on 2026-11-22).
    Bambuddy's import then keeps the document as it is (``services/orca_profiles.py``
    ``resolve_preset`` returns a preset with no ``inherits`` unchanged), and the native
    slicer flattens it over nothing (``slicing/profiles.py`` ``flatten``).
    Stable: the bytes it gives are cleaned to themselves."""
    try:
        doc = json.loads(body, parse_constant=_no_constant, parse_float=_finite)
    except RecursionError:
        raise _unfit(f"a preset file nests at most {MAX_DEPTH} levels deep") from None
    except ValueError:  # JSONDecodeError and UnicodeDecodeError are ValueErrors too
        doc = None
    if _depth(doc) > MAX_DEPTH:
        raise _unfit(f"a preset file nests at most {MAX_DEPTH} levels deep")
    name = doc.get("name") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or not isinstance(name, str) or not 1 <= len(name) <= MAX_NAME:
        raise _unfit(
            f"a preset file is a JSON object whose name is a string of 1 to {MAX_NAME} characters"
        )
    if doc.get("type", "filament") != "filament" or SETTINGS_IDS & doc.keys():
        raise _unfit("a preset file is a filament preset, not a printer or process preset")
    if not isinstance(doc.get("inherits", ""), str):
        raise _unfit("a preset file's inherits, when it has one, is a string")
    kept = {
        key: value
        for key, value in doc.items()
        if not key.lower().endswith("gcode") and key != "post_process"
    }
    return name, json.dumps(kept, indent=4, ensure_ascii=False).encode()


async def store_file(
    state: AppState, run_id: UUID, sha256: str, printer_model: str, nozzle: str, body: bytes
) -> None:
    """``PUT …/runs/{run_id}/files/{sha256}``: one target's preset file, checked and
    cleaned (`clean_preset`), kept as a blob and held by the run. ``sha256`` is the sent
    bytes'; the kept blob is the cleaned document, named by its own sha256. A file sent
    again for a key replaces it, and the hold of a blob no key of the run names any more
    goes."""
    if len(body) > MAX_FILE_BYTES:
        raise ApiError(status.HTTP_413_CONTENT_TOO_LARGE, "a preset file is at most 512 KiB")
    if hashlib.sha256(body).hexdigest() != sha256:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "a file's name is its sha256")
    name, body = clean_preset(body)
    sha256 = hashlib.sha256(body).hexdigest()
    pool = state.settings_store.pool

    def check() -> None:
        with pool.connection() as conn:
            _check_target(_run(conn, run_id), printer_model, nozzle)

    await asyncio.to_thread(check)
    key = await keep_file(state.store.blobs, io.BytesIO(body), sha256)

    def write() -> None:
        with pool.connection() as conn, conn.transaction():
            row = _run(conn, run_id, lock=True)
            _check_target(row, printer_model, nozzle)
            files = row["files"]
            files.setdefault(printer_model, {})[nozzle] = {
                "key": key,
                "name": name,
                "sha256": sha256,
            }
            conn.execute(
                "UPDATE filament_profile_fetches SET files = %s WHERE id = %s",
                (Jsonb(files), run_id),
            )
            conn.execute(
                "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES (%s, %s, %s)"
                " ON CONFLICT DO NOTHING",
                (key, HOLDER, str(run_id)),
            )
            held = [entry["key"] for model in files.values() for entry in model.values()]
            conn.execute(
                "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s"
                " AND NOT (key = ANY(%s))",
                (HOLDER, str(run_id), held),
            )

    await asyncio.to_thread(write)


async def mark_failed(state: AppState, run_id: UUID, reason: str) -> None:
    """``POST …/runs/{run_id}/failed``: the workflow gave up; the run ends ``failed``."""

    def write() -> None:
        with state.settings_store.pool.connection() as conn, conn.transaction():
            _run(conn, run_id, lock=True)
            conn.execute(
                "UPDATE filament_profile_fetches SET status = 'failed', error = %s,"
                " finished_at = now() WHERE id = %s",
                (reason, run_id),
            )

    await asyncio.to_thread(write)


# -- the import ---------------------------------------------------------------------------


@dataclass
class _Ending:
    """What an import writes: the run's end, and the rows it merges over the type's."""

    status: Literal["imported", "partial", "failed"]
    missing: list[MissingKey]
    presets: list[TypePreset] = field(default_factory=list)
    k_values: list[TypeKValue] = field(default_factory=list)
    #: The (printer, nozzle) keys whose K rows the fetch replaces, each with the printer's
    #: label: a mapped file's key on a printer of its model, unless that printer's K
    #: table did not answer (`_k_values`). Their old rows that ``k_values`` does not hold
    #: are removed (R-F4).
    k_scope: dict[tuple[UUID, str], str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    #: The fetched documents, each kept as a blob, that the end makes the type's ScadBuddy
    #: presets of their keys (`_write_presets`), which fills ``presets``.
    writes: list[_Write] = field(default_factory=list)
    #: The ScadBuddy presets the end wrote (a new one or a new version), for the mirror.
    written: list[int] = field(default_factory=list)

    def outcome(self) -> ImportOutcome:
        return ImportOutcome(
            status=self.status,
            presets=len(self.presets),
            k_values=len(self.k_values),
            missing=self.missing,
            notes=self.notes,
        )


@dataclass(frozen=True)
class _File:
    printer_model: str
    nozzle: str
    key: str
    name: str
    sha256: str


@dataclass(frozen=True)
class _Write:
    """A fetched file's document as ScadBuddy keeps it (cleaned, the type's temperatures
    applied), kept as blob ``key``."""

    file: _File
    key: str
    sha256: str
    doc: dict[str, Any]
    #: The keys whose values differ from the preset's current document, read before the
    #: end; ``None`` when the key has no fetched preset yet.
    changed: list[str] | None


async def import_run(
    state: AppState, run_id: UUID, key: str, failures: list[ExportFailure]
) -> ImportOutcome:
    """``POST …/runs/{run_id}/import``: the run's files made the type's ScadBuddy presets
    (#7176), once per ``key``. A run already ended answers its stored outcome and writes
    nothing. The blob store down raises, so the worker retries with the same key. The
    import holds the run's lease (`_claim`), so a request that arrives while another for
    the run still runs waits for it, holding no pooled connection, and then answers its
    outcome (R-PR3)."""
    asked = {"failures": [f.model_dump() for f in failures]}
    pool = state.settings_store.pool

    def first() -> tuple[Replay | None, Any, datetime | None, list[dict[str, Any]]]:
        with pool.connection() as conn:
            replay = stored(conn, "profile-fetch", key, IMPORT_ROUTE, asked)
            if replay is not None:
                return replay, None, None, []
            row = _run(conn, run_id, running=False)
            if row["status"] != "running":
                return None, row, None, []
            lease = _claim(conn, run_id)
            if lease is None:
                return None, None, None, []
            printers = conn.execute(
                "SELECT id, name, model, bambuddy_printer_id FROM printers"
                " WHERE bambuddy_printer_id IS NOT NULL AND model IS NOT NULL"
                " ORDER BY name, id"
            ).fetchall()
        return None, row, lease, printers

    while True:
        replay, row, lease, printers = await asyncio.to_thread(first)
        if replay is not None:
            return ImportOutcome.model_validate(replay.body)
        if row is not None and lease is None:
            return _stored_outcome(row)
        if lease is not None:
            break
        await asyncio.sleep(LEASE_POLL_SECONDS)
    try:
        return await _import_once(state, row, key, asked, printers, failures)
    finally:
        # Also after a cancel: the release runs on in its thread.
        await asyncio.shield(asyncio.to_thread(_release, pool, run_id, lease))


def _claim(conn: Conn, run_id: UUID) -> datetime | None:
    """The run's import lease, until `IMPORT_REQUEST_TIMEOUT` from now, if the run is
    still running and no other request holds a lease that has not expired; else None."""
    claimed = conn.execute(
        "UPDATE filament_profile_fetches SET import_lease_until = now() + %s"
        " WHERE id = %s AND status = 'running'"
        " AND (import_lease_until IS NULL OR import_lease_until < now())"
        " RETURNING import_lease_until",
        (IMPORT_REQUEST_TIMEOUT, run_id),
    ).fetchone()
    return None if claimed is None else claimed["import_lease_until"]


def _release(pool: ConnectionPool[Conn], run_id: UUID, lease: datetime) -> None:
    """Give the lease back, unless it expired and another request has claimed it since."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_profile_fetches SET import_lease_until = NULL"
            " WHERE id = %s AND import_lease_until = %s",
            (run_id, lease),
        )


async def _import_once(
    state: AppState,
    row: Any,
    key: str,
    asked: dict[str, Any],
    printers: list[dict[str, Any]],
    failures: list[ExportFailure],
) -> ImportOutcome:
    pool = state.settings_store.pool
    run_id = row["id"]
    ending = await _import(state, row, failures, printers)
    first_presets: list[bool] = []

    def finish() -> Replay:
        with pool.connection() as conn, conn.transaction():
            again = idempotent(conn, "profile-fetch", key, IMPORT_ROUTE, asked)
            if again is not None:
                return again
            now = _run(conn, run_id, lock=True, running=False)
            if now["status"] != "running":
                reply = Replay(200, _stored_outcome(now).model_dump(mode="json"))
            else:
                # R-T9-3: a change fans out to the type's spools, as a Type page save does.
                type_id, by = now["type_id"], now["started_by"]
                had = conn.execute(
                    "SELECT 1 FROM filament_spool_type_presets WHERE type_id = %s LIMIT 1",
                    (type_id,),
                ).fetchone()
                _write_presets(conn, now, ending)
                first_presets.append(had is None and bool(ending.presets))
                with list_edit(conn, type_id, "presets", by):
                    SpoolTypeStore.merge_presets(conn, type_id, ending.presets)
                with list_edit(conn, type_id, "k_values", by):
                    _merge_k_values(conn, type_id, ending)
                    _drop_stale_k_values(conn, type_id, ending)
                files = now["files"]
                for preset in ending.presets:
                    entry = files[preset.printer_model][preset.nozzle_diameter]
                    entry["preset_id"] = preset.slicer_filament
                conn.execute(
                    "UPDATE filament_profile_fetches SET status = %s, files = %s, missing = %s,"
                    " k_values = %s, notes = %s, error = %s, finished_at = now(),"
                    " import_lease_until = NULL"
                    " WHERE id = %s",
                    (
                        ending.status,
                        Jsonb(files),
                        Jsonb([m.model_dump() for m in ending.missing]),
                        len(ending.k_values),
                        Jsonb(ending.notes),
                        ending.error,
                        run_id,
                    ),
                )
                reply = Replay(200, ending.outcome().model_dump(mode="json"))
            record(conn, "profile-fetch", key, IMPORT_ROUTE, asked, reply)
            return reply

    done = await asyncio.to_thread(finish)
    if ending.written:
        # After the commit, and a failure never fails the import (#7176 Decision 11).
        settings = await asyncio.to_thread(state.settings_store.load)
        own = FilamentPresetStore(pool, lambda: state.store.blobs)
        # Bounded, so the worker's import call never waits on a slow Bambuddy; what is
        # left is sent by the Schedule's next pull.
        try:
            await asyncio.wait_for(
                mirror_pending(pool, own, settings, ending.written), IMPORT_MIRROR_TIMEOUT
            )
        except TimeoutError:
            logger.warning("the preset mirror ran past the import's bound; the next pull sends")
    if first_presets == [True]:
        # A type's first presets fill its empty fields from their profile (#6714, R-6714-a);
        # after the commit, and a failure never fails the import.
        from scadbuddy.filament.component import FILAMENT

        await fill_quietly(
            lambda: state.components.get(FILAMENT).filler,
            state.settings_store.load,
            [row["type_id"]],
            actor=row["started_by"],
        )
    return ImportOutcome.model_validate(done.body)


def _stored_outcome(row: Any) -> ImportOutcome:
    """An ended run's outcome, from its row: its presets are its files' keys that are not
    missing (none for a failed run)."""
    missing = [MissingKey.model_validate(m) for m in row["missing"]]
    gone = {(m.printer_model, m.nozzle_diameter) for m in missing}
    presets = (
        0
        if row["status"] == "failed"
        else sum(
            1
            for model, nozzles in row["files"].items()
            for nozzle in nozzles
            if (model, nozzle) not in gone
        )
    )
    return ImportOutcome(
        status=row["status"],
        presets=presets,
        k_values=row["k_values"],
        missing=missing,
        notes=row["notes"],
    )


async def _import(
    state: AppState,
    row: Any,
    failures: list[ExportFailure],
    printers: list[dict[str, Any]],
) -> _Ending:
    targets = _keys(row["targets"])
    files = [
        _File(model, nozzle, entry["key"], entry["name"], entry["sha256"])
        for model, nozzles in row["files"].items()
        for nozzle, entry in nozzles.items()
        if (model, nozzle) in targets
    ]
    sent = {(f.printer_model, f.nozzle) for f in files}
    reasons = {
        (f.printer_model, f.nozzle): f.reason
        for f in failures
        if (f.printer_model, f.nozzle) in targets and (f.printer_model, f.nozzle) not in sent
    }
    missing = {k: reasons.get(k, NO_FILE) for k in targets if k not in sent}
    if not files:
        return _failed("3dfilamentprofiles gave no preset file for any target", missing)
    docs: dict[str, dict[str, Any] | None] = {}
    for f in files:
        if f.sha256 not in docs:
            # Cleaned again before it is kept (defence in depth): the blob store is shared,
            # so the bytes are checked where they are used, not only where they came in.
            try:
                docs[f.sha256] = json.loads(clean_preset(await _read_blob(state, f.key))[1])
            except ApiError as error:
                if error.status != status.HTTP_422_UNPROCESSABLE_CONTENT:
                    raise
                docs[f.sha256] = None
    for f in [f for f in files if docs[f.sha256] is None]:
        missing[(f.printer_model, f.nozzle)] = (
            f"the kept preset file is not fit to import: {f.name}"
        )
        files.remove(f)
    if not files:
        return _failed("3dfilamentprofiles gave no preset file fit to import", missing)
    ending = _Ending(status="imported", missing=[])
    temps = await asyncio.to_thread(_type_temps, state.settings_store.pool, row["type_id"])
    own = FilamentPresetStore(state.settings_store.pool, lambda: state.store.blobs)
    for f in files:
        doc = dict(docs[f.sha256] or {})
        for note in apply_type_temps(doc, *temps):
            ending.notes.append(f"{f.printer_model} {f.nozzle} mm: {note}")
        key, sha256 = await keep_document(state.store.blobs, document_bytes(doc))
        changed = await _changed_keys(own, row["type_id"], f, doc)
        ending.writes.append(_Write(f, key, sha256, doc, changed))
    settings = await asyncio.to_thread(state.settings_store.load)
    if printers and settings.bambuddy_url:
        # K values are the printers' calibrations, read through Bambuddy (Decision 12).
        try:
            async with client_for(settings) as client:
                await _k_values(client, ending, row["type_id"], ending.writes, printers)
        except ApiError as error:
            ending.notes.append(f"no K values were read from Bambuddy: {error.detail}")
    ending.missing = [
        MissingKey(printer_model=m, nozzle_diameter=n, reason=r)
        for (m, n), r in sorted(missing.items())
    ]
    if ending.missing:
        ending.status = "partial"
    return ending


def _type_temps(pool: ConnectionPool[Conn], type_id: UUID) -> tuple[int | None, int | None]:
    """The type's own nozzle range, which wins over 3DFP's (#7142)."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT nozzle_temp_min, nozzle_temp_max FROM filament_spool_types WHERE id = %s",
            (type_id,),
        ).fetchone()
    return (None, None) if row is None else (row["nozzle_temp_min"], row["nozzle_temp_max"])


#: The keys a type's nozzle range sets, and the ones clamped into it (#7142).
RANGE_KEYS: Final = ("nozzle_temperature_range_low", "nozzle_temperature_range_high")
CLAMPED_KEYS: Final = ("nozzle_temperature", "nozzle_temperature_initial_layer")


def _number(text: Any) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _degrees(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def apply_type_temps(doc: dict[str, Any], low: int | None, high: int | None) -> list[str]:
    """The type's ``low``/``high`` (its label) written into ``doc``'s nozzle range, and its
    nozzle temperatures clamped into it, in place (#7142: 3DFP's range is its type's
    shared default, not a colour's label). A note per value replaced."""
    notes: list[str] = []
    for name, value in zip(RANGE_KEYS, (low, high), strict=True):
        old = doc.get(name)
        if value is None or not isinstance(old, list) or not old:
            continue
        new = [str(value)] * len(old)
        if old != new:
            doc[name] = new
            notes.append(f"{name} is the type's {value}, not 3DFP's {', '.join(map(str, old))}")
    for name in CLAMPED_KEYS:
        old = doc.get(name)
        if not isinstance(old, list):
            continue
        new = []
        for item in old:
            number = _number(item)
            if number is None:
                new.append(item)
                continue
            floor = number if low is None else max(number, low)
            clamped = floor if high is None else min(floor, high)
            new.append(item if clamped == number else _degrees(clamped))
        if new != old:
            doc[name] = new
            notes.append(
                f"{name} is {', '.join(new)}, inside the type's range, not 3DFP's"
                f" {', '.join(map(str, old))}"
            )
    return notes


async def _changed_keys(
    own: FilamentPresetStore, type_id: UUID, f: _File, doc: dict[str, Any]
) -> list[str] | None:
    """The keys of ``doc`` that differ from the type's fetched preset of ``f``'s key;
    ``None`` when it has none yet. Read before the write takes the presets' lock, so the
    note it makes is best-effort: a version written meanwhile is compared against the
    one read here."""
    current = await asyncio.to_thread(own.fetched, type_id, f.printer_model, f.nozzle)
    if current is None:
        return None
    old = await own.read(current.blob_key) or {}
    return sorted(k for k in {*old, *doc} if old.get(k) != doc.get(k))


#: The most changed keys a version's note names.
MAX_NAMED_KEYS: Final = 12


def _write_presets(conn: Conn, run: Any, ending: _Ending) -> None:
    """Each of ``ending.writes`` made the type's ScadBuddy preset of its key: a new
    ``3dfp`` preset, or a new version of the one there when the document changed
    (Decision 3), each a note. Fills ``ending.presets`` and ``ending.written``. On
    ``conn``, in the end's transaction."""
    # The presets' lock first, as an adoption takes it, so one making this key's
    # preset meanwhile is seen here, never a unique violation.
    lock_presets_on(conn)
    for w in ending.writes:
        f = w.file
        found = conn.execute(
            "SELECT id FROM filament_presets WHERE source = '3dfp' AND type_id = %s"
            " AND printer_model = %s AND nozzle_diameter = %s",
            (run["type_id"], f.printer_model, f.nozzle),
        ).fetchone()
        made = dict(
            name=f.name,
            key=w.key,
            sha256=w.sha256,
            doc=w.doc,
            made_by=run["started_by"],
            fetch_run_id=run["id"],
        )
        if found is None:
            preset_id = create_on(
                conn,
                source="3dfp",
                type_id=run["type_id"],
                printer_model=f.printer_model,
                nozzle_diameter=f.nozzle,
                tdfp_id=run["tdfp_id"],
                **made,
            )
            ending.written.append(preset_id)
        else:
            preset_id = found["id"]
            version = version_on(conn, preset_id, **made)
            if version is not None:
                ending.written.append(preset_id)
                keys = w.changed or []
                named = ", ".join(keys[:MAX_NAMED_KEYS])
                more = len(keys) - MAX_NAMED_KEYS
                ending.notes.append(
                    f"{f.printer_model} {f.nozzle} mm: version {version} of {f.name}"
                    + (f"; changed {named}" if named else "")
                    + (f" and {more} more" if more > 0 else "")
                )
        ending.presets.append(
            TypePreset(
                type_id=run["type_id"],
                printer_model=f.printer_model,
                nozzle_diameter=f.nozzle,
                slicer_filament=sb_ref(preset_id),
                slicer_filament_name=f.name,
            )
        )


def _failed(error: str, missing: dict[tuple[str, str], str]) -> _Ending:
    return _Ending(
        status="failed",
        error=error,
        missing=[
            MissingKey(printer_model=m, nozzle_diameter=n, reason=r)
            for (m, n), r in sorted(missing.items())
        ],
    )


async def _read_blob(state: AppState, key: str) -> bytes:
    data = await read_blob(state.store.blobs, key)
    if data is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, f"the blob store lost {key}")
    return data


async def _k_values(
    client: BambuddyClient,
    ending: _Ending,
    type_id: UUID,
    writes: list[_Write],
    printers: list[dict[str, Any]],
) -> None:
    """The K value each printer holds for a mapped file's filament: only where its
    ``filament_id`` is the preset document's and not a Bambu system id (``GF…``, which
    many types share), and one value per extruder. A printer that does not answer is a
    note, and its key stays out of ``k_scope``, so its old K rows are kept."""
    for printer in printers:
        for w in writes:
            f = w.file
            if f.printer_model != printer["model"]:
                continue
            printer_id = UUID(str(printer["id"]))
            label = f"{printer['name']} ({printer['model']})"
            filament_id = w.doc.get("filament_id")
            if not isinstance(filament_id, str) or filament_id.startswith("GF"):
                ending.k_scope[(printer_id, f.nozzle)] = label
                continue
            try:
                rows = await client.k_profiles(printer["bambuddy_printer_id"], f.nozzle)
            except ApiError as error:
                ending.notes.append(
                    f"{label} did not give its {f.nozzle} mm K table: {error.detail}"
                )
                continue
            ending.k_scope[(printer_id, f.nozzle)] = label
            ending.k_values.extend(_matched(type_id, printer_id, f.nozzle, filament_id, rows))


#: Why a fetch kept a K row of a key it fetched, by the row's ``source`` (Decision 127).
_KEPT_BECAUSE: dict[str, str] = {
    "person": "a person set it",
    "profile": "it came from the linked Bambuddy spools",
    "printer_calibration": "it is the printer's calibration",
}


def _merge_k_values(conn: Conn, type_id: UUID, ending: _Ending) -> None:
    """Write ``ending.k_values`` over the type's K rows that a fetch set
    (`SpoolTypeStore.merge_k_values`, #6857, Decision 127), and keep in ``ending.k_values``
    only the rows written, so the run counts those. A row a person, the fill or a printer
    calibration set stands, named in ``ending.notes`` when its value differs from the fetched
    one. On ``conn``, in the end's transaction."""
    written, kept = SpoolTypeStore.merge_k_values(conn, type_id, ending.k_values)
    for row, old in kept:
        if Decimal(old["k_value"]) == row.k_value:
            continue
        label = ending.k_scope.get((row.printer_id, row.nozzle_diameter), "a printer")
        ending.notes.append(
            f"kept the K value {old['k_value']} of {label}, extruder {old['extruder']},"
            f" {old['nozzle_diameter']} mm, not the fetched {row.k_value}:"
            f" {_KEPT_BECAUSE.get(old['source'], old['source'])}"
        )
    ending.k_values = written


def _drop_stale_k_values(conn: Conn, type_id: UUID, ending: _Ending) -> None:
    """Remove the type's ``profile_fetch`` K rows of ``ending.k_scope``'s keys that the
    fetch did not match (`_Ending.k_scope`), each named in ``ending.notes``; a row a
    person, the fill or a printer calibration set stays (#6857, Decision 127). On
    ``conn``, in the end's transaction, after `_merge_k_values`."""
    keep = {(k.printer_id, k.extruder, k.nozzle_diameter) for k in ending.k_values}
    for (printer_id, nozzle), label in sorted(ending.k_scope.items()):
        rows = conn.execute(
            "SELECT extruder, k_value FROM filament_spool_type_k_values"
            " WHERE type_id = %s AND printer_id = %s AND nozzle_diameter = %s"
            " AND source = 'profile_fetch' ORDER BY extruder",
            (type_id, printer_id, nozzle),
        ).fetchall()
        for row in rows:
            if (printer_id, row["extruder"], nozzle) in keep:
                continue
            conn.execute(
                "DELETE FROM filament_spool_type_k_values WHERE type_id = %s"
                " AND printer_id = %s AND extruder = %s AND nozzle_diameter = %s",
                (type_id, printer_id, row["extruder"], nozzle),
            )
            ending.notes.append(
                f"removed the old K value {row['k_value']} of {label}, extruder"
                f" {row['extruder']}, {nozzle} mm: the fetched preset does not match it"
            )


def _matched(
    type_id: UUID, printer_id: UUID, nozzle: str, filament_id: str, rows: list[KProfile]
) -> list[TypeKValue]:
    by_extruder: dict[int, list[tuple[KProfile, Decimal]]] = {}
    for row in rows:
        value = _k(row.k_value)
        if row.filament_id == filament_id and value is not None:
            by_extruder.setdefault(row.extruder_id, []).append((row, value))
    out: list[TypeKValue] = []
    for extruder, matches in sorted(by_extruder.items()):
        if len({value for _, value in matches}) != 1:
            continue
        row, value = matches[0]
        out.append(
            TypeKValue(
                type_id=type_id,
                printer_id=printer_id,
                extruder=extruder,
                nozzle_diameter=nozzle,
                nozzle_type=row.nozzle_id,
                k_value=value,
                name=row.name,
                cali_idx=row.slot_id,
                setting_id=row.setting_id,
            )
        )
    return out


def _k(text: str) -> Decimal | None:
    """A K value the type's column holds (``numeric(6,4)``, 0 to under 100), or None."""
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() and 0 <= value < 100 else None
