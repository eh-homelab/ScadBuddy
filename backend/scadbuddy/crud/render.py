"""The render worker's stores over the CRUD routes (#5298). The routes are
`api/crud_render.py`.

`RemoteRenderSettingsSource` is `store/bambuddy.py` `RenderSettingsSource` read from the
API instead of the database: the same 30 s re-read, and a re-read that fails keeps the
last good settings.

`RemoteJobProjection` is the part of `render/projection.py` `JobProjection` the worker's
activities call (accept, claims, `project`), with the same names, signatures and
answers. Every write but the finish is natural (plan D5): sent again after a lost
answer, the route answers as it did the first time. The finish sends an
``Idempotency-Key`` of its own, so a resend replays whether this call settled the row.

`RemoteBlobRefs` and `RemoteGeometryIndex` are `store/refs.py` `BlobRefs.add` and
`store/geometry.py` `GeometryIndex` (get, put, forget) over the routes, with the same
names, signatures and answers. Every write is natural.

`RemoteBlobIndex` is `store/index.py` `BlobIndex` (the part `ContentStore` and its callers
use, `IndexesBlobs`) over the routes, with the same answers: a `put` the object it
replaced, a delete the row it took, `ReuseLostError` and, once the API's own retries are
spent, `DeadlockDetected` as over SQL. Its non-natural writes send an
``Idempotency-Key`` each (`WriteKeys`). A put, swap or mark of a key only the API writes
(an upload's, a font's, a snapshot's) is refused, a `CrudRefusedError` 403.

`RemoteStoreFolders` is `store/bambuddy.py` `FolderFinder` (`KeepsFolders`) over the
routes (plan D6): the API finds or makes a folder in Bambuddy itself, with its own render
target and key, under the folder's advisory lock, and records it, all in one request. It
does so only for the Bambuddy and inbox the worker names, which must be the API's own.
Every call is natural: a find sent again answers the folder the first one recorded, and a
forget of nothing is a success.

`RemoteAssetRows` is the ``assets`` rows of `library/assets.py` `RemoteAssetStore`, the
worker's uploads, whose files stay on the worker's volume: get, use, adopt and the last
uses a prune reads. Each refusal is the error the SQL store raised: no row is
`AssetNotFoundError`, an adopt the API refuses `AssetRejectedError`. A failure that passes
(the API out of reach) stays the client's `CrudUnavailableError`, which the activity
retries, never a not-found a render fails on for good. Every call is natural: an adopt
sent again is the same row (ON CONFLICT), a use stamps the row again.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from psycopg.errors import DeadlockDetected, LockNotAvailable
from temporalio import activity

from scadbuddy.bambuddy.client import DEFAULT_TIMEOUT as BAMBUDDY_TIMEOUT
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.client import CrudClient, CrudRefusedError, CrudSyncClient, activity_key
from scadbuddy.crud.guard import PREFIX
from scadbuddy.crud.idempotency import KEY_PROBLEM
from scadbuddy.library.assets import AssetMeta, AssetNotFoundError, AssetRejectedError
from scadbuddy.library.settings_store import RenderStoreSettings
from scadbuddy.render.job_models import Job, JobNotFoundError, QueueFullError, StepInfo
from scadbuddy.render.projection import PROJECT_POOL_TIMEOUT
from scadbuddy.store.bambuddy import FOLDER_LOCK_SECONDS, FolderRole, Inbox, StoreSettingsSource
from scadbuddy.store.content_models import BlobRef, BlobStat, ReuseLostError
from scadbuddy.store.geometry import GeometryRow

logger = logging.getLogger(__name__)

BASE = f"/api/v1{PREFIX}/render"
#: `api/crud_render.py` `QUEUE_FULL`.
QUEUE_FULL = "queue-full"
#: `api/crud_render.py` `REUSE_LOST` and `DEADLOCK`.
REUSE_LOST = "reuse-lost"
DEADLOCK = "deadlock"
#: `api/crud_render.py` `BAMBUDDY_UNAVAILABLE`.
BAMBUDDY_UNAVAILABLE = "bambuddy-unavailable"
#: `api/crud_render.py` `TARGET_MOVED` and `FOLDER_LOCKED`.
TARGET_MOVED = "target-moved"
FOLDER_LOCKED = "folder-locked"
#: A problem's RFC 9457 members; every other member is an extension of the `ApiError`.
_PROBLEM_MEMBERS = frozenset({"type", "title", "status", "detail", "instance"})
#: How many activity attempts `WriteKeys` counts writes for at once; the oldest is
#: forgotten first (an attempt long finished).
WRITE_KEYS_KEPT = 1024
#: How long a `project` write tries again after a failed try, inside its 10 s attempt
#: (`pipelines.PROJECT_TIMEOUT`). Each wait of a try (connect, send, read) lasts at most
#: `PROJECT_POOL_TIMEOUT` (5 s), as the SQL store's pool wait does; a credential exchange
#: is outside these bounds.
PROJECT_BUDGET = 4.0
#: An accept's bounds. Each wait of a try (connect, send, read) lasts at most
#: `ACCEPT_TIMEOUT` (accepts took 0.03-0.4 s on prod) and no try starts past
#: `ACCEPT_BUDGET`, so `ACCEPT_RETRY`'s three attempts with their backoff (1 s, 2 s)
#: usually end within the route's 10 s deadline. A slow connect followed by a slow read
#: can take a try past that; the request is then answered still-accepting at the
#: deadline, and the run's last attempt decides. A read timeout on the last attempt
#: passes (`pipelines.ACCEPT_RETRY`), so a slow API is never answered render-unstartable.
ACCEPT_TIMEOUT = 2.0
ACCEPT_BUDGET = 1.0
#: A template call's hold of the blob it emitted (`pipeline_activities._hold`): as long as
#: the SQL store's pool wait (30 s). The hold comes after the template ran, so a hold
#: that fails reruns the template.
HOLD_BUDGET = 30.0
#: A folder find's wait for its answer: the API finds the template's folder and then its
#: `Work/`, each under a lock it may wait `FOLDER_LOCK_SECONDS` for, then a listing and a
#: create at Bambuddy's own timeout. A slow but healthy find is answered inside it.
FIND_TIMEOUT = 2 * (FOLDER_LOCK_SECONDS + 2 * BAMBUDDY_TIMEOUT)
#: How long a find is sent again after a try that failed before the API answered it (no
#: connection, a pod without the route, a 5xx). A lock waited for in vain and Bambuddy
#: not answering are answers, never sent again (`RemoteStoreFolders.find`).
FIND_BUDGET = 5.0


class RemoteRenderSettingsSource(StoreSettingsSource):
    def __init__(
        self,
        client: CrudClient,
        *,
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(ttl=ttl, clock=clock)
        self._client = client

    async def _read(self) -> RenderStoreSettings:
        response = await self._client.request("GET", f"{BASE}/settings")
        return RenderStoreSettings.model_validate(response.json())


class RemoteJobProjection:
    def __init__(self, client: CrudSyncClient) -> None:
        self._client = client

    def close(self) -> None:
        self._client.close()

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        budget: float | None = None,
        timeout: float = PROJECT_POOL_TIMEOUT,
        key: str | None = None,
    ) -> httpx.Response:
        """``budget``: an accept's or a `project` write's call, ended inside its attempt,
        each of its waits bounded by ``timeout``."""
        return self._client.request(
            method,
            f"{BASE}{path}",
            json=json,
            idempotency_key=key,
            timeout=None if budget is None else timeout,
            budget=budget,
        )

    def accept(
        self,
        job: Job,
        key: str,
        *,
        workflow_id: str,
        run_id: str,
        max_pending: int = 0,
        supersedes: str | None = None,
    ) -> Job:
        body = {
            "job": job.model_dump(mode="json"),
            "key": key,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "max_pending": max_pending,
            "supersedes": supersedes,
        }
        try:
            response = self._send(
                "POST", "/jobs/accept", json=body, budget=ACCEPT_BUDGET, timeout=ACCEPT_TIMEOUT
            )
        except CrudRefusedError as error:
            if error.status == 409 and error.problem.get("reason") == QUEUE_FULL:
                raise QueueFullError(int(error.problem["depth"])) from error
            raise
        return Job.model_validate(response.json())

    def set_claims(self, job_id: str, claims: int) -> None:
        self._send(
            "PUT", f"/jobs/{_part(job_id)}/claims", json={"claims": claims}, budget=PROJECT_BUDGET
        )

    def mark_started(self, job_id: str) -> Job | None:
        response = self._send("POST", f"/jobs/{_part(job_id)}/started", budget=PROJECT_BUDGET)
        body = response.json()
        return None if body is None else Job.model_validate(body)

    def set_steps(self, job_id: str, steps: list[StepInfo]) -> None:
        self._send(
            "PUT",
            f"/jobs/{_part(job_id)}/steps",
            json=[step.model_dump(mode="json") for step in steps],
            budget=PROJECT_BUDGET,
        )

    def finish(self, job: Job) -> bool:
        """As `JobProjection.finish`: whether this call changed the row (the route's
        ``moved``), so a retried activity's call, which finds it settled, answers False
        and its settle is counted once."""
        response = self._send(
            "POST",
            f"/jobs/{_part(job.id)}/finish",
            json=job.model_dump(mode="json"),
            budget=PROJECT_BUDGET,
            key=uuid.uuid4().hex,
        )
        return bool(response.json()["moved"])

    def read(self, job_id: str, *, pool_timeout: float | None = None) -> Job:
        """``pool_timeout``: a `project` activity's read, bounded as its writes are."""
        try:
            response = self._send(
                "GET",
                f"/jobs/{_part(job_id)}",
                budget=None if pool_timeout is None else PROJECT_BUDGET,
            )
        except CrudRefusedError as error:
            if error.status == 404:
                raise JobNotFoundError(job_id) from error
            raise
        return Job.model_validate(response.json())


