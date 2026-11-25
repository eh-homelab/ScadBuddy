"""The RFID override mode (#6264): while it is on, Bambuddy's ``auto_add_unknown_rfid`` is
off, so Bambuddy does not make a spool of a tag it does not know. The value Bambuddy had
is saved before the first write and put back when the mode turns off.

ScadBuddy's own setting `AUTO_ADD_SETTING` (#7156, R-7156-1 to R-7156-3), off, holds the
same value off: no system makes a spool of an unknown tag. The two holds share the one
saved value, which goes back only once neither holds. The setting is stored only and read
in SQL, in the same statements as the mode, so the guards below see both at once.

Every step is state-based: read Bambuddy first, write only a difference."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Final

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.errors import SCOPE_PROBLEM, Scope
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import models as m
from scadbuddy.filament.rfid_push import acquire_lock, error_label
from scadbuddy.library.settings_store import StoredSettings

Conn = Connection[DictRow]

#: ScadBuddy's setting (#7156): ``false`` holds Bambuddy's auto-add off. Stored only (no
#: row reads as on), as `filament/shopping.py`'s switch.
AUTO_ADD_SETTING: Final = "filament_auto_add_unknown_rfid"
#: Whether the setting holds, as SQL: only a stored JSON ``false`` is off.
_SETTING_OFF = (
    "COALESCE((SELECT value = 'false'::jsonb FROM settings"
    f" WHERE name = '{AUTO_ADD_SETTING}'), false)"
)
#: Either hold, as SQL.
_HOLDS = f"(override_mode OR {_SETTING_OFF})"
_COLUMNS = f"override_mode, saved_auto_add, blocked, changed_at, {_SETTING_OFF} AS auto_add_off"
#: The detail of a mode that is on while no Bambuddy is configured: nothing to retry.
NO_BAMBUDDY = "no Bambuddy"
#: The detail of a reconcile that found another one running: the holder does the work.
BUSY = "busy"
#: The session advisory lock (hashed with ``hashtextextended``) one `reconcile_mode` holds,
#: across processes, from its state read to its last row write.
RFID_MODE_LOCK = "scadbuddy:filament:rfid_mode"
#: How long a PUT waits for that lock before it answers ``pending`` (``busy``).
RFID_MODE_LOCK_WAIT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.25
BLOCKED_DETAIL = (
    "Bambuddy's auth is on, and no API key can change its settings: turn its auth off, "
    "or turn off auto-add of unknown RFID spools in Bambuddy"
)


class RfidModeStore:
    """The ``filament_rfid_state`` row over a pool whose connections autocommit and
    return dict rows (the settings store's). Synchronous: callers run it in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    @property
    def conninfo(self) -> str:
        """The database the pool serves, for the lock's connection of its own."""
        conninfo = self._pool.conninfo
        return conninfo() if callable(conninfo) else conninfo

    def state(self) -> m.RfidState:
        with self._pool.connection() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM filament_rfid_state").fetchone()
        assert row is not None  # the migration inserts the one row
        return m.RfidState.model_validate(row)

    def set_mode(self, enabled: bool) -> m.RfidState:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE filament_rfid_state SET override_mode = %s, changed_at = now()"
                " WHERE override_mode IS DISTINCT FROM %s",
                (enabled, enabled),
            )
        return self.state()

    def save_found(self, value: bool) -> bool:
        """Keep ``value`` as the one to restore, unless one is kept already or nothing
        holds (a hold that ended since it was read must not keep a value)."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE filament_rfid_state SET saved_auto_add = %s"
                f" WHERE saved_auto_add IS NULL AND {_HOLDS}",
                (value,),
            )
        return cur.rowcount == 1

    def clear_saved(self, restored: bool) -> bool:
        """Drop the saved value after ``restored`` was put back, unless a hold began since
        (it then still owns the value) or the saved value is another one."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE filament_rfid_state SET saved_auto_add = NULL"
                f" WHERE NOT {_HOLDS} AND saved_auto_add IS NOT DISTINCT FROM %s",
                (restored,),
            )
        return cur.rowcount == 1

    def set_blocked(self, blocked: bool) -> None:
        with self._pool.connection() as conn:
            conn.execute("UPDATE filament_rfid_state SET blocked = %s", (blocked,))


def _blocked(exc: ApiError) -> bool:
    """Bambuddy refused the settings write for the key's scope (its auth is on)."""
    return exc.type == SCOPE_PROBLEM and exc.extensions.get("required_scope") == (
        Scope.ADMIN_SETTINGS.value
    )


def _status(
    state: m.RfidState,
    kind: str,
    *,
    seen: bool | None = None,
    detail: str | None = None,
) -> m.RfidStatus:
    return m.RfidStatus(
        override_mode=state.override_mode,
        auto_add_unknown_rfid=not state.auto_add_off,
        state=kind,  # type: ignore[arg-type]
        bambuddy_auto_add=seen,
        saved_auto_add=state.saved_auto_add,
        detail=detail,
    )


