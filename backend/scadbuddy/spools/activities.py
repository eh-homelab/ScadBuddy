"""The spool refresh's activities (#3818 tier A, plan 2026-10-18 Task 6b), on the
``library`` worker (`main._run_library_worker`): the providers' names, then each
provider's refresh as an activity of its own, then what the printers' trays show. The
print dialog reads only what they wrote (`SpoolStore.overlay`); nothing in it starts,
signals or waits on a refresh.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from temporalio import activity

from scadbuddy.spools.component import Spools
from scadbuddy.workflows.spool_refresh import LIST_ACTIVITY, PROVIDER_ACTIVITY, SEEN_ACTIVITY

logger = logging.getLogger(__name__)

#: How long one provider's whole refresh may take (asking whether it is configured,
#: its read and its write) before it counts as failed.
PROVIDER_TIMEOUT = 60.0

Outcome = Literal["refreshed", "failed", "skipped"]


def _now() -> datetime:
    return datetime.now(UTC)


async def refresh_provider(
    spools: Spools, name: str, *, now: Callable[[], datetime] = _now
) -> Outcome:
    """Refresh one provider. Any failure in it, its write's too, is that provider's:
    recorded as failed (its type only, never its message), its rows kept."""
    provider = next(p for p in spools.providers if p.name == name)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + PROVIDER_TIMEOUT

    async def step() -> Outcome:
        if not await provider.configured():
            return "skipped"
        records = await provider.records()
        # The write ends by the same deadline, so it never commits after the provider
        # was recorded as failed (`SpoolStore.replace_records`).
        await spools.store.replace_records(name, records, now(), timeout=deadline - loop.time())
        return "refreshed"

    try:
        return await asyncio.wait_for(step(), PROVIDER_TIMEOUT)
    except Exception as error:
        failure = type(error).__name__
        logger.warning(
            "could not refresh a spool provider", extra={"provider": name, "error": failure}
        )
        await spools.store.failed(name, now(), failure)
        return "failed"


async def record_seen(spools: Spools) -> None:
    """What every printer's trays show (`spools/seen.py`), when a tray source is set."""
    if spools.trays is not None:
        await spools.store.seen(await spools.trays.latest())


def spool_activities(spools: Spools) -> list[Callable[..., Any]]:
    @activity.defn(name=LIST_ACTIVITY)
    async def spool_providers_list() -> list[str]:
        return [provider.name for provider in spools.providers]

    @activity.defn(name=PROVIDER_ACTIVITY)
    async def spool_provider_refresh(name: str) -> Outcome:
        return await refresh_provider(spools, name)

    @activity.defn(name=SEEN_ACTIVITY)
    async def spool_trays_seen() -> None:
        try:
            await record_seen(spools)
        except Exception:
            logger.exception("could not record what the printers' trays show")
            raise

    return [spool_providers_list, spool_provider_refresh, spool_trays_seen]