class RemoteBlobRefs:
    """The render worker holds blobs for its jobs only: the route refuses any other
    holder kind (a 403 ``holder-kind``) and a holder that is no render job (a 404)."""

    def __init__(self, client: CrudSyncClient) -> None:
        self._client = client

    def add(
        self, key: str, holder_kind: str, holder_id: str, *, budget: float | None = None
    ) -> None:
        """``budget``: how long the call tries; None bounds it as a `project` write, since
        `project` holds a done job's blobs inside its 10 s attempt. A job that is gone
        (pruned while it ran) is no refusal: the SQL write held a blob for any holder id,
        and the sweep dropped that orphan ref, so a ref for it holds nothing and the
        render goes on."""
        body = {"key": key, "holder_kind": holder_kind, "holder_id": holder_id}
        try:
            self._client.request(
                "POST",
                f"{BASE}/blob-refs",
                json=body,
                timeout=PROJECT_POOL_TIMEOUT,
                budget=PROJECT_BUDGET if budget is None else budget,
            )
        except CrudRefusedError as error:
            if error.status != 404:
                raise
            logger.warning(
                "no render job holds this blob: the job is gone",
                extra={"job_id": holder_id, "key": key},
            )


class RemoteGeometryIndex:
    """A hint, as `GeometryIndex` is: each call has the client's own budget, as the SQL
    store's waited the pool's default for a connection."""

    def __init__(self, client: CrudSyncClient) -> None:
        self._client = client

    def get(self, geometry_key: str) -> GeometryRow | None:
        response = self._client.request("GET", f"{BASE}/geometry/{_part(geometry_key)}")
        body = response.json()
        if body is None:
            return None
        return GeometryRow(body["piece_key"], list(body["colours"]), dict(body["main"]))

    def put(
        self, geometry_key: str, piece_key: str, colours: list[str], main: dict[str, Any]
    ) -> None:
        self._client.request(
            "PUT",
            f"{BASE}/geometry/{_part(geometry_key)}",
            json={"piece_key": piece_key, "colours": colours, "main": main},
        )

    def forget(self, geometry_key: str, piece_key: str) -> None:
        self._client.request(
            "DELETE",
            f"{BASE}/geometry/{_part(geometry_key)}?piece_key={_part(piece_key)}",
        )


