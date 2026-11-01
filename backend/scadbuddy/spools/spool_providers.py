"""ScadBuddy's own inventory as a spool provider (#4643, tier B of #3818; plan 2026-10-18
D1 names it the second provider, after Bambuddy's, `bambuddy/spool_providers.py`).

Its records are ``native_spools`` (`spools/native.py`), which a person keeps; the refresh
(`workflows/spool_refresh.py`) copies the unarchived ones into ``spool_provider_records``
as every provider's are, and a write through ``/api/v1/native-spools`` refreshes it at
once (`api/native_spools.py`), so the overlay never shows an edit late. It reads
Postgres only: a shop with no Bambuddy has shelf spools and remaining grams with it.

A native print takes what it used off these records (#4643 part 2): each LAN start
saves its slots' grams against the native record the overlay put in each tray
(`native.spool_uses`, ``lan_print``), and :func:`native_use_hook`, a read hook of the
print follow, settles them once the run's print settles, then re-caches the records.

It is first (priority 10, Bambuddy's is 100): a tray's remaining weight is the first
fresh record's (`SpoolProvider.priority`), and the shop's own count is the one it keeps.
It is configured while any spool is on its shelf, so a shop that keeps none never sees
a note that its records are stale. It places no spool itself (`printer_key` is
``None``): an untagged tray finds a native record by a person's tray link.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scadbuddy.core.components import Components
from scadbuddy.printers.models import PrinterRow
from scadbuddy.spools.models import SpoolSnapshot
from scadbuddy.spools.native import NativeSpoolStore
from scadbuddy.spools.providers import ProviderCore, SpoolProvider, SpoolProviderBuild

if TYPE_CHECKING:
    from scadbuddy.bambuddy.follow import ReadHook
    from scadbuddy.bambuddy.progress import PrintProgress
    from scadbuddy.spools.component import Spools

logger = logging.getLogger(__name__)

NAME = "native"


class NativeProvider:
    name = NAME
    label = "ScadBuddy"
    priority = 10

    def __init__(self, store: NativeSpoolStore) -> None:
        self.store = store

    async def configured(self) -> bool:
        return await asyncio.to_thread(self.store.any)

    async def records(self) -> list[SpoolSnapshot]:
        return [spool.snapshot() for spool in await asyncio.to_thread(self.store.list)]

    def printer_key(self, row: PrinterRow) -> str | None:
        return None


def native_provider(spools: Spools) -> NativeProvider:
    """The native provider among ``spools.providers``; every process that builds the
    providers has it."""
    return next(p for p in spools.providers if isinstance(p, NativeProvider))


async def recache(spools: Spools) -> None:
    """The native provider's records into its cache now (`SpoolStore.replace_records`, as
    the refresh does), even with the shelf empty, which the Schedule's refresh skips
    (`NativeProvider.configured`). A failure is recorded as the provider's, as the
    Schedule's would be, and never raised: what changed the records stands."""
    provider = native_provider(spools)
    try:
        records = await provider.records()
        await spools.store.replace_records(NAME, records, datetime.now(UTC))
    except Exception as error:
        failure = type(error).__name__
        logger.warning(
            "could not refresh ScadBuddy's own spools", extra={"provider": NAME, "error": failure}
        )
        try:
            await spools.store.failed(NAME, datetime.now(UTC), failure)
        except Exception:
            logger.warning("could not record the native spools' failed refresh")


def native_use_hook(spools: Spools) -> ReadHook:
    """The print follow's read hook for what a native print used (#4643): once a LAN
    run's print has settled, each of its starts that ended ``done`` takes its grams off
    its native spools (`NativeSpoolStore.settle_uses`), once whichever reader settles it
    first, and the records are re-cached for the overlay. Advisory: a failure is logged,
    never raised into the follow. Reads and writes ScadBuddy's Postgres only."""

    async def hook(run_subject: str, progress: PrintProgress) -> None:
        if progress.route != "lan" or not progress.settled or progress.run_id is None:
            return
        stages = [copy.stage for copy in progress.copies_detail] or [progress.stage]
        store = native_provider(spools).store
        try:
            changed = await asyncio.to_thread(store.settle_uses, progress.run_id, stages)
        except Exception as error:
            logger.warning(
                "could not take a native print's filament off its spools",
                extra={"run_id": progress.run_id, "error": type(error).__name__},
            )
            return
        if changed:
            await recache(spools)

    return hook


def _build(core: ProviderCore, components: Components | None) -> SpoolProvider:
    return NativeProvider(NativeSpoolStore(core.settings_store.pool))


SPOOL_PROVIDERS: list[SpoolProviderBuild] = [_build]
