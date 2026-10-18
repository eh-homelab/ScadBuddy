"""The spool refresh's one activity (#3818 tier A, plan 2026-10-18 Task 6b), on the
``library`` worker (`main._run_library_worker`): every configured provider's records
into Postgres, then what the printers' trays show. The print dialog reads only what
this wrote (`SpoolStore.overlay`); nothing in it starts, signals or waits on a refresh.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from temporalio import activity

from scadbuddy.spools.component import Spools
from scadbuddy.workflows.spool_refresh import REFRESH_ACTIVITY

logger = logging.getLogger(__name__)

#: How long one provider's read may take before it counts as failed: each provider
#: is read in turn, so a hung one would hold the rest back.
PROVIDER_TIMEOUT = 60.0

Outcome = Literal["refreshed", "failed", "skipped"]


def _now() -> datetime:
    return datetime.now(UTC)


async def refresh_providers(
    spools: Spools, *, now: Callable[[], datetime] = _now
) -> dict[str, Outcome]:
    """Refresh each provider in turn; what came of each. A provider that fails is
    recorded as failed (its type only, never its message) and its rows are kept."""
    outcomes: dict[str, Outcome] = {}
    for provider in spools.providers:
        try:
            if not await provider.configured():
                outcomes[provider.name] = "skipped"
                continue
            records = await asyncio.wait_for(provider.records(), PROVIDER_TIMEOUT)
        except Exception as error:
            failure = type(error).__name__
            logger.warning(
                "could not refresh a spool provider",
                extra={"provider": provider.name, "error": failure},
            )
            await spools.store.failed(provider.name, now(), failure)
            outcomes[provider.name] = "failed"
            continue
        await spools.store.replace_records(provider.name, records, now())
        outcomes[provider.name] = "refreshed"
    if spools.trays is not None:
        await spools.store.seen(await spools.trays.latest())
    return outcomes


def spool_activities(spools: Spools) -> list[Callable[..., Any]]:
    @activity.defn(name=REFRESH_ACTIVITY)
    async def spool_providers_refresh() -> dict[str, Outcome]:
        return await refresh_providers(spools)

    return [spool_providers_refresh]
