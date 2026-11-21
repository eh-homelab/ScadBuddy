"""The Bambuddy pull in one process, for the pull's tests (#6772): the print worker's read
(`filament/bb_pull_run.py`) sending to the API's apply (`filament/bb_pull_apply.py`
`LocalBbPull`), as ``FilamentBbPull``'s activity runs them where the API serves the
``bambuddy`` queue. The route between them is `tests/crud/test_crud_filament_pull.py`'s."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import bb_pull_run
from scadbuddy.filament.bambuddy_pull import ACTOR, EVERY, PullReport, Pulls
from scadbuddy.filament.bb_pull_apply import (
    LocalBbPull,
    new_pull_id,
    pull_start,
    remember_actor,
)
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.filament.component import Filament
from scadbuddy.library.settings_store import StoredSettings


async def read_and_pull(
    store: Filament, settings: StoredSettings, *, actor: str = ACTOR, pulls: Pulls = EVERY
) -> PullReport:
    """`bb_pull_run.read_and_pull` of ``settings``' Bambuddy into ``store``; an
    ``actor`` other than the Schedule's is kept as the import route keeps it."""
    pull_id = new_pull_id()
    if actor != ACTOR:
        remember_actor(store, pull_id, actor)
    return await bb_pull_run.read_and_pull(
        LocalBbPull(store, lambda: settings),
        settings,
        instance=instance_for(settings.bambuddy_url or ""),
        pull_id=pull_id,
        pulls=pulls,
    )


async def scheduled_pull(
    store: Filament, load: Callable[[], StoredSettings], sync: Callable[[], FilamentSync]
) -> str:
    """The spool refresh's pull: what the API decides (`pull_start`), then the worker's
    run (`bb_pull_run.run_pull`); its ``pulled``, ``skipped`` or ``failed``."""
    start = await pull_start(store, load, sync)
    if start is None:
        return "skipped"

    async def loaded() -> StoredSettings:
        return load()

    return (await bb_pull_run.run_pull(LocalBbPull(store, load), loaded, start)).outcome


async def read_ha_sensors(store: Filament, settings: StoredSettings, now: datetime) -> None:
    """`bb_pull_run.read_ha_sensors`, its readings recorded at ``now``."""
    await bb_pull_run.read_ha_sensors(
        LocalBbPull(store, lambda: settings, clock=lambda: now),
        settings,
        instance_for(settings.bambuddy_url or ""),
    )
