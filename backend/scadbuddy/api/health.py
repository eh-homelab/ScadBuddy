from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from scadbuddy.api.deps import StateDep
from scadbuddy.store.factory import StoreHealth, store_health

router = APIRouter(tags=["health"])


class TemporalHealth(BaseModel):
    """Where this process sends its renders (SCADBUDDY_TEMPORAL_*)."""

    address: str
    namespace: str
    task_queue: str
    worker_inprocess: bool


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    openscad_version: str | None
    data_dir_writable: bool
    # Build provenance, from SCADBUDDY_REVISION / SCADBUDDY_VERSION. A deploy is
    # verified from outside by comparing `revision` with the commit that was
    # pinned, so keep these exact and unformatted.
    revision: str
    version: str
    temporal: TemporalHealth
    #: The blob store this process runs on, against the one stored in Settings (#426).
    store: StoreHealth


#: How long a data dir check answers `/healthz` before another starts (#6418).
DATA_DIR_TTL = 30.0
#: How long the very first `/healthz` waits for the first check.
FIRST_CHECK_WAIT = 2.0
#: A check still running after this long answers "not writable" until it ends, however
#: the last one ended: a stalled NAS is not a writable data dir.
STALLED_AFTER = 5.0


class DataDirCheck:
    """Whether the data dir is a writable directory, checked on a thread of its own,
    one check at a time (#6418). In prod the data dir is a `hard` NFS mount: a stalled
    NAS holds `stat` and `access` until it answers, and on the event loop that would
    hold every request and fail the liveness probe. Not `asyncio.to_thread`: its pool
    is shared, and a full pool is what held `/healthz` before (#6354)."""

    def __init__(self, root: Path, *, ttl: float = DATA_DIR_TTL) -> None:
        self._root = root
        self._ttl = ttl
        self._lock = threading.Lock()
        self._checking: concurrent.futures.Future[None] | None = None
        self._at = float("-inf")
        self._started = 0.0
        self._writable: bool | None = None

    async def writable(self) -> bool:
        """The last answer, or False while a check has run STALLED_AFTER or longer. A
        due check starts in the background and is never waited for, except the first,
        which is waited for up to FIRST_CHECK_WAIT."""
        with self._lock:
            if self._checking is None and time.monotonic() - self._at >= self._ttl:
                self._checking = concurrent.futures.Future()
                self._started = time.monotonic()
                threading.Thread(target=self._check, name="healthz-data-dir", daemon=True).start()
            checking = self._checking
        if self._writable is None and checking is not None:
            await asyncio.wait([asyncio.wrap_future(checking)], timeout=FIRST_CHECK_WAIT)
        with self._lock:
            if self._checking is not None and time.monotonic() - self._started >= STALLED_AFTER:
                return False
            return bool(self._writable)

    def _check(self) -> None:
        try:
            writable = self._root.is_dir() and os.access(self._root, os.W_OK)
        except OSError:
            writable = False
        with self._lock:
            done, self._checking = self._checking, None
            self._writable, self._at = writable, time.monotonic()
        if done is not None:
            done.set_result(None)


_checks: dict[Path, DataDirCheck] = {}
_checks_lock = threading.Lock()


def data_dir_check(root: Path) -> DataDirCheck:
    with _checks_lock:
        if (check := _checks.get(root)) is None:
            check = _checks[root] = DataDirCheck(root)
        return check


@router.get("/healthz", response_model=Health, summary="Liveness")
async def healthz(state: StateDep) -> Health:
    # Never on the event loop (#6418): the data dir is an NFS mount in prod.
    writable = await data_dir_check(state.paths.root).writable()
    # Never waits on a settings read (#6354): the probe has 5 s, and a read waits on the
    # default executor and the database pool.
    store = await store_health(state.store, wait=False)
    healthy = writable and state.openscad_version is not None and store.settings_current
    settings = state.settings
    return Health(
        status="ok" if healthy else "degraded",
        openscad_version=state.openscad_version,
        data_dir_writable=writable,
        revision=settings.revision,
        version=settings.version,
        store=store,
        temporal=TemporalHealth(
            address=settings.temporal_address,
            namespace=settings.temporal_namespace,
            task_queue=settings.temporal_task_queue_render,
            worker_inprocess=settings.temporal_worker_inprocess,
        ),
    )
