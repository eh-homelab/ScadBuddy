"""Where render jobs live between submit and prune, and how workers take them.

`JobBackend` is what `RenderQueue` needs from a store: submitting, with coalescing
and superseding done atomically where the jobs live; claiming the oldest waiting
job; recording how it ended; and recovering jobs whose worker went away. Two
implementations:

- `JobStore` (here): JSON files under ``data/jobs/`` plus an in-process wait list,
  used when no database is configured. The wait list dies with the process, so a
  restart fails whatever was unfinished.
- `PostgresJobStore` (``pg_store.py``, ``SCADBUDDY_DATABASE_URL``): the wait list
  is a table. Accepted jobs survive a restart and are rendered after it, workers
  claim with ``FOR UPDATE SKIP LOCKED``, and a job whose worker stops
  heartbeating is requeued.

Every method is synchronous: `RenderQueue` calls them through `asyncio.to_thread`,
and the API's own reads come from sync routes, which run on the threadpool.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from scadbuddy.core.paths import DataPaths
from scadbuddy.render.job_models import Job
from scadbuddy.render.job_models import now as _now
from scadbuddy.render.schema import ParamValue

SUPERSEDED_ERROR = "superseded by a newer render before it started"
RESTART_ERROR = "interrupted by a restart"
LOST_WORKER_ERROR = "the worker rendering it stopped responding, and it has no attempts left"


class QueueFullError(Exception):
    """``max_pending`` jobs are already waiting (SCADBUDDY_RENDER_QUEUE_MAX). Raised
    before anything changes: a refused submit supersedes nothing and queues nothing.
    `RenderQueue` fills in ``retry_after``."""

    def __init__(self, depth: int, retry_after: int = 1) -> None:
        super().__init__(
            f"the render queue is full ({depth} jobs waiting for a worker); "
            f"try again in {retry_after} s"
        )
        self.depth = depth
        self.retry_after = retry_after


class JobNotFoundError(LookupError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"no job with id {job_id!r}")
        self.job_id = job_id


def render_key(slug: str, params: Mapping[str, ParamValue], model_version: str | None) -> str:
    """What makes two render requests the same render: the model, the revision the
    submit resolved (`None` when there is no repository) and the parameters."""
    raw = json.dumps([slug, model_version, dict(params)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Submitted:
    """How a store answered a submit."""

    job: Job
    #: Answered with a job already waiting, rather than a new one.
    coalesced: bool = False
    #: The job this submit replaced and dropped, if it did.
    superseded: Job | None = None


@dataclass(frozen=True)
class QueueCounts:
    pending: int = 0
    running: int = 0
    #: When the longest-waiting pending job was submitted; `None` with none waiting.
    oldest_pending: datetime | None = None


@dataclass(frozen=True)
class Reaped:
    """Running jobs whose worker stopped heartbeating."""

    requeued: list[Job] = field(default_factory=list)
    failed: list[Job] = field(default_factory=list)


class JobBackend(Protocol):
    def open(self) -> None:
        """Connect, and bring the schema up to date where there is one."""

    def close(self) -> None: ...

    def abandon_orphans(self) -> list[Job]:
        """At startup: fail the unfinished jobs nothing will ever finish."""
        ...

    def reap(self, *, lease: float, max_attempts: int) -> Reaped:
        """Requeue running jobs not heartbeated for ``lease`` seconds, or fail them
        once they have had ``max_attempts``."""
        ...

    def submit(
        self,
        job: Job,
        key: str,
        *,
        supersedes: str | None = None,
        max_pending: int = 0,
    ) -> Submitted:
        """Queue ``job`` -- or answer with the pending job whose key is ``key``.

        ``supersedes`` names a job this one replaces. If it is still pending, one
        claim on it is released, and it is failed as superseded when that was the
        last. When it is itself this render, it is simply returned.

        ``max_pending`` > 0 refuses a NEW job with `QueueFullError` once that many
        wait, counted after the supersede frees its place. A submit answered by
        coalescing takes no place and is never refused. 0 accepts everything."""
        ...

    def claim(self) -> Job | None:
        """Take the oldest pending job and mark it running; `None` if none waits."""
        ...

    def heartbeat(self, job: Job) -> None:
        """Renew the lease on a job `claim` handed out."""
        ...

    def finish(self, job: Job) -> bool:
        """Record a claimed job's final state. False if this worker no longer holds
        it (its lease expired and it was requeued), in which case nothing changes."""
        ...

    def read(self, job_id: str) -> Job:
        """Raises `JobNotFoundError`."""
        ...

    def list_jobs(self) -> list[Job]: ...

    def has_unfinished(self, slug: str) -> bool:
        """Is a render of ``slug`` queued or running?"""
        ...

    def counts(self) -> QueueCounts: ...

    def prune(self, ttl: float, *, now: datetime | None = None) -> list[str]:
        """Delete settled jobs, and their work directories, older than ``ttl``."""
        ...

    def delete(self, job_id: str) -> None: ...


@dataclass
class _Waiting:
    job: Job
    key: str
    #: Submissions answered with this job: one, plus one per coalesced duplicate.
    claims: int = 1


class JobStore:
    """Jobs as JSON files on the PVC, with the wait list in this process."""

    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths
        self._lock = threading.Lock()
        # Insertion order is submit order: the first entry is the next to claim.
        self._waiting: dict[str, _Waiting] = {}
        self._by_key: dict[str, str] = {}
        self._running: set[str] = set()

    def open(self) -> None:
        self.paths.jobs.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        pass

    def write(self, job: Job) -> None:
        """Atomically: a status poll reads the file from another thread while a worker
        rewrites it, and must see the old job or the new one, never a torn one."""
        self.paths.jobs.mkdir(parents=True, exist_ok=True)
        target = self.paths.job_file(job.id)
        staging = target.with_name(f".{target.name}.{threading.get_ident()}.tmp")
        staging.write_text(
            json.dumps(job.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )
        os.replace(staging, target)

    def read(self, job_id: str) -> Job:
        try:
            raw = self.paths.job_file(job_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            raise JobNotFoundError(job_id) from None
        return Job.model_validate_json(raw)

    def list_jobs(self) -> list[Job]:
        if not self.paths.jobs.is_dir():
            return []
        jobs = [
            Job.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.paths.jobs.glob("*.json"))
        ]
        return sorted(jobs, key=lambda job: job.created_at)

    def has_unfinished(self, slug: str) -> bool:
        return any(
            job.slug == slug and job.state in ("pending", "running") for job in self.list_jobs()
        )

    def delete(self, job_id: str) -> None:
        self.paths.job_file(job_id).unlink(missing_ok=True)
        shutil.rmtree(self.paths.job_work_dir(job_id), ignore_errors=True)

    def abandon_orphans(self) -> list[Job]:
        return self.fail_unfinished()

    def fail_unfinished(self) -> list[Job]:
        failed: list[Job] = []
        for job in self.list_jobs():
            if job.state not in ("pending", "running"):
                continue
            job.state = "failed"
            job.finished_at = _now()
            job.error = RESTART_ERROR
            self.write(job)
            failed.append(job)
        return failed

    def reap(self, *, lease: float, max_attempts: int) -> Reaped:
        # A worker here is a task in this process; it cannot be lost without the
        # process, and `abandon_orphans` deals with that at the next start.
        return Reaped()

    def submit(
        self,
        job: Job,
        key: str,
        *,
        supersedes: str | None = None,
        max_pending: int = 0,
    ) -> Submitted:
        with self._lock:
            previous = self._waiting.get(supersedes) if supersedes is not None else None
            if previous is not None and previous.key == key:
                return Submitted(previous.job, coalesced=True)
            if max_pending and key not in self._by_key:
                # Decided before anything changes, so a refusal supersedes nothing.
                frees = previous is not None and previous.claims == 1
                depth = len(self._waiting) - (1 if frees else 0)
                if depth >= max_pending:
                    raise QueueFullError(depth)
            superseded = self._release(supersedes) if supersedes is not None else None
            existing = self._by_key.get(key)
            if existing is not None:
                waiting = self._waiting[existing]
                waiting.claims += 1
                return Submitted(waiting.job, coalesced=True, superseded=superseded)
            self.write(job)
            self._waiting[job.id] = _Waiting(job=job, key=key)
            self._by_key[key] = job.id
            return Submitted(job, superseded=superseded)

    def _release(self, job_id: str) -> Job | None:
        waiting = self._waiting.get(job_id)
        if waiting is None:
            return None
        waiting.claims -= 1
        if waiting.claims > 0:
            return None
        self._forget(job_id)
        job = waiting.job
        job.state = "failed"
        job.error = SUPERSEDED_ERROR
        job.finished_at = _now()
        self.write(job)
        return job

    def _forget(self, job_id: str) -> _Waiting | None:
        waiting = self._waiting.pop(job_id, None)
        if waiting is not None and self._by_key.get(waiting.key) == job_id:
            del self._by_key[waiting.key]
        return waiting

    def claim(self) -> Job | None:
        with self._lock:
            if not self._waiting:
                return None
            waiting = self._forget(next(iter(self._waiting)))
            assert waiting is not None
            job = waiting.job
            job.state = "running"
            job.started_at = _now()
            self.write(job)
            self._running.add(job.id)
            return job.claimed(1)

    def heartbeat(self, job: Job) -> None:
        pass

    def finish(self, job: Job) -> bool:
        with self._lock:
            self._running.discard(job.id)
            self.write(job)
        return True

    def counts(self) -> QueueCounts:
        with self._lock:
            first = next(iter(self._waiting.values()), None)
            return QueueCounts(
                pending=len(self._waiting),
                running=len(self._running),
                oldest_pending=first.job.created_at if first is not None else None,
            )

    def prune(self, ttl: float, *, now: datetime | None = None) -> list[str]:
        cutoff = (now or _now()).timestamp() - ttl
        removed: list[str] = []
        for job in self.list_jobs():
            if job.state in ("pending", "running"):
                continue  # still wanted; a restart fails it before it can go stale
            stamp = job.finished_at or job.created_at
            if stamp.timestamp() < cutoff:
                self.delete(job.id)
                removed.append(job.id)
        return removed
