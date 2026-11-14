"""The intake worker's activities (#6133, spec 2026-11-13 §7). The worker holds no database
secret and no store credential: it reaches the CRUD service's ``intake`` routes with its
service credential, and the bucket only through the URLs those routes presign.

`IntakeActivities.check`:

1. asks for the staged file (``POST …/intake/sources/{id}/read``): a presigned GET, for
   5 minutes, or a 409 when the source no longer waits for a check (then it does nothing);
2. downloads it to its scratch disk, counting bytes (at most the declared size) and
   hashing them;
3. checks it in a child process (`runner.py`, `check.py`);
4. for a ready file, asks for a presigned PUT of its preview (``…/preview``) and PUTs it;
5. posts the verdict (``PUT …/check``) with a key that is the same on each retry.

A refusal is a verdict, posted like a pass. A check that cannot finish (the child timed
out or crashed, the download failed) raises, so Temporal tries it again; `failed` posts
``failed`` once the workflow gives up.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Final

import httpx
from temporalio import activity

from scadbuddy.crud.client import CrudClient, CrudRefusedError, activity_key
from scadbuddy.crud.wire import PREFIX
from scadbuddy.intake_worker.runner import run_check
from scadbuddy.intake_worker.wire import (
    CHECK_ACTIVITY,
    FAILED_ACTIVITY,
    CheckResult,
    PreviewAsk,
    PreviewSlot,
    SourceRead,
)

BASE: Final = f"/api/v1{PREFIX}/intake/sources"
#: The problem type's last part a read answers for a source no longer waiting for a check.
NOT_CHECKING: Final = "not_checking"
_CHUNK: Final = 1 << 20
#: The bucket's answers: a download or a PUT that takes longer fails the attempt.
TRANSFER_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)


class _SizeMismatchError(Exception):
    pass


async def _download(http: httpx.AsyncClient, read: SourceRead, to: Path) -> str:
    """The frozen copy into ``to``, at most its size in bytes; its SHA-256. ``If-Match``
    names its ETag: S3 answers 412 for any other object, and the attempt fails."""
    digest = hashlib.sha256()
    size = read.size
    taken = 0
    async with http.stream("GET", read.url, headers={"If-Match": read.etag}) as response:
        response.raise_for_status()
        with to.open("wb") as out:
            async for chunk in response.aiter_bytes(_CHUNK):
                taken += len(chunk)
                if taken > size:
                    raise _SizeMismatchError
                digest.update(chunk)
                out.write(chunk)
    if taken != size:
        raise _SizeMismatchError
    return digest.hexdigest()


class IntakeActivities:
    def __init__(
        self,
        crud: CrudClient,
        work_dir: Path,
        http: httpx.AsyncClient | None = None,
        *,
        sandbox: bool = False,
    ) -> None:
        self._crud = crud
        self._work_dir = work_dir
        #: The check's child runs in the pod's sandbox container (`runner.py`).
        self._sandbox = sandbox
        self._http = http or httpx.AsyncClient(timeout=TRANSFER_TIMEOUT)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, source_id: str, result: CheckResult) -> None:
        await self._crud.request(
            "PUT",
            f"{BASE}/{source_id}/check",
            json=result.model_dump(mode="json"),
            idempotency_key=activity_key(1),
        )

    @activity.defn(name=CHECK_ACTIVITY)
    async def check(self, source_id: str) -> str:
        """The check (the module docstring); its outcome, or ``skipped``."""
        try:
            response = await self._crud.request("POST", f"{BASE}/{source_id}/read")
        except CrudRefusedError as refusal:
            if refusal.status in (404, 409):
                activity.logger.info("source %s no longer waits for a check", source_id)
                return "skipped"
            raise
        read = SourceRead.model_validate(response.json())
        with tempfile.TemporaryDirectory(dir=self._work_dir, prefix="check-") as scratch:
            folder = Path(scratch)
            file = folder / "upload.bin"
            try:
                sha256 = await _download(self._http, read, file)
            except _SizeMismatchError:
                result = CheckResult(
                    outcome="refused", reason="The file is not the size it was sent as."
                )
            else:
                verdict = await run_check(file, read.filename, folder, sandbox=self._sandbox)
                if verdict.preview is None:
                    result = CheckResult(outcome="refused", reason=verdict.reason)
                else:
                    png = verdict.preview.read_bytes()
                    asked = await self._crud.request(
                        "POST",
                        f"{BASE}/{source_id}/preview",
                        json=PreviewAsk(size=len(png)).model_dump(),
                    )
                    slot = PreviewSlot.model_validate(asked.json())
                    put = await self._http.put(slot.url, content=png, headers=slot.headers)
                    put.raise_for_status()
                    result = CheckResult(outcome="ready", sha256=sha256, facts=verdict.facts)
        await self._post(source_id, result)
        return result.outcome

    @activity.defn(name=FAILED_ACTIVITY)
    async def failed(self, source_id: str) -> str:
        """``failed`` posted once the check gave up; the shop can run it again."""
        await self._post(
            source_id, CheckResult(outcome="failed", reason="The check could not finish.")
        )
        return "failed"