class WriteKeys:
    """The ``Idempotency-Key`` of each blob-index write (plan D5).

    Inside a Temporal activity, `activity_key` of the write's ordinal in this attempt of
    the activity: a retried attempt sends its writes under the same keys, in the same
    order while its code path is the same, so a write whose answer the last attempt lost
    is answered as it was (a `put` the object it replaced, which this attempt then
    releases). A retried attempt's write is often not the same request (a new upload, or
    a reuse of the object the last attempt stored): the ledger refuses the key for it
    (422), and `RemoteBlobIndex` sends that write again under a new key, as a write of
    its own. Outside an activity (a sweep on the worker's own timer), a new UUID per
    write. Either way the client sends the key again on every retry of that call."""

    def __init__(self) -> None:
        self._ordinals: OrderedDict[tuple[str | None, str | None, str, int], int] = OrderedDict()
        self._lock = threading.Lock()

    def next(self) -> str:
        if not activity.in_activity():
            return uuid.uuid4().hex
        info = activity.info()
        attempt = (info.workflow_id, info.workflow_run_id, info.activity_id, info.attempt)
        with self._lock:
            ordinal = self._ordinals.pop(attempt, 0)
            self._ordinals[attempt] = ordinal + 1
            while len(self._ordinals) > WRITE_KEYS_KEPT:
                self._ordinals.popitem(last=False)
        return activity_key(ordinal)


