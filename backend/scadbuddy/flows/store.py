"""``workflow_definitions`` and ``workflow_runs`` (#1057, spec 2026-10-01 §7.3).

A definition is one row per version (spec 2026-10-11-flow-grants-and-git-design.md
§6.2): its script is a commit in ``data/flows`` (``flows/repository.py``), and the row is
inserted under that repository's write lock, with the version git numbered it
(``flows/definitions.py``). A run's row is written only by the run's own
workflow (plan 2026-10-09-durable-phase-6-flows.md Ruling 7), each write with its
``flow_run.changed`` event in the same transaction, and only by the sweep otherwise
(``mark_terminated``, ``follow_run``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.runs import TransactionalEvents
from scadbuddy.core.events import FlowRunEvent
from scadbuddy.flows.grants import Consumption, Grant, GrantSpec, GrantStatus, GrantUse, RuleUse
from scadbuddy.flows.models import (
    RESULT_MAX,
    TERMINAL,
    BackfillRow,
    Decision,
    Proposal,
    Run,
    StoredDefinition,
)
from scadbuddy.workflows.flow_models import (
    FlowRecord,
    FlowStep,
    FlowWaiting,
    Hop,
    HopRecord,
    ProjectionWrite,
)

_DECISION = (
    "request_id, run_id, workflow_run_id, call_id, kind, outcome, response, responder,"
    " input_hash, hops, created_at"
)
_DEFINITION = "id, name, version, commit, script, approval_timeout_s, created_by, created_at"
_GRANT = (
    "id, run_id, status, spec, used, requested_by, approved_by, approved_at, revoked_by,"
    " revoked_at, expires_at, created_at"
)
_GRANT_USE = (
    "request_id, grant_id, run_id, fn, tool, input_hash, rule_index, prints, filament_g,"
    " session_id, narrowed_by, pins, created_at"
)
_RUN = (
    "id, definition_id, version, name, status, waiting_on, steps, result,"
    " result_truncated, approval_timeout_s, commit, inputs, workflow_id, workflow_run_id,"
    " started_by, created_at, updated_at"
)


#: How long the row stays locked waiting for Temporal's Reset to answer. The operation
#: around it (`operation_activities._heartbeating`) heartbeats every 5 s while this
#: thread waits, so a slow RPC never trips the run's 30 s `RUN_HEARTBEAT` (#2303); the
#: wait is bounded by this, not by the heartbeat.
RESET_CALL_S = 30.0
#: The resolved entries a row keeps for a later Reset (#2268). Past this the oldest go,
#: and a Reset to before the last of them is refused (`reset_floor`).
DONE_KEPT = 200


class ResetSupersededError(Exception):
    """A different Reset replaced the execution first; ``run`` is the row as it moved."""

    def __init__(self, run: Run) -> None:
        super().__init__(f"run {run.id} was reset by another Reset first")
        self.run = run


class GrantStateError(Exception):
    """The grant is not in a state that allows this (one grant per run, fixed once
    approved, revocation final: amendment Ruling 6)."""

    def __init__(self, grant: Grant, wanted: str) -> None:
        super().__init__(f"grant {grant.id} is {grant.status}; it cannot be {wanted}")
        self.grant = grant


class HopMovedError(Exception):
    """The parked call is no longer at the hop the decision was made for (#2482): the
    starter session's hop ended, or a person decided it first."""


#: `approval_expiry_seconds` (the agent's Settings → Assistant) when it is not set.
DEFAULT_APPROVAL_EXPIRY_S = 600


def author_principal(owner_id: str) -> str:
    """`owner_id` as the agent writes it in the author header (agent
    `tools/authorship.ts` `principalHeader`): printable ASCII as it is, anything else
    percent-encoded as UTF-8, at most 300 characters, a whole character at a time."""
    out = ""
    for c in owner_id:
        encoded = c if 0x21 <= ord(c) <= 0x7E else quote(c, safe="", errors="replace")
        if len(out) + len(encoded) > 300:
            break
        out += encoded
    return out or "unknown"


#: The agent's tiers, lowest first (agent `auth/principal.ts` `TIERS`).
TIERS = ("read", "write", "outward")
APPROVAL_EXPIRY_SETTING = "approval_expiry_seconds"


class ResetBeforeFloorError(Exception):
    """A Reset to a point whose parked entries the row no longer keeps (#2268)."""

    def __init__(self, floor: int) -> None:
        super().__init__(f"a Reset must be to a point after event {floor + 1}")
        self.floor = floor


def _trimmed(done: list[FlowWaiting], floor: int | None) -> tuple[list[FlowWaiting], int | None]:
    """`done` cut to `DONE_KEPT`, the earliest resolved going first, and the floor that
    leaves: the latest history length a dropped entry was resolved at. An entry with no
    resolved length (written before 6e) is never dropped."""
    excess = len(done) - DONE_KEPT
    if excess <= 0:
        return done, floor
    dated = sorted((w for w in done if w.resolved_at is not None), key=lambda w: w.resolved_at or 0)
    dropped = dated[:excess]
    if not dropped:
        return done, floor
    gone = {id(w) for w in dropped}
    latest = max(w.resolved_at or 0 for w in dropped)
    return [w for w in done if id(w) not in gone], max(latest, floor or 0)


def _done(row: DictRow) -> list[FlowWaiting]:
    """The row's resolved entries."""
    return [FlowWaiting.model_validate(w) for w in row["waiting_done"]]


class FlowStore:
    def __init__(
        self,
        pool: ConnectionPool[Connection[DictRow]],
        *,
        events: TransactionalEvents | None = None,
    ) -> None:
        self._pool = pool
        self.events = events

    # -- definitions ------------------------------------------------------------

    def insert_version(
        self,
        version_id: str,
        name: str,
        version: int,
        commit: str,
        created_by: dict[str, Any],
        *,
        approval_timeout_s: int | None,
        created_at: datetime | None = None,
    ) -> StoredDefinition:
        """A version's row, naming its commit, with the version number git gave it. Run
        under the flows repository's write lock, after the commit."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO workflow_definitions"
                " (id, name, version, commit, approval_timeout_s, created_by, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, now()))"
                f" RETURNING {_DEFINITION}",
                (
                    version_id,
                    name,
                    version,
                    commit,
                    approval_timeout_s,
                    Jsonb(created_by),
                    created_at,
                ),
            ).fetchone()
        assert row is not None
        return StoredDefinition.model_validate(row)

    def version_commits(self) -> dict[str, str | None]:
        """Every version id, and the commit it names (None before its backfill): the
        reconciler's check."""
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT id, commit FROM workflow_definitions").fetchall()
        return {row["id"]: row["commit"] for row in rows}

    def unbackfilled(self) -> list[BackfillRow]:
        """The rows written before flows were in git, in ``(name, version)`` order."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, name, version, script, created_by, created_at"
                " FROM workflow_definitions WHERE commit IS NULL ORDER BY name, version"
            ).fetchall()
        return [BackfillRow.model_validate(row) for row in rows]

    def set_commit(self, definition_id: str, commit: str) -> None:
        """Join a row to its commit, and its runs with it."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "UPDATE workflow_definitions SET commit = %s WHERE id = %s AND commit IS NULL",
                (commit, definition_id),
            )
            conn.execute(
                "UPDATE workflow_runs SET commit = %s WHERE definition_id = %s AND commit IS NULL",
                (commit, definition_id),
            )

    def stored_definition(self, definition_id: str) -> StoredDefinition | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_DEFINITION} FROM workflow_definitions WHERE id = %s",
                (definition_id,),
            ).fetchone()
        return StoredDefinition.model_validate(row) if row else None

    # -- runs -------------------------------------------------------------------

    async def get_run(self, run_id: str) -> Run | None:
        return await asyncio.to_thread(self._get_run, run_id)

    async def reset_floor(self, run_id: str) -> int | None:
        """The history length a Reset's point must be past (#2268); None for any."""
        return await asyncio.to_thread(self._reset_floor, run_id)

    async def list_runs(
        self, *, definition_id: str | None = None, session: str | None = None, limit: int = 50
    ) -> list[Run]:
        """The newest runs first; ``session`` is the agent session that started them."""
        return await asyncio.to_thread(self._list_runs, definition_id, session, limit)

    async def insert_run(self, record: FlowRecord) -> Run:
        """The run's row; the existing one when a retried activity inserts it again,
        announced only when inserted."""
        return await asyncio.to_thread(self._insert_run, record)

    async def project(self, write: ProjectionWrite) -> Run | None:
        """Apply one of the run's own writes. A finished run is not moved again, except
        by a Reset's write. ``None`` when the run has no row."""
        return await asyncio.to_thread(self._project, write)

    async def reset_run(
        self,
        run_id: str,
        *,
        replaced: str,
        point: int,
        request_id: str,
        reset: Callable[[], Coroutine[Any, Any, str]],
    ) -> Run | None:
        """Reset the run's execution `replaced` to event `point` with `reset` (which
        answers the new execution's run id), and move the row with it, holding the row's
        lock throughout: the new execution's first writes wait for it, and a failed
        `reset` leaves the row as it was. Steps started after `point` are dropped, steps
        that ended after it run again, and calls parked at `point` wait again. Writes
        from `replaced` are ignored from now on. ``None`` when the run has no row; the
        row unchanged when this `request_id` reset `replaced` already (a retry), and
        `ResetSupersededError` when a different Reset did.

        A Reset that landed although this call failed (the RPC timed out, or the worker
        died before the commit) leaves the row rolled back while the new execution
        writes to it. The retry's `reset` answers the same execution (Temporal dedupes
        on the request id), and what that execution recorded is kept as it is."""
        loop = asyncio.get_running_loop()

        def call() -> str:
            future = asyncio.run_coroutine_threadsafe(reset(), loop)
            try:
                return future.result(RESET_CALL_S)
            except TimeoutError:
                future.cancel()
                raise

        return await asyncio.to_thread(self._reset_run, run_id, replaced, point, request_id, call)

    async def open_runs(self, older_than: timedelta) -> list[Run]:
        """Runs still ``running`` or ``waiting`` whose row has not moved for
        ``older_than``: the sweep's candidates."""
        return await asyncio.to_thread(self._open_runs, older_than)

    async def mark_terminated(self, run_id: str) -> Run | None:
        """End an open run whose execution closed without finishing it (the sweep)."""
        return await asyncio.to_thread(self._mark_terminated, run_id)

    async def follow_run(self, run_id: str, workflow_run_id: str) -> None:
        """Record the execution's current run id (a Reset the row has not seen yet)."""
        await asyncio.to_thread(self._follow_run, run_id, workflow_run_id)

    async def delete_run(self, run_id: str) -> bool:
        """Delete the run's row; False when there was none."""
        return await asyncio.to_thread(self._delete_run, run_id)

    # -- decisions ---------------------------------------------------------------

    async def record_decision(self, decision: Decision, *, at_hop: Hop | None = None) -> bool:
        """Record a person's answer before the harness is told. True when recorded, or
        when this same decision is there already (a retried run resends it); False when
        another decision holds the request id. With `at_hop`, only while the call waits
        at that hop, checked under the run's row lock: `HopMovedError` otherwise."""
        return await asyncio.to_thread(self._record_decision, decision, at_hop)

    async def get_decision(self, request_id: str) -> Decision | None:
        return await asyncio.to_thread(self._get_decision, request_id)

    async def record_grant_decision(self, decision: Decision) -> bool:
        """Record a run grant's approval of a call (#2481), only if the grant is in force
        now and its recorded use of this request id is for this very input: one statement
        under the grant row's lock, so a revocation lands wholly before or after it (and
        the agent checks the grant again as the call runs). True when recorded, or when it
        is there already; False otherwise."""
        return await asyncio.to_thread(self._record_grant_decision, decision)

    # -- escalation (#2482) -------------------------------------------------------

    async def move_hop(
        self,
        run_id: str,
        call_id: str,
        record: HopRecord,
        *,
        proposal: Proposal | None = None,
    ) -> FlowWaiting | None:
        """Move a parked approval from hop `parent` to `person`, recording `record` on
        it, and keep a narrowed `proposal` beside it, written once per call: under the
        run's row lock, so it happens once. None when the call is not waiting at hop
        `parent`."""
        return await asyncio.to_thread(self._move_hop, run_id, call_id, record, proposal)

    async def get_proposal(self, request_id: str) -> Proposal | None:
        """A parent's narrowed input for the call, its hash, and who proposed it."""
        return await asyncio.to_thread(self._get_proposal, request_id)

    async def parent_tiers(self, session: str, principal: str) -> list[str] | None:
        """The tiers the starter session acts with now (#2482, spec §4.4): the browser
        user's all of them, a token's its live tier (the tiers the agent sends the
        escalation turn with), anyone else's `read`. None when the session is gone,
        archived, or no longer the run starter's (a handoff): it is no parent then."""
        return await asyncio.to_thread(self._parent_tiers, session, principal)

    async def approval_expiry_s(self) -> int:
        """How long a parent hop lasts when the run never times out (spec §4.6): the
        agent's `approval_expiry_seconds`, else its default."""
        return await asyncio.to_thread(self._approval_expiry_s)

    async def delete_decision(self, request_id: str) -> None:
        """Undo a decision the harness refused: no `approved` row for a call it never ran."""
        await asyncio.to_thread(self._delete_decision, request_id)

    async def withdraw_grant_decision(self, request_id: str) -> None:
        """Remove a run grant's own decision for the call (responder `grant:<id>`), never a
        person's: its approval never reached the harness, and a person decides it."""
        await asyncio.to_thread(self._withdraw_grant_decision, request_id)

    # -- grants (#2481) -----------------------------------------------------------

    async def insert_grant(
        self, run_id: str, spec: GrantSpec, *, status: GrantStatus, requested_by: dict[str, Any]
    ) -> Grant:
        """The run's grant, `approved` (by the browser) or `requested`; the existing one
        when the run has one already (a repeated start)."""
        return await asyncio.to_thread(self._insert_grant, run_id, spec, status, requested_by)

    async def get_grant(self, run_id: str) -> Grant | None:
        return await asyncio.to_thread(self._get_grant, run_id)

    async def approve_grant(
        self, run_id: str, spec: GrantSpec | None, *, approved_by: str
    ) -> Grant | None:
        """Approve the run's requested grant, as `spec` when given (the person's edit).
        None without a grant; `GrantStateError` unless it is requested."""
        return await asyncio.to_thread(self._approve_grant, run_id, spec, approved_by)

    async def decline_grant(self, run_id: str) -> Grant | None:
        """Decline the run's requested grant: final."""
        return await asyncio.to_thread(self._decline_grant, run_id)

    async def revoke_grant(self, run_id: str, *, revoked_by: str) -> Grant | None:
        """Revoke the run's grant, requested or approved: final."""
        return await asyncio.to_thread(self._revoke_grant, run_id, revoked_by)

    async def grant_use(self, request_id: str) -> GrantUse | None:
        return await asyncio.to_thread(self._grant_use, request_id)

    async def grant_uses(self, run_id: str) -> list[GrantUse]:
        """What the run's grant let through, oldest first."""
        return await asyncio.to_thread(self._grant_uses, run_id)

    async def consume_grant(
        self, run_id: str, request_id: str, pick: Callable[[Grant], Consumption | str]
    ) -> GrantUse | str:
        """Under the grant's row lock: the use already recorded for `request_id`, or the
        one `pick` chooses against the counters as they are, recorded with them; else
        `pick`'s reason."""
        return await asyncio.to_thread(self._consume_grant, run_id, request_id, pick)

    async def orphan_grants(self, older_than: timedelta) -> list[str]:
        """The run ids of grants older than `older_than` whose run has no row: a start
        that failed after inserting its grant (the sweep checks the execution too)."""
        return await asyncio.to_thread(self._orphan_grants, older_than)

    async def run_talking_to(self, session_id: str, call_id: str) -> Run | None:
        """The run whose step `call_id` talks to session `session_id` (#2481, spec §3.4):
        how a session's call finds its run, from the run's own record."""
        return await asyncio.to_thread(self._run_talking_to, session_id, call_id)

    async def delete_grant(self, run_id: str) -> None:
        await asyncio.to_thread(self._delete_grant, run_id)

    # -- implementation -----------------------------------------------------------

    def _insert_grant(
        self, run_id: str, spec: GrantSpec, status: GrantStatus, requested_by: dict[str, Any]
    ) -> Grant:
        approved = status == "approved"
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO flow_grants (id, run_id, status, spec, requested_by, approved_by,"
                " approved_at, expires_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, %s)"
                f" ON CONFLICT (run_id) DO NOTHING RETURNING {_GRANT}",
                (
                    str(uuid.uuid4()),
                    run_id,
                    status,
                    Jsonb(spec.model_dump(mode="json", by_alias=True)),
                    Jsonb(requested_by),
                    "browser" if approved else None,
                    approved,
                    spec.expires_at if approved else None,
                ),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    f"SELECT {_GRANT} FROM flow_grants WHERE run_id = %s", (run_id,)
                ).fetchone()
        assert row is not None
        return Grant.model_validate(row)

    def _get_grant(self, run_id: str) -> Grant | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_GRANT} FROM flow_grants WHERE run_id = %s", (run_id,)
            ).fetchone()
        return Grant.model_validate(row) if row else None

    def _locked_grant(self, conn: Connection[DictRow], run_id: str) -> Grant | None:
        row = conn.execute(
            f"SELECT {_GRANT} FROM flow_grants WHERE run_id = %s FOR UPDATE", (run_id,)
        ).fetchone()
        return Grant.model_validate(row) if row else None

    def _approve_grant(self, run_id: str, spec: GrantSpec | None, approved_by: str) -> Grant | None:
        with self._pool.connection() as conn, conn.transaction():
            grant = self._locked_grant(conn, run_id)
            if grant is None:
                return None
            if grant.status != "requested" or grant.revoked_at is not None:
                raise GrantStateError(grant, "approved")
            final = spec or grant.spec
            row = conn.execute(
                "UPDATE flow_grants SET status = 'approved', spec = %s, approved_by = %s,"
                f" approved_at = now(), expires_at = %s WHERE id = %s RETURNING {_GRANT}",
                (
                    Jsonb(final.model_dump(mode="json", by_alias=True)),
                    approved_by,
                    final.expires_at,
                    grant.id,
                ),
            ).fetchone()
        assert row is not None
        return Grant.model_validate(row)

    def _decline_grant(self, run_id: str) -> Grant | None:
        with self._pool.connection() as conn, conn.transaction():
            grant = self._locked_grant(conn, run_id)
            if grant is None:
                return None
            if grant.status != "requested" or grant.revoked_at is not None:
                raise GrantStateError(grant, "declined")
            row = conn.execute(
                f"UPDATE flow_grants SET status = 'declined' WHERE id = %s RETURNING {_GRANT}",
                (grant.id,),
            ).fetchone()
        assert row is not None
        return Grant.model_validate(row)

    def _revoke_grant(self, run_id: str, revoked_by: str) -> Grant | None:
        with self._pool.connection() as conn, conn.transaction():
            grant = self._locked_grant(conn, run_id)
            if grant is None:
                return None
            if grant.status == "declined" or grant.revoked_at is not None:
                raise GrantStateError(grant, "revoked")
            row = conn.execute(
                "UPDATE flow_grants SET revoked_by = %s, revoked_at = now() WHERE id = %s"
                f" RETURNING {_GRANT}",
                (revoked_by, grant.id),
            ).fetchone()
        assert row is not None
        return Grant.model_validate(row)

    def _grant_use(self, request_id: str) -> GrantUse | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_GRANT_USE} FROM flow_grant_uses WHERE request_id = %s", (request_id,)
            ).fetchone()
        return GrantUse.model_validate(row) if row else None

    def _grant_uses(self, run_id: str) -> list[GrantUse]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_GRANT_USE} FROM flow_grant_uses WHERE run_id = %s"
                " ORDER BY created_at, request_id",
                (run_id,),
            ).fetchall()
        return [GrantUse.model_validate(row) for row in rows]

    def _consume_grant(
        self, run_id: str, request_id: str, pick: Callable[[Grant], Consumption | str]
    ) -> GrantUse | str:
        with self._pool.connection() as conn, conn.transaction():
            # A run grant a trigger's standing grant seeded (#2484, §12.4) spends the
            # standing grant's limits, across every run it seeded: its row is locked first
            # (as a revocation locks it before the run grants), and its counters are the
            # ones the rules are checked against.
            # It spends only under the very approval that seeded it: one re-approved
            # since (after an edit, maybe with other rules) is another approval, whose
            # counters it may not reach and whose rules it never had.
            seeded = conn.execute(
                "SELECT standing_grant_id, standing_approval_id FROM flow_grants WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            standing = None
            if seeded is not None and seeded["standing_grant_id"] is not None:
                standing = conn.execute(
                    "SELECT id, used, approval_id, (status = 'approved' AND revoked_at IS NULL"
                    " AND expires_at > now()) AS in_force"
                    " FROM flow_trigger_grants WHERE id = %s FOR UPDATE",
                    (seeded["standing_grant_id"],),
                ).fetchone()
                if (
                    standing is None
                    or not standing["in_force"]
                    or standing["approval_id"] is None
                    or standing["approval_id"] != seeded["standing_approval_id"]
                ):
                    return "the trigger's standing grant is not in force for this run"
            grant = self._locked_grant(conn, run_id)
            if grant is None:
                return "no grant in force"
            # After the lock: a concurrent check of the same request id has committed.
            prior = conn.execute(
                f"SELECT {_GRANT_USE} FROM flow_grant_uses WHERE request_id = %s", (request_id,)
            ).fetchone()
            if prior is not None:
                return GrantUse.model_validate(prior)
            approval = standing["approval_id"] if standing is not None else ""
            shared = dict(standing["used"].get(approval, {})) if standing is not None else {}
            checked = grant
            if standing is not None:
                # Its narrowings' counters stay the run's own.
                mine = {k: v for k, v in grant.used.items() if "/" in k}
                rules = {k: RuleUse.model_validate(v) for k, v in shared.items()}
                checked = grant.model_copy(update={"used": {**mine, **rules}})
            taken = pick(checked)
            if isinstance(taken, str):
                return taken
            use = taken.use

            def spend(counters: dict[str, Any], keys: list[str]) -> dict[str, Any]:
                out = dict(counters)
                for key in keys:
                    was = out.get(key, {"prints": 0, "filament_g": 0.0, "calls": 0})
                    out[key] = {
                        "prints": was["prints"] + use.prints,
                        "filament_g": was["filament_g"] + use.filament_g,
                        "calls": was["calls"] + 1,
                    }
                return out

            keys = [k for k in (str(taken.rule_index), taken.narrowed_key) if k]
            used = spend({k: v.model_dump() for k, v in grant.used.items()}, keys)
            conn.execute("UPDATE flow_grants SET used = %s WHERE id = %s", (Jsonb(used), grant.id))
            if standing is not None:
                counters = {**standing["used"], approval: spend(shared, [str(taken.rule_index)])}
                conn.execute(
                    "UPDATE flow_trigger_grants SET used = %s WHERE id = %s",
                    (Jsonb(counters), standing["id"]),
                )
            row = conn.execute(
                "INSERT INTO flow_grant_uses (request_id, grant_id, run_id, fn, tool,"
                " input_hash, rule_index, prints, filament_g, session_id, narrowed_by, pins)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                f" RETURNING {_GRANT_USE}",
                (
                    use.request_id,
                    use.grant_id,
                    use.run_id,
                    use.fn,
                    use.tool,
                    use.input_hash,
                    use.rule_index,
                    use.prints,
                    use.filament_g,
                    use.session_id,
                    use.narrowed_by,
                    Jsonb(use.pins),
                ),
            ).fetchone()
        assert row is not None
        return GrantUse.model_validate(row)

    def _orphan_grants(self, older_than: timedelta) -> list[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT g.run_id FROM flow_grants g"
                " WHERE g.created_at <= now() - %s"
                " AND NOT EXISTS (SELECT 1 FROM workflow_runs r WHERE r.id = g.run_id)"
                " ORDER BY g.created_at",
                (older_than,),
            ).fetchall()
        return [row["run_id"] for row in rows]

    def _run_talking_to(self, session_id: str, call_id: str) -> Run | None:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_RUN} FROM workflow_runs WHERE steps @> %s LIMIT 2",
                (Jsonb([{"call_id": call_id, "session_id": session_id}]),),
            ).fetchall()
        # A call id is a uuid4 the run drew: two runs naming it is no run's.
        return Run.model_validate(rows[0]) if len(rows) == 1 else None

    def _delete_grant(self, run_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM flow_grants WHERE run_id = %s", (run_id,))

    def _record_decision(self, decision: Decision, at_hop: Hop | None = None) -> bool:
        with self._pool.connection() as conn, conn.transaction():
            if at_hop is not None:
                locked = conn.execute(
                    "SELECT waiting_on FROM workflow_runs WHERE id = %s FOR UPDATE",
                    (decision.run_id,),
                ).fetchone()
                waiting = (
                    [FlowWaiting.model_validate(w) for w in locked["waiting_on"]] if locked else []
                )
                if not any(
                    w.call_id == decision.call_id and (w.hop or "person") == at_hop for w in waiting
                ):
                    raise HopMovedError(decision.request_id)
            row = conn.execute(
                "INSERT INTO workflow_run_decisions (request_id, run_id, workflow_run_id,"
                " call_id, kind, outcome, response, responder, input_hash, hops)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (request_id) DO NOTHING RETURNING request_id",
                (
                    decision.request_id,
                    decision.run_id,
                    decision.workflow_run_id,
                    decision.call_id,
                    decision.kind,
                    decision.outcome,
                    Jsonb(decision.response),
                    decision.responder,
                    decision.input_hash,
                    _hops(decision.hops),
                ),
            ).fetchone()
            if row is not None:
                return True
            existing = conn.execute(
                f"SELECT {_DECISION} FROM workflow_run_decisions WHERE request_id = %s",
                (decision.request_id,),
            ).fetchone()
        if existing is None:
            return False
        held = Decision.model_validate(existing)
        return (held.outcome, held.response) == (decision.outcome, decision.response)

    def _get_decision(self, request_id: str) -> Decision | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_DECISION} FROM workflow_run_decisions WHERE request_id = %s",
                (request_id,),
            ).fetchone()
        return Decision.model_validate(row) if row else None

    def _record_grant_decision(self, decision: Decision) -> bool:
        grant_id = decision.responder.removeprefix("grant:")
        with self._pool.connection() as conn, conn.transaction():
            held = conn.execute(
                "SELECT 1 FROM flow_grants g JOIN flow_grant_uses u ON u.grant_id = g.id"
                " WHERE g.id = %s AND g.run_id = %s AND u.request_id = %s"
                " AND u.input_hash = %s AND g.status = 'approved' AND g.revoked_at IS NULL"
                " AND now() < g.expires_at FOR SHARE OF g",
                (grant_id, decision.run_id, decision.request_id, decision.input_hash),
            ).fetchone()
            if held is None:
                return False
            row = conn.execute(
                "INSERT INTO workflow_run_decisions (request_id, run_id, workflow_run_id,"
                " call_id, kind, outcome, response, responder, input_hash, hops)"
                " VALUES (%s, %s, %s, %s, 'approval', 'approved', %s, %s, %s, %s)"
                " ON CONFLICT (request_id) DO NOTHING RETURNING request_id",
                (
                    decision.request_id,
                    decision.run_id,
                    decision.workflow_run_id,
                    decision.call_id,
                    # A parent's narrowing (#2482): the narrowed input, which the gated
                    # body runs only when it hashes to `input_hash`.
                    Jsonb(decision.response),
                    decision.responder,
                    decision.input_hash,
                    _hops(decision.hops),
                ),
            ).fetchone()
            if row is not None:
                return True
            existing = conn.execute(
                "SELECT responder, input_hash FROM workflow_run_decisions WHERE request_id = %s",
                (decision.request_id,),
            ).fetchone()
        return existing is not None and (existing["responder"], existing["input_hash"]) == (
            decision.responder,
            decision.input_hash,
        )

    def _move_hop(
        self,
        run_id: str,
        call_id: str,
        record: HopRecord,
        proposal: Proposal | None,
    ) -> FlowWaiting | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                f"SELECT {_RUN} FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,)
            ).fetchone()
            if row is None:
                return None
            run = Run.model_validate(row)
            index = next(
                (
                    i
                    for i, w in enumerate(run.waiting_on)
                    if w.call_id == call_id and w.kind == "approval" and w.hop == "parent"
                ),
                None,
            )
            if run.status in TERMINAL or index is None:
                return None
            entry = run.waiting_on[index]
            moved = entry.model_copy(
                update={
                    "hop": "person",
                    "hop_until": None,
                    "proposed": entry.proposed or proposal is not None,
                    "hops": [*entry.hops, record],
                }
            )
            waiting = [*run.waiting_on]
            waiting[index] = moved
            if proposal is not None:
                # Once per call: what a person was shown is never replaced under them.
                conn.execute(
                    "INSERT INTO workflow_run_proposals (request_id, run_id, call_id, args,"
                    " input_hash, proposed_by) VALUES (%s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (request_id) DO NOTHING",
                    (
                        f"flow:{run.id}:{run.workflow_run_id}:{call_id}",
                        run.id,
                        call_id,
                        Jsonb(proposal.args),
                        proposal.input_hash,
                        proposal.proposed_by,
                    ),
                )
            updated = conn.execute(
                "UPDATE workflow_runs SET waiting_on = %s, updated_at = now() WHERE id = %s"
                f" RETURNING {_RUN}",
                (Jsonb([w.model_dump(mode="json") for w in waiting]), run_id),
            ).fetchone()
            assert updated is not None
            self._announce(conn, Run.model_validate(updated))
        return moved

    def _get_proposal(self, request_id: str) -> Proposal | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT args, input_hash, proposed_by FROM workflow_run_proposals"
                " WHERE request_id = %s",
                (request_id,),
            ).fetchone()
        return None if row is None else Proposal.model_validate(row)

    def _parent_tiers(self, session: str, principal: str) -> list[str] | None:
        with self._pool.connection() as conn:
            try:
                owner = conn.execute(
                    "SELECT owner_kind, owner_id FROM ai_sessions"
                    " WHERE id::text = %s AND archived_at IS NULL",
                    (session,),
                ).fetchone()
                if owner is None or author_principal(str(owner["owner_id"])) != principal:
                    return None
                if owner["owner_kind"] == "browser":
                    return list(TIERS)
                token = str(owner["owner_id"])
                if owner["owner_kind"] != "bearer" or not token.startswith("token:"):
                    return ["read"]
                held = conn.execute(
                    "SELECT tier FROM ai_mcp_tokens WHERE id::text = %s AND revoked_at IS NULL"
                    " AND (expires_at IS NULL OR expires_at > now())",
                    (token.removeprefix("token:"),),
                ).fetchone()
            except psycopg.errors.UndefinedTable:
                # No agent service has migrated this database: no session is anyone's.
                return None
        if held is None or held["tier"] not in TIERS:
            return []
        return list(TIERS[: TIERS.index(held["tier"]) + 1])

    def _approval_expiry_s(self) -> int:
        with self._pool.connection() as conn:
            try:
                row = conn.execute(
                    "SELECT value FROM ai_settings WHERE key = %s", (APPROVAL_EXPIRY_SETTING,)
                ).fetchone()
            except psycopg.errors.UndefinedTable:
                return DEFAULT_APPROVAL_EXPIRY_S
        value = None if row is None else row["value"]
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return DEFAULT_APPROVAL_EXPIRY_S

    def _delete_decision(self, request_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM workflow_run_decisions WHERE request_id = %s", (request_id,))

    def _withdraw_grant_decision(self, request_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM workflow_run_decisions"
                " WHERE request_id = %s AND starts_with(responder, 'grant:')",
                (request_id,),
            )

    def _announce(self, conn: Connection[DictRow], run: Run) -> None:
        if self.events is not None:
            self.events.publish_in(
                conn,
                FlowRunEvent(run_id=run.id, definition_id=run.definition_id, status=run.status),
            )

    def _get_run(self, run_id: str) -> Run | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_RUN} FROM workflow_runs WHERE id = %s", (run_id,)
            ).fetchone()
        return Run.model_validate(row) if row else None

    def _reset_floor(self, run_id: str) -> int | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT reset_floor FROM workflow_runs WHERE id = %s", (run_id,)
            ).fetchone()
        return None if row is None else row["reset_floor"]

    def _list_runs(self, definition_id: str | None, session: str | None, limit: int) -> list[Run]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_RUN} FROM workflow_runs"
                " WHERE (%(definition)s::text IS NULL OR definition_id = %(definition)s)"
                " AND (%(session)s::text IS NULL OR started_by ->> 'session' = %(session)s)"
                " ORDER BY created_at DESC LIMIT %(limit)s",
                {"definition": definition_id, "session": session, "limit": limit},
            ).fetchall()
        return [Run.model_validate(row) for row in rows]

    def _insert_run(self, record: FlowRecord) -> Run:
        with self._pool.connection() as conn, conn.transaction():
            # A run started before #2503 names no commit: its version's, once backfilled.
            row = conn.execute(
                "INSERT INTO workflow_runs (id, definition_id, version, name, status,"
                " approval_timeout_s, commit, inputs, workflow_id, workflow_run_id, started_by)"
                " VALUES (%s, %s, %s, %s, 'running', %s, COALESCE(NULLIF(%s, ''),"
                " (SELECT commit FROM workflow_definitions WHERE id = %s)), %s, %s, %s, %s)"
                f" ON CONFLICT (id) DO NOTHING RETURNING {_RUN}",
                (
                    record.run_id,
                    record.definition_id,
                    record.version,
                    record.name,
                    record.approval_timeout_s,
                    record.commit,
                    record.definition_id,
                    None if record.inputs is None else Jsonb(record.inputs),
                    record.workflow_id,
                    record.workflow_run_id,
                    Jsonb(record.started_by),
                ),
            ).fetchone()
            if row is not None:
                run = Run.model_validate(row)
                self._announce(conn, run)
                return run
            existing = conn.execute(
                f"SELECT {_RUN} FROM workflow_runs WHERE id = %s", (record.run_id,)
            ).fetchone()
        assert existing is not None  # the conflict was on this id
        return Run.model_validate(existing)

    def _project(self, write: ProjectionWrite) -> Run | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                f"SELECT {_RUN}, waiting_done, reset_floor,"
                " %s = ANY(superseded_run_ids) AS superseded"
                " FROM workflow_runs WHERE id = %s FOR UPDATE",
                (write.workflow_run_id, write.run_id),
            ).fetchone()
            if row is None:
                return None
            run = Run.model_validate(row)
            if run.status in TERMINAL and not write.reset:
                return run
            if row["superseded"]:
                # A write a Reset's replaced execution still had in flight.
                return run
            # Merged here, under the row's lock, never by concatenating JSON in SQL.
            steps = {step.call_id: step for step in run.steps}
            if write.step is not None:
                steps[write.step.call_id] = write.step.model_copy(
                    update={"workflow_run_id": write.workflow_run_id}
                )
            waiting = [w for w in run.waiting_on if w.call_id != write.waiting_remove]
            done = _done(row)
            done += [
                w.model_copy(update={"resolved_at": write.history_length})
                for w in run.waiting_on
                if w.call_id == write.waiting_remove
            ]
            done, floor = _trimmed(done, row["reset_floor"])
            if write.waiting_add is not None and all(
                w.call_id != write.waiting_add.call_id for w in waiting
            ):
                waiting.append(
                    write.waiting_add.model_copy(update={"workflow_run_id": write.workflow_run_id})
                )
            if write.status in ("succeeded", "failed"):
                status: str = write.status
            else:
                status = "waiting" if waiting else "running"
            result, truncated = run.result, run.result_truncated
            if write.result is not None:
                result, truncated = write.result[:RESULT_MAX], len(write.result) > RESULT_MAX
            updated = conn.execute(
                "UPDATE workflow_runs SET status = %s, waiting_on = %s, waiting_done = %s,"
                " reset_floor = %s, steps = %s, result = %s, result_truncated = %s,"
                f" workflow_run_id = %s, updated_at = now() WHERE id = %s RETURNING {_RUN}",
                (
                    status,
                    Jsonb([w.model_dump(mode="json") for w in waiting]),
                    Jsonb([w.model_dump(mode="json") for w in done]),
                    floor,
                    Jsonb([s.model_dump(mode="json") for s in sorted(steps.values(), key=_seq)]),
                    result,
                    truncated,
                    write.workflow_run_id,
                    write.run_id,
                ),
            ).fetchone()
            assert updated is not None
            after = Run.model_validate(updated)
            self._announce(conn, after)
        return after

    def _reset_run(
        self, run_id: str, replaced: str, point: int, request_id: str, reset: Callable[[], str]
    ) -> Run | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                f"SELECT {_RUN}, waiting_done, reset_floor, superseded_run_ids,"
                " reset_request_ids FROM workflow_runs WHERE id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            run = Run.model_validate(row)
            if replaced in row["superseded_run_ids"]:
                by = dict(zip(row["superseded_run_ids"], row["reset_request_ids"], strict=False))
                if by.get(replaced) == request_id:
                    return run
                raise ResetSupersededError(run)
            floor = row["reset_floor"]
            if floor is not None and point - 1 <= floor:
                # An entry parked before the point and resolved after it may be one the
                # row dropped: the Reset could not park it again.
                raise ResetBeforeFloorError(floor)
            current = reset()

            def own(record: FlowStep | FlowWaiting) -> bool:
                # Written by the new execution already: this Reset landed on an earlier
                # attempt whose commit was lost, and the execution has run since.
                return record.workflow_run_id == current

            # Temporal keeps the history up to the reset task's started event and does
            # that task again (with new randomness): what was recorded in it or after
            # it is done again. A step from before 6e has no end length; it counts as
            # ended before the point, since a step that runs again records its end again.
            redone = point - 1
            steps = [
                step
                if own(step)
                or step.ended_history_length is None
                or step.ended_history_length < redone
                else step.model_copy(
                    update={
                        "status": "running",
                        "ended_at": None,
                        "ended_history_length": None,
                        "error": None,
                    }
                )
                for step in run.steps
                if own(step) or step.history_length < redone
            ]
            waiting = [w for w in run.waiting_on if own(w)]
            done = [w for w in _done(row) if own(w)]
            for w in [*run.waiting_on, *_done(row)]:
                if own(w):
                    continue
                # An entry with no length was parked before 6e; `check_reset` refuses
                # such a run, so this only keeps it waiting as it was.
                if w.history_length is not None and w.history_length >= redone:
                    continue
                if w.resolved_at is not None and w.resolved_at < redone:
                    done.append(w)
                else:
                    waiting.append(w.model_copy(update={"resolved_at": None}))
            updated = conn.execute(
                "UPDATE workflow_runs SET status = %s, waiting_on = %s, waiting_done = %s,"
                " steps = %s, result = NULL, result_truncated = false, workflow_run_id = %s,"
                " superseded_run_ids = array_append(superseded_run_ids, %s),"
                " reset_request_ids = array_append(reset_request_ids, %s),"
                f" updated_at = now() WHERE id = %s RETURNING {_RUN}",
                (
                    "waiting" if waiting else "running",
                    Jsonb([w.model_dump(mode="json") for w in waiting]),
                    Jsonb([w.model_dump(mode="json") for w in done]),
                    Jsonb([s.model_dump(mode="json") for s in steps]),
                    current,
                    replaced,
                    request_id,
                    run_id,
                ),
            ).fetchone()
            assert updated is not None
            after = Run.model_validate(updated)
            self._announce(conn, after)
        return after

    def _open_runs(self, older_than: timedelta) -> list[Run]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_RUN} FROM workflow_runs WHERE status IN ('running', 'waiting')"
                " AND updated_at <= now() - %s ORDER BY updated_at",
                (older_than,),
            ).fetchall()
        return [Run.model_validate(row) for row in rows]

    def _mark_terminated(self, run_id: str) -> Run | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE workflow_runs SET status = 'terminated', waiting_on = '[]',"
                " updated_at = now() WHERE id = %s AND status IN ('running', 'waiting')"
                f" RETURNING {_RUN}",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            run = Run.model_validate(row)
            self._announce(conn, run)
        return run

    def _follow_run(self, run_id: str, workflow_run_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE workflow_runs SET workflow_run_id = %s, updated_at = now() WHERE id = %s",
                (workflow_run_id, run_id),
            )

    def _delete_run(self, run_id: str) -> bool:
        with self._pool.connection() as conn, conn.transaction():
            # Its decisions go by cascade; the operations that made them carry the
            # answers in their requests, so they go too.
            conn.execute("DELETE FROM operations WHERE subject = %s", (f"flow:{run_id}",))
            # Its grant's uses go by cascade.
            conn.execute("DELETE FROM flow_grants WHERE run_id = %s", (run_id,))
            cur = conn.execute("DELETE FROM workflow_runs WHERE id = %s", (run_id,))
        return cur.rowcount > 0


def _hops(hops: list[HopRecord]) -> Jsonb:
    return Jsonb([h.model_dump(mode="json") for h in hops])


def _seq(step: Any) -> int:
    return int(step.seq)
