"""The edge's sign-out retries (#5283, review of #5953).

A sign-out the CRUD side did not answer still signs the device out (the edge clears
``sb_session`` and gives a new visitor), but the session's row stays valid until it
expires (`SESSION_MAX_AGE`, 30 days). So the edge answers 503 ``sign_out_incomplete``,
never success, and tries the revocation again in the background: `SIGN_OUT_RETRY_DELAYS`
(five tries over about ten minutes), each try and the give-up logged.

The queue is in memory and bounded (`SIGN_OUT_RETRIES_MAX`): a full queue takes no more,
and the route logs the sign-out "not queued". An edge restart drops the queue; a session it
held then ends at its expiry.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS

logger = logging.getLogger(__name__)

SIGN_OUT_INCOMPLETE: Final = f"{STOREFRONT_PROBLEMS}sign_out_incomplete"
SIGN_OUT_INCOMPLETE_TEXT: Final = (
    "You are signed out on this device. The shop could not end the session yet,"
    " and it keeps trying."
)
#: The answer when the queue was full: no retry is promised.
SIGN_OUT_NOT_QUEUED_TEXT: Final = (
    "You are signed out on this device. The shop could not end the session;"
    " it ends on its own within 30 days."
)
#: Seconds before each try: five tries, 600 s in all.
SIGN_OUT_RETRY_DELAYS: Final = (30.0, 60.0, 120.0, 180.0, 210.0)
SIGN_OUT_RETRIES_MAX: Final = 1000


class SignOutRetries:
    """The revocations still to send, one task each, at most ``limit`` at once.
    ``sign_out`` is `StorefrontCrud.account_sign_out`."""

    def __init__(
        self,
        sign_out: Callable[..., Awaitable[None]],
        *,
        delays: tuple[float, ...] = SIGN_OUT_RETRY_DELAYS,
        limit: int = SIGN_OUT_RETRIES_MAX,
    ) -> None:
        self._sign_out = sign_out
        self._delays = delays
        self._limit = limit
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def add(self, *, visitor_id: str, client: str, session: str) -> bool:
        """Queue a revocation; False when the queue is full (the session then expires)."""
        if len(self._tasks) >= self._limit:
            # The caller logs the warning ("not queued"): one line per sign-out.
            logger.debug("sign-out retries: the queue is full")
            return False
        task = asyncio.create_task(self._retry(visitor_id, client, session))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _retry(self, visitor_id: str, client: str, session: str) -> None:
        tries = len(self._delays)
        for n, delay in enumerate(self._delays, start=1):
            await asyncio.sleep(delay)
            try:
                await self._sign_out(visitor_id=visitor_id, client=client, session=session)
            except CrudUnavailableError as error:
                logger.warning("sign-out retry %d of %d: no answer (%s)", n, tries, error)
                continue
            except CrudRefusedError as error:
                logger.error("sign-out retry %d of %d: refused (%s); stopping", n, tries, error)
                return
            except Exception:
                # Every give-up is logged: the task's exception is never read.
                logger.exception("sign-out retry %d of %d failed; stopping", n, tries)
                return
            logger.info("sign-out retry %d of %d ended the session", n, tries)
            return
        logger.warning(
            "sign-out retries gave up after %d tries; the session ends at its expiry", tries
        )

    async def aclose(self) -> None:
        """Cancel every queued retry (the edge is stopping)."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