class RemoteBlobIndex:
    """`BlobIndex` over the routes. Each call has the client's own budget, as each SQL
    call waited the pool's default for a connection."""

    def __init__(self, client: CrudSyncClient) -> None:
        self._client = client
        self._keys = WriteKeys()

    def _read(self, path: str, **query: Any) -> Any:
        return self._client.request("GET", _with(f"{BASE}/blob-index{path}", query)).json()

    def _write(self, method: str, path: str, *, json: Any = None, **query: Any) -> Any:
        url = _with(f"{BASE}/blob-index{path}", query)
        try:
            try:
                response = self._client.request(
                    method, url, json=json, idempotency_key=self._keys.next()
                )
            except CrudRefusedError as error:
                if error.status != 422 or error.problem.get("type") != KEY_PROBLEM:
                    raise
                # An earlier attempt of this activity sent another write under this
                # ordinal (`WriteKeys`): this one is a write of its own.
                response = self._client.request(
                    method, url, json=json, idempotency_key=uuid.uuid4().hex
                )
        except CrudRefusedError as error:
            reason = error.problem.get("reason") if error.status == 409 else None
            if reason == REUSE_LOST:
                raise ReuseLostError(str(error.problem.get("detail", ""))) from error
            if reason == DEADLOCK:
                raise DeadlockDetected(str(error.problem.get("detail", ""))) from error
            raise
        return response.json()

    def get(self, key: str) -> BlobStat | None:
        body = self._read(f"/{_part(key)}")
        return None if body is None else BlobStat.model_validate(body)

    def by_sha(self, sha256: str, kind: str, backend: str) -> BlobRef | None:
        body = self._read("", sha256=sha256, kind=kind, backend=backend)
        return None if body is None else BlobRef.model_validate(body)

    def put(
        self,
        key: str,
        ref: BlobRef,
        *,
        slug: str | None,
        meta: dict[str, Any],
        reuse: bool = False,
    ) -> BlobRef | None:
        body = self._write(
            "PUT",
            f"/{_part(key)}",
            json={"ref": ref.model_dump(mode="json"), "slug": slug, "meta": meta, "reuse": reuse},
        )
        previous = body["previous"]
        return None if previous is None else BlobRef.model_validate(previous)

    def swap(
        self,
        key: str,
        ref: BlobRef,
        *,
        expected: str | None,
        slug: str | None,
        meta: dict[str, Any],
        reuse: bool = False,
    ) -> bool:
        body = self._write(
            "POST",
            f"/{_part(key)}/swap",
            json={
                "ref": ref.model_dump(mode="json"),
                "expected": expected,
                "slug": slug,
                "meta": meta,
                "reuse": reuse,
            },
        )
        return bool(body["swapped"])

    def now(self) -> datetime:
        return datetime.fromisoformat(self._read("/now")["now"])

    def mark(self, keys: Sequence[str], *, backend: str, cutoff: datetime, **meta: Any) -> None:
        self._write(
            "POST",
            "/mark",
            json={
                "keys": list(keys),
                "backend": backend,
                "cutoff": cutoff.isoformat(),
                "meta": meta,
            },
        )

    def touch(self, key: str) -> None:
        self._client.request("POST", f"{BASE}/blob-index/{_part(key)}/touch")

    def delete(self, key: str, *, backend: str | None = None) -> BlobStat | None:
        body = self._write("DELETE", f"/{_part(key)}", backend=backend)
        return None if body is None else BlobStat.model_validate(body)

    def delete_if_stale(
        self, key: str, cutoff: datetime, *, backend: str | None = None
    ) -> BlobStat | None:
        body = self._write(
            "DELETE", f"/{_part(key)}/stale", before=cutoff.isoformat(), backend=backend
        )
        return None if body is None else BlobStat.model_validate(body)

    def shares_backend_id(self, backend: str, backend_id: str) -> bool:
        return bool(self._read(f"/backends/{_part(backend)}/{_part(backend_id)}")["shared"])

    def recorded(self, backend: str, backend_id: str) -> bool:
        path = f"/backends/{_part(backend)}/{_part(backend_id)}"
        return bool(self._read(path, copies="false")["shared"])

    def stats(
        self,
        kinds: Sequence[str] | None,
        slug: str | None = None,
        *,
        backend: str | None = None,
    ) -> list[BlobStat]:
        if kinds is not None and not kinds:
            return []
        body = self._read(
            "/stats", kind=None if kinds is None else list(kinds), slug=slug, backend=backend
        )
        return [BlobStat.model_validate(row) for row in body]

    def usage(self, backend: str) -> tuple[int, int, dict[str, int]]:
        body = self._read("/usage", backend=backend)
        return int(body["count"]), int(body["bytes"]), dict(body["by_kind"])


