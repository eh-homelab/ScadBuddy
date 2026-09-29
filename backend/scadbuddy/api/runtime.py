"""Putting the stored settings into effect (#322).

``AppState.settings`` is what this process is running with. At start the whole of the
store's resolution is applied, before anything is sized from it; after that only the
fields ``APPLIES`` calls ``live``, on every ``settings.changed`` from any replica. A
``restart`` field whose stored value differs from the running one is listed by
:func:`restart_required` until the next start.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from scadbuddy.api.deps import AppState, set_previews
from scadbuddy.core.events import Event, SettingsChanged
from scadbuddy.core.pg_events import EventLogRetention, PgNotifyEventBus
from scadbuddy.core.settings import APPLIES, ENV_SEEDED, Settings
from scadbuddy.render.previews import TIMEOUT_FACTOR

logger = logging.getLogger(__name__)

LIVE = tuple(name for name in ENV_SEEDED if APPLIES[name] == "live")
RESTART = tuple(name for name in ENV_SEEDED if APPLIES[name] == "restart")


def apply_runtime(state: AppState, values: Settings, *, booting: bool = False) -> None:
    """Run with ``values``: every env-seeded field when ``booting``, else the live ones."""
    names = ENV_SEEDED if booting else LIVE
    running = state.settings.model_copy(update={name: getattr(values, name) for name in names})
    config = running.to_config()
    state.settings = running
    state.config = config
    state.queue.reconfigure(config)
    state.assets.max_total_bytes = config.asset_max_total_bytes
    state.assets.max_count = config.asset_max_count
    state.libraries.max_bytes = config.library_max_bytes
    state.catalogue.duplicate_staging_max_age = config.duplicate_staging_max_age
    state.fonts.catalogue_ttl = config.fonts_catalogue_ttl
    state.fonts.use_api_key(config.google_fonts_api_key)
    if isinstance(state.events, PgNotifyEventBus):
        state.events.retention = EventLogRetention(
            seconds=running.event_log_retention_seconds, rows=running.event_log_retention_rows
        )
    logging.getLogger().setLevel(running.log_level)
    if booting:
        # Nothing has taken a permit or started a preview yet.
        state.checks = asyncio.Semaphore(config.check_concurrency)
        state.language_servers = asyncio.Semaphore(config.lsp_sessions)
        state.realtime_sockets = asyncio.Semaphore(config.realtime_sockets)
        set_previews(state, running.preview_renders)
    if state.previews is not None:
        state.previews.timeout = config.render_timeout * TIMEOUT_FACTOR


def restart_required(state: AppState, values: Settings) -> list[str]:
    """The fields saved with a value this process does not run with until it restarts."""
    return [name for name in RESTART if getattr(values, name) != getattr(state.settings, name)]


def follow_changes(state: AppState) -> Callable[[], None]:
    """Re-read the store and apply the live fields on every ``settings.changed`` heard,
    this replica's own included (applying twice is harmless)."""
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()

    async def reapply() -> None:
        try:
            snapshot = await asyncio.to_thread(state.settings_store.snapshot)
        except Exception:
            logger.exception("could not re-read the settings after a change")
            return
        apply_runtime(state, snapshot.runtime)

    def schedule() -> None:
        task = loop.create_task(reapply())
        pending.add(task)
        task.add_done_callback(pending.discard)

    def on_event(event: Event) -> None:
        if isinstance(event, SettingsChanged) and event.section == "connection":
            # The loop may have closed first, at shutdown.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(schedule)

    remove: Callable[[], None] = state.events.add_listener(on_event)
    return remove
