"""``flow_triggers``, their standing grants and their fires (#2484, spec 2026-10-11 §12).

The routes write who made, changed or paused a trigger from the request's author, never
from its body; a fire reads everything it acts on from here. Revoking a standing grant
revokes the run grants it seeded in the same transaction, so their next outward calls wait
(§3.5); an edit returns it to `requested` (Ruling 20) and leaves runs already started
alone. Approving one is bound to the trigger as the person saw it (`state_hash`), checked
under the trigger's row lock, which every edit takes too. A fire is admitted under the
same lock, so concurrent fires keep to the trigger's overlap and rate cap.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.flow_triggers.models import (
    EventKind,
    Fire,
    FireOutcome,
    StandingGrant,
    Trigger,
    state_hash,
)
from scadbuddy.flows.grants import MAX_EXPIRY, GrantSpec, GrantStatus
from scadbuddy.flows.models import TERMINAL

_TRIGGER = (
    "id, name, definition_id, kind, schedule, event, overlap, inputs, approval_timeout_s,"
    " paused, paused_at, paused_by, paused_reason, created_by, created_at, updated_by,"
    " updated_at,"
    " (SELECT d.version FROM workflow_definitions d WHERE d.id = definition_id) AS version"
)
_GRANT = (
    "id, trigger_id, status, spec, requested_by, approved_by, approved_at, revoked_by,"
    " revoked_at, expires_at, created_at, approval_id, used"
)
#: The trigger's live grant first, else its newest.
_CURRENT_GRANT = (
    f"SELECT {_GRANT} FROM flow_trigger_grants WHERE trigger_id = %s"
    " ORDER BY (status IN ('requested', 'approved') AND revoked_at IS NULL) DESC,"
    " created_at DESC LIMIT 1"
)
#: A fire whose run has no row yet is taken for open only this long: a start that never
#: wrote its row must not hold every later fire back.
STARTING_GRACE = timedelta(minutes=10)
_HOUR = timedelta(hours=1)
#: What an edit may change: never who made it, nor its pause.
_EDITABLE = frozenset(
    {
        "name",
        "definition_id",
        "kind",
        "schedule",
        "event",
        "overlap",
        "inputs",
        "approval_timeout_s",
    }
)


class LiveGrantError(Exception):
    """The trigger has a live standing grant already: one at a time."""


class TriggerChangedError(Exception):
    """The trigger or its standing grant changed since the person saw it."""


class StandingGrantStateError(Exception):
    """The standing grant is not in a state that allows this."""

    def __init__(self, grant: StandingGrant) -> None:
        super().__init__(f"standing grant {grant.id} is {grant.status}")
        self.grant = grant


def _json(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value)


class TriggerStore:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    async def _call[T](self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.to_thread(fn, *args)

    # -- triggers ---------------------------------------------------------------

    async def insert(self, trigger: Trigger) -> Trigger:
        return await self._call(self._insert, trigger)

    async def get(self, trigger_id: str) -> Trigger | None:
        return await self._call(self._get, trigger_id)

    async def list_triggers(self, name: str | None = None) -> list[Trigger]:
        return await self._call(self._list, name)

    async def update(
        self, trigger_id: str, fields: dict[str, Any], by: dict[str, Any], revoked_by: str
    ) -> Trigger | None:
        """Change `fields` and return the trigger's standing grant to `requested` (Ruling
        20); the run grants it seeded already are left as they are."""
        return await self._call(self._update, trigger_id, fields, by, revoked_by)

    async def set_paused(
        self, trigger_id: str, paused: bool, by: dict[str, Any] | None, reason: str | None
    ) -> Trigger | None:
        return await self._call(self._set_paused, trigger_id, paused, by, reason)

    async def pause_flow(self, name: str, reason: str, by: dict[str, Any]) -> list[Trigger]:
        """Pause every trigger of flow `name` not paused already; the ones it paused."""
        return await self._call(self._pause_flow, name, reason, by)

    async def delete(self, trigger_id: str, revoked_by: str) -> bool:
        return await self._call(self._delete, trigger_id, revoked_by)

    async def event_triggers(self, kind: EventKind, slug: str) -> list[str]:
        """The unpaused event triggers this event fires."""
        return await self._call(self._event_triggers, kind, slug)

    # -- standing grants --------------------------------------------------------

    async def attach_grant(
        self,
        trigger_id: str,
        spec: GrantSpec,
        status: GrantStatus,
        requested_by: dict[str, Any],
        seen: str | None,
    ) -> StandingGrant | None:
        """A new standing grant, `approved` (by the browser) or `requested`; None without
        the trigger. `LiveGrantError` while another is live; `TriggerChangedError` when
        `seen` is not the trigger's `state_hash` now."""
        return await self._call(self._attach_grant, trigger_id, spec, status, requested_by, seen)

    async def current_grant(self, trigger_id: str) -> StandingGrant | None:
        return await self._call(self._current_grant, trigger_id)

    async def approve_grant(
        self, trigger_id: str, spec: GrantSpec | None, seen: str
    ) -> StandingGrant | None:
        """Approve the requested grant, as `spec` when given, only if `seen` is the
        trigger's `state_hash` now (`TriggerChangedError`)."""
        return await self._call(self._decide_grant, trigger_id, spec, True, seen)

    async def decline_grant(self, trigger_id: str) -> StandingGrant | None:
        return await self._call(self._decide_grant, trigger_id, None, False, None)

    async def revoke_grant(self, trigger_id: str, revoked_by: str) -> StandingGrant | None:
        return await self._call(self._revoke_grant, trigger_id, revoked_by)

    async def seed_run_grant(
        self,
        run_id: str,
        trigger_id: str,
        requested_by: dict[str, Any],
        now: datetime,
        checked: str,
    ) -> str | None:
        """The run's own grant from the trigger's standing grant in force (§12.4), under
        that grant's lock so a revocation lands wholly before or after it. Its id, or None
        with no standing grant in force, or when the trigger is no longer the `checked`
        one (`state_hash` without its grant): its grant was approved for something else."""
        return await self._call(
            self._seed_run_grant, run_id, trigger_id, requested_by, now, checked
        )

    # -- fires --------------------------------------------------------------------

    async def claim_fire(
        self, trigger_id: str, fire_key: str, run_id: str, event: dict[str, Any] | None
    ) -> Fire | None:
        """Record the fire as `pending`; the fire already recorded under the key when there
        is one; None when the trigger is gone."""
        return await self._call(self._claim_fire, trigger_id, fire_key, run_id, event)

    async def settle_fire(
        self, trigger_id: str, fire_key: str, outcome: FireOutcome, reason: str | None = None
    ) -> None:
        await self._call(self._settle_fire, trigger_id, fire_key, outcome, reason)

    async def fires(self, trigger_id: str, limit: int = 20) -> list[Fire]:
        """The newest fires first, each with its run's status."""
        return await self._call(self._fires, trigger_id, limit)

    async def fire_of_run(self, run_id: str) -> tuple[Fire, Trigger | None] | None:
        return await self._call(self._fire_of_run, run_id)

    async def admit(
        self, trigger_id: str, fire_key: str, rate_cap: int | None, checked: str
    ) -> str | None:
        """Admit the pending fire under the trigger's row lock (§12.2): None when it may
        start its run (marked `admitted`; one admitted already stays so), else why not:
        `deleted`, `paused`, `overlap` (with `overlap: skip`, another admitted or started
        fire's run is open) or `rate` (`rate_cap` fires admitted or started in the last
        hour), or `changed` when the trigger is no longer the `checked` one
        (`state_hash` without its grant)."""
        return await self._call(self._admit, trigger_id, fire_key, rate_cap, checked)

    # -- implementation -------------------------------------------------------------

    def _insert(self, trigger: Trigger) -> Trigger:
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO flow_triggers (id, name, definition_id, kind, schedule, event,"
                " overlap, inputs, approval_timeout_s, paused, created_by)"
                f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, false, %s) RETURNING {_TRIGGER}",
                (
                    trigger.id,
                    trigger.name,
                    trigger.definition_id,
                    trigger.kind,
                    _json(trigger.schedule and trigger.schedule.model_dump()),
                    _json(trigger.event and trigger.event.model_dump()),
                    trigger.overlap,
                    _json(trigger.inputs),
                    trigger.approval_timeout_s,
                    Jsonb(trigger.created_by),
                ),
            ).fetchone()
        assert row is not None
        return Trigger.model_validate(row)

    def _get(self, trigger_id: str) -> Trigger | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_TRIGGER} FROM flow_triggers WHERE id = %s", (trigger_id,)
            ).fetchone()
        return Trigger.model_validate(row) if row else None

    def _list(self, name: str | None) -> list[Trigger]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_TRIGGER} FROM flow_triggers"
                " WHERE %(name)s::text IS NULL OR name = %(name)s ORDER BY created_at DESC",
                {"name": name},
            ).fetchall()
        return [Trigger.model_validate(row) for row in rows]

    def _update(
        self, trigger_id: str, fields: dict[str, Any], by: dict[str, Any], revoked_by: str
    ) -> Trigger | None:
        if unknown := set(fields) - _EDITABLE:
            raise ValueError(f"not editable: {sorted(unknown)}")
        columns = {
            key: _json(value) if key in ("schedule", "event", "inputs") else value
            for key, value in fields.items()
        }
        sets = "".join(f", {column} = %({column})s" for column in columns)
        with self._pool.connection() as conn, conn.transaction():
            # The row lock an approval checks what it saw under.
            row = conn.execute(
                f"UPDATE flow_triggers SET updated_by = %(by)s, updated_at = now(){sets}"
                f" WHERE id = %(id)s RETURNING {_TRIGGER}",
                {**columns, "by": Jsonb(by), "id": trigger_id},
            ).fetchone()
            if row is None:
                return None
            # Ruling 20: a person approves it again for what the trigger is now. Runs
            # already started keep the grant they were given.
            conn.execute(
                "UPDATE flow_trigger_grants SET status = 'requested', approved_by = NULL,"
                " approved_at = NULL, expires_at = NULL, approval_id = NULL WHERE trigger_id = %s"
                " AND status = 'approved' AND revoked_at IS NULL",
                (trigger_id,),
            )
        return Trigger.model_validate(row)

    def _set_paused(
        self, trigger_id: str, paused: bool, by: dict[str, Any] | None, reason: str | None
    ) -> Trigger | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE flow_triggers SET paused = %s,"
                " paused_at = CASE WHEN %s THEN now() END, paused_by = %s, paused_reason = %s"
                f" WHERE id = %s RETURNING {_TRIGGER}",
                (
                    paused,
                    paused,
                    _json(by) if paused else None,
                    reason if paused else None,
                    trigger_id,
                ),
            ).fetchone()
        return Trigger.model_validate(row) if row else None

    def _pause_flow(self, name: str, reason: str, by: dict[str, Any]) -> list[Trigger]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "UPDATE flow_triggers SET paused = true, paused_at = now(), paused_by = %s,"
                f" paused_reason = %s WHERE name = %s AND NOT paused RETURNING {_TRIGGER}",
                (Jsonb(by), reason, name),
            ).fetchall()
        return [Trigger.model_validate(row) for row in rows]

    def _delete(self, trigger_id: str, revoked_by: str) -> bool:
        with self._pool.connection() as conn, conn.transaction():
            # The trigger first, as every other writer locks it: then its grants.
            if self._locked_trigger(conn, trigger_id) is None:
                return False
            self._revoke_live(conn, trigger_id, revoked_by)
            # The run grants it seeded, revoked above, keep naming it after the delete.
            deleted = conn.execute("DELETE FROM flow_triggers WHERE id = %s", (trigger_id,))
        return deleted.rowcount > 0

    def _event_triggers(self, kind: EventKind, slug: str) -> list[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM flow_triggers WHERE kind = 'event' AND NOT paused"
                " AND event ->> 'kind' = %s"
                " AND (event ->> 'slug' IS NULL OR event ->> 'slug' = %s)",
                (kind, slug),
            ).fetchall()
        return [row["id"] for row in rows]

    def _attach_grant(
        self,
        trigger_id: str,
        spec: GrantSpec,
        status: GrantStatus,
        requested_by: dict[str, Any],
        seen: str | None,
    ) -> StandingGrant | None:
        approved = status == "approved"
        try:
            with self._pool.connection() as conn, conn.transaction():
                trigger = self._locked_trigger(conn, trigger_id)
                if trigger is None:
                    return None
                if self._live_grant(conn, trigger_id) is not None:
                    raise LiveGrantError(trigger_id)
                # An approved attach is bound to what was seen; a request is not
                # (a person approves it later against what they see then).
                if approved and (not seen or seen != state_hash(trigger, None)):
                    raise TriggerChangedError(trigger_id)
                row = conn.execute(
                    "INSERT INTO flow_trigger_grants (id, trigger_id, status, spec, requested_by,"
                    " approved_by, approved_at, expires_at, approval_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, %s, %s)"
                    f" RETURNING {_GRANT}",
                    (
                        str(uuid.uuid4()),
                        trigger_id,
                        status,
                        Jsonb(spec.model_dump(mode="json", by_alias=True)),
                        Jsonb(requested_by),
                        "browser" if approved else None,
                        approved,
                        spec.expires_at if approved else None,
                        str(uuid.uuid4()) if approved else None,
                    ),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise LiveGrantError(trigger_id) from None
        assert row is not None
        return StandingGrant.model_validate(row)

    def _locked_trigger(self, conn: Connection[DictRow], trigger_id: str) -> Trigger | None:
        row = conn.execute(
            f"SELECT {_TRIGGER} FROM flow_triggers WHERE id = %s FOR UPDATE", (trigger_id,)
        ).fetchone()
        return Trigger.model_validate(row) if row else None

    def _live_grant(self, conn: Connection[DictRow], trigger_id: str) -> StandingGrant | None:
        row = conn.execute(
            f"SELECT {_GRANT} FROM flow_trigger_grants WHERE trigger_id = %s"
            " AND status IN ('requested', 'approved') AND revoked_at IS NULL FOR UPDATE",
            (trigger_id,),
        ).fetchone()
        return StandingGrant.model_validate(row) if row else None

    def live_hash(self, trigger: Trigger) -> str:
        """`state_hash` of the trigger with its live standing grant, as a view shows it."""
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_GRANT} FROM flow_trigger_grants WHERE trigger_id = %s"
                " AND status IN ('requested', 'approved') AND revoked_at IS NULL",
                (trigger.id,),
            ).fetchone()
        return state_hash(trigger, StandingGrant.model_validate(row) if row else None)

    def _current_grant(self, trigger_id: str) -> StandingGrant | None:
        with self._pool.connection() as conn:
            row = conn.execute(_CURRENT_GRANT, (trigger_id,)).fetchone()
        return StandingGrant.model_validate(row) if row else None

    def _decide_grant(
        self, trigger_id: str, spec: GrantSpec | None, approve: bool, seen: str | None
    ) -> StandingGrant | None:
        with self._pool.connection() as conn, conn.transaction():
            trigger = self._locked_trigger(conn, trigger_id)
            if trigger is None:
                return None
            row = conn.execute(_CURRENT_GRANT + " FOR UPDATE", (trigger_id,)).fetchone()
            if row is None:
                return None
            grant = StandingGrant.model_validate(row)
            if grant.status != "requested" or grant.revoked_at is not None:
                raise StandingGrantStateError(grant)
            # Approving is bound to what the person saw (§12.4); declining only removes.
            if approve and (not seen or seen != state_hash(trigger, grant)):
                raise TriggerChangedError(trigger_id)
            final = spec or grant.spec
            row = conn.execute(
                "UPDATE flow_trigger_grants SET status = %s, spec = %s,"
                " approved_by = CASE WHEN %s THEN 'browser' END,"
                " approved_at = CASE WHEN %s THEN now() END, expires_at = %s,"
                " approval_id = %s"
                f" WHERE id = %s RETURNING {_GRANT}",
                (
                    "approved" if approve else "declined",
                    Jsonb(final.model_dump(mode="json", by_alias=True)),
                    approve,
                    approve,
                    final.expires_at if approve else None,
                    str(uuid.uuid4()) if approve else None,
                    grant.id,
                ),
            ).fetchone()
        assert row is not None
        return StandingGrant.model_validate(row)

    def _revoke_grant(self, trigger_id: str, revoked_by: str) -> StandingGrant | None:
        with self._pool.connection() as conn, conn.transaction():
            if self._locked_trigger(conn, trigger_id) is None:
                return None
            row = conn.execute(_CURRENT_GRANT + " FOR UPDATE", (trigger_id,)).fetchone()
            if row is None:
                return None
            grant = StandingGrant.model_validate(row)
            if grant.revoked_at is not None or grant.status == "declined":
                raise StandingGrantStateError(grant)
            self._revoke(conn, grant.id, revoked_by)
            row = conn.execute(
                f"SELECT {_GRANT} FROM flow_trigger_grants WHERE id = %s", (grant.id,)
            ).fetchone()
        assert row is not None
        return StandingGrant.model_validate(row)

    def _revoke_live(self, conn: Connection[DictRow], trigger_id: str, revoked_by: str) -> None:
        rows = conn.execute(
            "SELECT id FROM flow_trigger_grants WHERE trigger_id = %s"
            " AND status IN ('requested', 'approved') AND revoked_at IS NULL FOR UPDATE",
            (trigger_id,),
        ).fetchall()
        for row in rows:
            self._revoke(conn, row["id"], revoked_by)

    @staticmethod
    def _revoke(conn: Connection[DictRow], grant_id: str, revoked_by: str) -> None:
        """The standing grant and every run grant it seeded, in the caller's transaction."""
        conn.execute(
            "UPDATE flow_trigger_grants SET revoked_at = now(), revoked_by = %s WHERE id = %s",
            (revoked_by, grant_id),
        )
        conn.execute(
            "UPDATE flow_grants SET revoked_at = now(), revoked_by = %s"
            " WHERE standing_grant_id = %s AND revoked_at IS NULL",
            (revoked_by, grant_id),
        )

    def _seed_run_grant(
        self,
        run_id: str,
        trigger_id: str,
        requested_by: dict[str, Any],
        now: datetime,
        checked: str,
    ) -> str | None:
        with self._pool.connection() as conn, conn.transaction():
            trigger = self._locked_trigger(conn, trigger_id)
            if trigger is None:
                return None
            # Fails closed: no hash, or another, matches nothing.
            if not checked or checked != state_hash(trigger, None):
                return None
            row = conn.execute(
                f"SELECT {_GRANT} FROM flow_trigger_grants WHERE trigger_id = %s"
                " AND status = 'approved' AND revoked_at IS NULL AND expires_at > %s"
                " ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
                (trigger_id, now),
            ).fetchone()
            if row is None:
                return None
            standing = StandingGrant.model_validate(row)
            assert standing.expires_at is not None
            expires = min(standing.expires_at, now + MAX_EXPIRY)
            # No starter session to ask; its limits are the standing grant's, spent
            # under this approval (`standing_approval_id`) across every run it seeds.
            spec = standing.spec.model_copy(
                update={"expires_at": expires, "ask_starter_session": False}
            )
            grant_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO flow_grants (id, run_id, status, spec, requested_by, approved_by,"
                " approved_at, expires_at, standing_grant_id, standing_approval_id)"
                " VALUES (%s, %s, 'approved', %s, %s, %s, now(), %s, %s, %s)",
                (
                    grant_id,
                    run_id,
                    Jsonb(spec.model_dump(mode="json", by_alias=True)),
                    Jsonb(requested_by),
                    f"trigger-grant:{standing.id}",
                    expires,
                    standing.id,
                    standing.approval_id,
                ),
            )
        return grant_id

    def _claim_fire(
        self, trigger_id: str, fire_key: str, run_id: str, event: dict[str, Any] | None
    ) -> Fire | None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO flow_trigger_fires (trigger_id, fire_key, run_id, outcome, event)"
                " SELECT %s, %s, %s, 'pending', %s"
                " WHERE EXISTS (SELECT 1 FROM flow_triggers WHERE id = %s)"
                " ON CONFLICT DO NOTHING",
                (trigger_id, fire_key, run_id, _json(event), trigger_id),
            )
            row = conn.execute(
                "SELECT f.*, NULL AS run_status FROM flow_trigger_fires f"
                " WHERE trigger_id = %s AND fire_key = %s",
                (trigger_id, fire_key),
            ).fetchone()
        return Fire.model_validate(row) if row else None

    def _settle_fire(
        self, trigger_id: str, fire_key: str, outcome: FireOutcome, reason: str | None
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE flow_trigger_fires SET outcome = %s, reason = %s"
                " WHERE trigger_id = %s AND fire_key = %s",
                (outcome, reason, trigger_id, fire_key),
            )

    def _fires(self, trigger_id: str, limit: int) -> list[Fire]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT f.*, r.status AS run_status FROM flow_trigger_fires f"
                " LEFT JOIN workflow_runs r ON r.id = f.run_id AND f.outcome = 'started'"
                " WHERE f.trigger_id = %s ORDER BY f.fired_at DESC LIMIT %s",
                (trigger_id, limit),
            ).fetchall()
        return [Fire.model_validate(row) for row in rows]

    def _fire_of_run(self, run_id: str) -> tuple[Fire, Trigger | None] | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT f.*, NULL AS run_status FROM flow_trigger_fires f"
                " WHERE run_id = %s AND outcome = 'started'",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return Fire.model_validate(row), self._get(row["trigger_id"])

    def _admit(
        self, trigger_id: str, fire_key: str, rate_cap: int | None, checked: str
    ) -> str | None:
        with self._pool.connection() as conn, conn.transaction():
            trigger = self._locked_trigger(conn, trigger_id)
            if trigger is None:
                return "deleted"
            if trigger.paused:
                return "paused"
            # Fails closed: no hash, or another, matches nothing.
            if not checked or checked != state_hash(trigger, None):
                return "changed"
            fire = conn.execute(
                "SELECT outcome FROM flow_trigger_fires WHERE trigger_id = %s AND fire_key = %s",
                (trigger_id, fire_key),
            ).fetchone()
            if fire is not None and fire["outcome"] in ("admitted", "started"):
                return None  # a retry of a fire admitted already, neither paused nor changed since
            others = conn.execute(
                "SELECT f.fired_at, r.status, now() - f.fired_at AS age"
                " FROM flow_trigger_fires f LEFT JOIN workflow_runs r ON r.id = f.run_id"
                " WHERE f.trigger_id = %s AND f.fire_key <> %s"
                " AND f.outcome IN ('admitted', 'started')",
                (trigger_id, fire_key),
            ).fetchall()
            if trigger.overlap == "skip" and any(
                (o["status"] is None and o["age"] < STARTING_GRACE)
                or (o["status"] is not None and o["status"] not in TERMINAL)
                for o in others
            ):
                return "overlap"
            if rate_cap is not None and sum(o["age"] < _HOUR for o in others) >= rate_cap:
                return "rate"
            conn.execute(
                "UPDATE flow_trigger_fires SET outcome = 'admitted'"
                " WHERE trigger_id = %s AND fire_key = %s",
                (trigger_id, fire_key),
            )
        return None