class RemoteStoreFolders:
    """`KeepsFolders` over the routes, on the worker's event loop. ``budget`` and
    ``timeout`` bound a find (`FIND_BUDGET`, `FIND_TIMEOUT`); a forget and a listing
    have the client's own. ``source`` is the worker's store settings, read again after
    the API says they are older than its own."""

    def __init__(
        self,
        client: CrudClient,
        *,
        source: StoreSettingsSource | None = None,
        budget: float = FIND_BUDGET,
        timeout: float = FIND_TIMEOUT,
    ) -> None:
        self._client = client
        self._source = source
        self._budget = budget
        self._timeout = timeout

    async def find(
        self, client: object, inbox: Inbox, slug: str, role: FolderRole, title: str
    ) -> int:
        """``client``, the worker's Bambuddy client, is not used: the API calls Bambuddy
        with its own. A refusal (the API's Bambuddy is not the worker's, Bambuddy's own
        404) is the `ApiError` the SQL find raised, so an upload still forgets a folder
        Bambuddy lost and makes it again; its title and upstream fields are kept.

        One call lasts at most what the SQL find did: one request, answered within
        `FIND_TIMEOUT`, sent again only while it failed before the API answered, within
        `FIND_BUDGET`. Bambuddy not answering comes as a 424 ``bambuddy-unavailable``,
        raised as the 502 or 504 `ApiError` the SQL find raised; a lock the API waited
        30 s for in vain as a 423 ``folder-locked``, raised as the `LockNotAvailable` the
        SQL find raised. Neither is sent again: the activity's own retry takes over. A
        409 ``target-moved`` (the worker's settings are older than the API's) has
        ``source`` read again first, so the activity's next attempt finds the folder on
        the Bambuddy and inbox Settings now name."""
        body = {
            "slug": slug,
            "role": role,
            "title": title,
            "instance": inbox.instance,
            "inbox_id": inbox.id,
        }
        try:
            response = await self._client.request(
                "POST",
                f"{BASE}/store-folders/find",
                json=body,
                timeout=self._timeout,
                budget=self._budget,
            )
        except CrudRefusedError as error:
            raise self._refused(error) from error
        return int(response.json()["folder_id"])

    def _refused(self, error: CrudRefusedError) -> Exception:
        problem = error.problem
        reason = problem.get("reason")
        if error.status == 423 and reason == FOLDER_LOCKED:
            return LockNotAvailable(str(problem.get("detail", "")))
        if error.status == 409 and reason == TARGET_MOVED and self._source is not None:
            self._source.invalidate()
        code = error.status
        extensions = {k: v for k, v in problem.items() if k not in _PROBLEM_MEMBERS}
        if code == 424 and reason == BAMBUDDY_UNAVAILABLE:
            # A 424 without its status is still Bambuddy out of reach: a 502, retried.
            code = int(extensions.pop("upstream_status", 502))
            extensions.pop("reason", None)
        return ApiError(
            code,
            str(problem.get("detail", "")),
            title=problem.get("title"),
            type_=str(problem.get("type", "about:blank")),
            **extensions,
        )

    async def forget(self, inbox: Inbox, slug: str) -> None:
        await self._client.request(
            "DELETE", _with(f"{BASE}/store-folders", _where(inbox, slug=slug))
        )

    async def work_folders(self, inbox: Inbox) -> set[int]:
        response = await self._client.request(
            "GET", _with(f"{BASE}/store-folders/work", _where(inbox))
        )
        return {int(folder_id) for folder_id in response.json()}


