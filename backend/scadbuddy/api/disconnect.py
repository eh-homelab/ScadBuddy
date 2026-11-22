"""Stop a route's database work when its caller goes away (#6902).

A CRUD route whose work runs in a thread (``asyncio.to_thread``) keeps running after the
client's read timeout: Starlette does not cancel the endpoint when the client
disconnects (`api/limits.py` `unless_the_client_leaves`), and a thread cannot be
cancelled. On prod (2026-11-21) the sync's ``plan`` and ``results`` calls ran on after the
print worker's 10 s timeout, and its retries ran beside them.

`until_disconnect` runs the work with a `CancelToken` and waits for it or for the
client's ``http.disconnect``. When the client has gone, or the route's own task is
cancelled, it cancels the token (`core/cancel.py`: the running statement is cancelled
and the next step raises, so the work's transaction rolls back), waits for the work to
end, so its connection is back in the pool and its locks are free, and answers 503
`CALLER_GONE`, which nobody reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from fastapi import Request, status

from scadbuddy.api.limits import _client_hung_up
from scadbuddy.core.cancel import CancelToken
from scadbuddy.core.problems import ApiError

logger = logging.getLogger(__name__)

#: The problem's ``reason`` when the caller went away.
CALLER_GONE = "caller_gone"


async def until_disconnect[T](request: Request, work: Callable[[CancelToken], Awaitable[T]]) -> T:
    """``work(token)``'s answer, as the module says."""
    token = CancelToken()
    task = asyncio.ensure_future(work(token))
    watcher = asyncio.ensure_future(_client_hung_up(request))
    try:
        await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        # The route's own task (a shutdown): stop the work, and leave it to roll back.
        watcher.cancel()
        asyncio.get_running_loop().run_in_executor(None, token.cancel)
        raise
    if task.done():
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        return task.result()
    logger.warning(
        "the caller of a CRUD route went away; its work is cancelled",
        extra={"path": request.url.path},
    )
    await asyncio.to_thread(token.cancel)
    with contextlib.suppress(Exception):
        await task
    raise ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "the caller went away; the work was cancelled and rolled back",
        reason=CALLER_GONE,
    )
