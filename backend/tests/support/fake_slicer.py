"""A Python `slice_plate` in the Rust slicer worker's place (plan
2026-10-16-native-slicer-rust-worker Task 11): it GETs every input URL as the worker
does, heartbeats a `SliceProgress`, PUTs a sliced 3MF to the output URL and answers
with its digest, or fails the ways `sb-slicer`'s `error.rs` does. A URL on the API's
internal base reaches the app through ASGI; any other (S3's presigned GET, moto's
server here) goes out over HTTP."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import httpx
from fastapi.testclient import TestClient
from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.worker import Worker

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.slicing.models import (
    SLICER_ERROR,
    TRANSFER_REFUSED,
    VALIDATION_EXIT,
    SlicePlateInput,
    SlicePlateOutput,
    SliceProgress,
    TransferUrl,
)
from tests.support.sliced3mf import sliced_3mf

#: Where the api tests' app says the worker reaches it (`api_internal_url`).
INTERNAL = "http://scadbuddy"
#: What the fake uploads as the sliced 3MF.
SLICED = sliced_3mf(plates={1: (3600, [12.5, 3.0])}, gcode={1: b"M620 S0A\nM620 S1A\nM620 S0A\n"})
PROGRESS = SliceProgress(
    plate_index=0, plate_count=1, plate_percent=40, total_percent=40, message="Slicing"
)


class FakeSlicer:
    def __init__(self, app: Any) -> None:
        self._app = app
        #: Refuse every call as Bambu Studio's validation exit (192), never retried.
        self.refuse_plate = False
        #: Fail this many calls first with a retryable `SlicerError` (a crash).
        self.crash_times = 0
        #: Fail this many calls first with a `SlicerWorkerError` carrying a message.
        self.worker_errors = 0
        #: Refuse this many calls' URLs first as expired (`TransferRefused`).
        self.expire_times = 0
        #: Fail as cancelled with no cancel asked of it, as `sb-slicer` returns
        #: `ActivityError::cancelled()` whenever its context is cancelled. Unlike Rust core,
        #: which reports one for its worker's shutdown as a `WorkerShutdown` failure, the
        #: Python SDK sends it as is, and the server leaves the attempt to its heartbeat
        #: timeout (#4403).
        self.self_cancel = False
        #: Report a digest other than the one it PUT.
        self.wrong_sha = False
        #: Waited on after the heartbeat, before the upload.
        self.hold: asyncio.Event | None = None
        #: The sliced 3MF it uploads.
        self.sliced = SLICED
        #: A plate's own sliced 3MF, by the plate a call slices, over ``sliced``
        #: (#4250: a quote slices every plate of an output).
        self.sliced_by_plate: dict[int, bytes] = {}
        self.calls = 0
        #: Each call's input, as the slicer worker got it.
        self.inputs: list[SlicePlateInput] = []
        #: What each successful call downloaded: the input 3MF, then each preset.
        self.downloads: list[list[bytes]] = []

    async def _get(self, url: TransferUrl) -> bytes:
        async with self._http(url) as http:
            response = await http.get(url.url, headers=url.headers)
        response.raise_for_status()
        return response.content

    def _http(self, url: TransferUrl) -> httpx.AsyncClient:
        if url.url.startswith(INTERNAL):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=self._app))
        return httpx.AsyncClient()

    @activity.defn(name="slice_plate")
    async def slice_plate(self, input: SlicePlateInput) -> SlicePlateOutput:
        self.calls += 1
        self.inputs.append(input)
        if self.expire_times > 0:
            self.expire_times -= 1
            raise ApplicationError("expired", type=TRANSFER_REFUSED, non_retryable=True)
        if self.self_cancel:
            raise CancelledError("the worker is shutting down")
        if self.refuse_plate:
            raise ApplicationError(
                "validation failed",
                {"exit_code": VALIDATION_EXIT, "stderr_tail": "plate 99 is empty"},
                type=SLICER_ERROR,
                non_retryable=True,
            )
        if self.crash_times > 0:
            self.crash_times -= 1
            raise ApplicationError(
                "the slicer exited with code 139",
                {"exit_code": 139, "stderr_tail": "segfault"},
                type=SLICER_ERROR,
            )
        if self.worker_errors > 0:
            self.worker_errors -= 1
            raise ApplicationError("secret detail", type="SlicerWorkerError")
        urls = [
            input.input_3mf,
            input.machine_preset,
            input.process_preset,
            *input.filament_presets,
        ]
        downloaded = [await self._get(url) for url in urls]
        activity.heartbeat(PROGRESS)
        if self.hold is not None:
            await self.hold.wait()
        sliced = self.sliced_by_plate.get(input.plate, self.sliced)
        async with self._http(input.sliced_3mf) as http:
            response = await http.put(
                input.sliced_3mf.url, headers=input.sliced_3mf.headers, content=sliced
            )
        response.raise_for_status()
        self.downloads.append(downloaded)
        return SlicePlateOutput(
            sliced_sha256=hashlib.sha256(b"other" if self.wrong_sha else sliced).hexdigest(),
            sliced_size=len(sliced),
            warnings=["a warning"],
            exit_code=0,
            elapsed_seconds=1.5,
        )


@contextmanager
def fake_slicer_worker(client: TestClient, queue: str) -> Iterator[FakeSlicer]:
    """A worker serving the fake on ``queue``, run in the app's portal beside its
    in-process workers, on the app's own client."""
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    temporal = state.temporal
    assert temporal is not None, "the api tests' app connects eagerly"
    assert client.portal is not None
    fake = FakeSlicer(client.app)

    @asynccontextmanager
    async def serving() -> AsyncIterator[None]:
        async with Worker(temporal, task_queue=queue, activities=[fake.slice_plate]):
            yield

    with client.portal.wrap_async_context_manager(serving()):
        yield fake