class RemoteAssetRows:
    """`AssetRows` over the routes. Each call has the client's own budget, as each SQL
    call waited the pool's default for a connection; a `use` waits there on the API's
    sweep's row lock, as the SQL `use` did."""

    def __init__(self, client: CrudSyncClient) -> None:
        self._client = client

    def _meta(self, method: str, asset_id: str, path: str = "") -> AssetMeta:
        try:
            response = self._client.request(method, f"{BASE}/assets/{_part(asset_id)}{path}")
        except CrudRefusedError as error:
            if error.status == 404:
                raise AssetNotFoundError(asset_id) from error
            raise
        return AssetMeta.model_validate(response.json())

    def get(self, asset_id: str) -> AssetMeta:
        return self._meta("GET", asset_id)

    def use(self, asset_id: str) -> AssetMeta:
        return self._meta("POST", asset_id, "/use")

    def adopt(self, meta: AssetMeta) -> None:
        """A refusal (no copy of the id in the API's blob store, a copy of another kind
        or size, a malformed id) is the `AssetRejectedError` of a fetched file that does
        not match: `RemoteAssets.ensure` then reports the upload as not in the store."""
        try:
            self._client.request(
                "POST", f"{BASE}/assets/{_part(meta.id)}/adopt", json=meta.model_dump(mode="json")
            )
        except CrudRefusedError as error:
            if error.status in (404, 422):
                raise AssetRejectedError(str(error.problem.get("detail", ""))) from error
            raise

    def last_used(self, ids: Sequence[str]) -> dict[str, datetime | None]:
        response = self._client.request("POST", f"{BASE}/assets/last-used", json={"ids": list(ids)})
        return {
            asset_id: None if stamp is None else datetime.fromisoformat(stamp)
            for asset_id, stamp in response.json().items()
        }


def _where(inbox: Inbox, **query: Any) -> dict[str, Any]:
    return {"instance": inbox.instance, "inbox_id": inbox.id, **query}


def _part(value: str) -> str:
    return quote(value, safe="")


def _with(path: str, query: dict[str, Any]) -> str:
    """``path`` with the query's set values; a list is one parameter per item."""
    present = {name: value for name, value in query.items() if value is not None}
    return f"{path}?{urlencode(present, doseq=True)}" if present else path
