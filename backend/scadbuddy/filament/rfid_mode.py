"""The RFID override mode (#6264): while it is on, Bambuddy's ``auto_add_unknown_rfid`` is
off, so Bambuddy does not make a spool of a tag it does not know. The value Bambuddy had
is saved before the first write and put back when the mode turns off.

Every step is state-based: read Bambuddy first, write only a difference."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.errors import SCOPE_PROBLEM, Scope
from scadbuddy.bambuddy.models import SpoolBuddyDevice
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import models as m
from scadbuddy.filament.rfid_push import error_label
from scadbuddy.library.settings_store import StoredSettings

Conn = Connection[DictRow]

_COLUMNS = "override_mode, saved_auto_add, blocked, changed_at"
#: The detail of a mode that is on while no Bambuddy is configured: nothing to retry.
NO_BAMBUDDY = "no Bambuddy"
BLOCKED_DETAIL = (
    "Bambuddy's auth is on, and no API key can change its settings: turn its auth off, "
    "or turn off auto-add of unknown RFID spools in Bambuddy"
)


class RfidModeStore:
    """The ``filament_rfid_state`` row over a pool whose connections autocommit and
    return dict rows (the settings store's). Synchronous: callers run it in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    def state(self) -> m.RfidState:
        with self._pool.connection() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM filament_rfid_state").fetchone()
        assert row is not None  # the migration inserts the one row
        return m.RfidState.model_validate(row)

    def set_mode(self, enabled: bool) -> m.RfidState:
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE filament_rfid_state SET override_mode = %s, changed_at = now()"
                f" WHERE override_mode IS DISTINCT FROM %s RETURNING {_COLUMNS}",
                (enabled, enabled),
            ).fetchone()
        return self.state() if row is None else m.RfidState.model_validate(row)

    def save_found(self, value: bool) -> bool:
        """Keep ``value`` as the one to restore, unless one is kept already or the mode is
        off (a mode that turned off since it was read must not keep a value)."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE filament_rfid_state SET saved_auto_add = %s"
                " WHERE saved_auto_add IS NULL AND override_mode IS TRUE",
                (value,),
            )
        return cur.rowcount == 1

    def clear_saved(self, restored: bool) -> bool:
        """Drop the saved value after ``restored`` was put back, unless the mode turned on
        since (it then still owns the value) or the saved value is another one."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE filament_rfid_state SET saved_auto_add = NULL"
                " WHERE override_mode IS FALSE AND saved_auto_add IS NOT DISTINCT FROM %s",
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
        state=kind,  # type: ignore[arg-type]
        bambuddy_auto_add=seen,
        saved_auto_add=state.saved_auto_add,
        detail=detail,
    )


async def reconcile_mode(store: RfidModeStore, client: BambuddyClient) -> m.RfidStatus:
    """Bring Bambuddy's ``auto_add_unknown_rfid`` to what the mode says. A refused or
    failed call never clears the saved value and never raises (a database error does)."""
    state = await asyncio.to_thread(store.state)
    seen: bool | None = None
    try:
        if state.override_mode:
            seen = await client.auto_add_unknown_rfid()
            # Saved before any write, so a crash after the write still has the original.
            await asyncio.to_thread(store.save_found, seen)
            if seen:
                state = await asyncio.to_thread(store.state)
                if state.saved_auto_add is None:
                    # The mode turned off since it was read, and nothing is kept to
                    # restore: a write now would leave auto-add off with the mode off.
                    return _status(state, "pending" if state.override_mode else "off", seen=seen)
                await client.set_auto_add_unknown_rfid(False)
                seen = await client.auto_add_unknown_rfid()
            await asyncio.to_thread(store.set_blocked, False)
            state = await asyncio.to_thread(store.state)
            return _status(state, "applied" if seen is False else "pending", seen=seen)
        if state.saved_auto_add is None:
            return _status(state, "off")
        seen = await client.auto_add_unknown_rfid()
        if seen != state.saved_auto_add:
            if (await asyncio.to_thread(store.state)).override_mode:
                # Turned on since it was read: its own reconcile owns the value now.
                return _status(await asyncio.to_thread(store.state), "pending", seen=seen)
            await client.set_auto_add_unknown_rfid(state.saved_auto_add)
            seen = state.saved_auto_add
        await asyncio.to_thread(store.clear_saved, state.saved_auto_add)
        await asyncio.to_thread(store.set_blocked, False)
        state = await asyncio.to_thread(store.state)
        return _status(state, "pending" if state.override_mode else "off", seen=seen)
    except ApiError as exc:
        blocked = _blocked(exc)
        await asyncio.to_thread(store.set_blocked, blocked)
        state = await asyncio.to_thread(store.state)  # the saved value may be new
        if blocked:
            return _status(state, "blocked", seen=seen, detail=BLOCKED_DETAIL)
        return _status(state, "pending", seen=seen, detail=error_label(exc))


async def reconcile_mode_now(
    store: RfidModeStore, load: Callable[[], StoredSettings]
) -> m.RfidStatus:
    """`reconcile_mode` with a client of its own. A mode that is off with nothing saved
    needs no Bambuddy; one that cannot reach it (none configured) stays ``pending``."""
    state = await asyncio.to_thread(store.state)
    if not state.override_mode and state.saved_auto_add is None:
        return _status(state, "off")
    try:
        settings = await asyncio.to_thread(load)
        if not settings.bambuddy_url:
            return _status(state, "pending", detail=NO_BAMBUDDY)
        async with client_for(settings) as client:
            return await reconcile_mode(store, client)
    except ApiError as exc:
        return _status(state, "pending", detail=error_label(exc))


async def read_status(store: RfidModeStore, load: Callable[[], StoredSettings]) -> m.RfidStatus:
    """The mode as it stands, with Bambuddy's value and the SpoolBuddy devices where
    Bambuddy answers. Writes nothing; a Bambuddy error leaves those fields empty."""
    state = await asyncio.to_thread(store.state)
    seen: bool | None = None
    devices: list[SpoolBuddyDevice] = []
    detail: str | None = None
    try:
        settings = await asyncio.to_thread(load)
        if not settings.bambuddy_url:
            detail = NO_BAMBUDDY
        else:
            async with client_for(settings) as client:
                seen = await client.auto_add_unknown_rfid()
                devices = await _devices(client)
    except ApiError as exc:
        detail = error_label(exc)
    if state.override_mode:
        kind = "applied" if seen is False else "pending"
    else:
        kind = "off" if state.saved_auto_add is None else "pending"
    if kind == "pending" and state.blocked and detail is None:
        kind, detail = "blocked", BLOCKED_DETAIL
    status = _status(state, kind, seen=seen, detail=detail)
    status.devices = devices
    return status


async def _devices(client: BambuddyClient) -> list[SpoolBuddyDevice]:
    try:
        return await client.spoolbuddy_devices()
    except ApiError:
        return []