async def reconcile_mode(store: RfidModeStore, client: BambuddyClient) -> m.RfidStatus:
    """Bring Bambuddy's ``auto_add_unknown_rfid`` to what the mode and the setting say. A
    refused or failed call never clears the saved value and never raises (a database error
    does)."""
    state = await asyncio.to_thread(store.state)
    seen: bool | None = None
    try:
        if state.holds:
            seen = await client.auto_add_unknown_rfid()
            # Saved before any write, so a crash after the write still has the original.
            await asyncio.to_thread(store.save_found, seen)
            if seen:
                state = await asyncio.to_thread(store.state)
                if state.saved_auto_add is None:
                    # The hold ended since it was read, and nothing is kept to restore: a
                    # write now would leave auto-add off with nothing holding it.
                    return _status(state, "pending" if state.holds else "off", seen=seen)
                await client.set_auto_add_unknown_rfid(False)
                seen = await client.auto_add_unknown_rfid()
            await asyncio.to_thread(store.set_blocked, False)
            state = await asyncio.to_thread(store.state)
            return _status(state, "applied" if seen is False else "pending", seen=seen)
        if state.saved_auto_add is None:
            return _status(state, "off")
        seen = await client.auto_add_unknown_rfid()
        if seen != state.saved_auto_add:
            if (await asyncio.to_thread(store.state)).holds:
                # A hold began since it was read: its own reconcile owns the value now.
                return _status(await asyncio.to_thread(store.state), "pending", seen=seen)
            await client.set_auto_add_unknown_rfid(state.saved_auto_add)
            seen = state.saved_auto_add
        await asyncio.to_thread(store.clear_saved, state.saved_auto_add)
        await asyncio.to_thread(store.set_blocked, False)
        state = await asyncio.to_thread(store.state)
        return _status(state, "pending" if state.holds else "off", seen=seen)
    except ApiError as exc:
        blocked = _blocked(exc)
        await asyncio.to_thread(store.set_blocked, blocked)
        state = await asyncio.to_thread(store.state)  # the saved value may be new
        if blocked:
            return _status(state, "blocked", seen=seen, detail=BLOCKED_DETAIL)
        return _status(state, "pending", seen=seen, detail=error_label(exc))


async def _lock(store: RfidModeStore, wait: float) -> psycopg.Connection[Any] | None:
    """Try the lock, again every `_LOCK_POLL_SECONDS` for up to ``wait`` seconds."""
    deadline = time.monotonic() + wait
    while True:
        conn = await acquire_lock(store.conninfo, RFID_MODE_LOCK)
        if conn is not None or time.monotonic() >= deadline:
            return conn
        await asyncio.sleep(_LOCK_POLL_SECONDS)


async def reconcile_mode_now(
    store: RfidModeStore, load: Callable[[], StoredSettings], *, wait: float = 0.0
) -> m.RfidStatus:
    """`reconcile_mode` with a client of its own, serialised across processes by
    `RFID_MODE_LOCK`. It tries the lock for ``wait`` seconds (once, by default); where
    another holds it, the answer is ``pending`` with detail `BUSY` and nothing is called.
    A mode that is off with nothing saved needs no Bambuddy; one with no Bambuddy
    configured stays ``pending`` (`NO_BAMBUDDY`)."""
    state = await asyncio.to_thread(store.state)
    if not state.holds and state.saved_auto_add is None:
        return _status(state, "off")
    try:
        settings = await asyncio.to_thread(load)
        if not settings.bambuddy_url:
            return _status(state, "pending", detail=NO_BAMBUDDY)
        lock = await _lock(store, wait)
        if lock is None:
            return _status(state, "pending", detail=BUSY)
        try:
            async with client_for(settings) as client:
                return await reconcile_mode(store, client)
        finally:
            await asyncio.to_thread(lock.close)
    except ApiError as exc:
        return _status(state, "pending", detail=error_label(exc))


async def read_status(store: RfidModeStore, load: Callable[[], StoredSettings]) -> m.RfidStatus:
    """The mode as it stands, with Bambuddy's value where Bambuddy answers.
    Writes nothing; a Bambuddy error leaves that field empty."""
    state = await asyncio.to_thread(store.state)
    seen: bool | None = None
    detail: str | None = None
    try:
        settings = await asyncio.to_thread(load)
        if not settings.bambuddy_url:
            detail = NO_BAMBUDDY
        else:
            async with client_for(settings) as client:
                seen = await client.auto_add_unknown_rfid()
    except ApiError as exc:
        detail = error_label(exc)
    if state.holds:
        kind = "applied" if seen is False else "pending"
    else:
        kind = "off" if state.saved_auto_add is None else "pending"
    if kind == "pending" and state.blocked and detail is None:
        kind, detail = "blocked", BLOCKED_DETAIL
    return _status(state, kind, seen=seen, detail=detail)
