"""Each running durable session's live output into `ai_session_events` (plan ruling 14).

One projector per agent-durable process. It claims a lease on a session's
`ai_durable_streams` row while the session is `running` or `waiting_approval`, follows the
workflow's live output from the stored offset, and appends each translated batch with the
status it leaves and the next offset in ONE transaction (the SQL of
agent/src/sessions/eventLog.ts `append`), so a crash replays from the last committed offset
and never logs an event twice. After each commit it sends the batch's `session.*` NOTIFY
(busEvents.ts `SessionEventPublisher`'s payload, without the throttle: batches are per
event group, not per token).

A follower is pinned to the run that was running when it started (`follow_run`). The
stored offset counts in a `chain` (the first run's id, which Continue-As-New keeps and a new
start does not): a follower of another chain reads from 0, and it stores the offset only
over the offset and chain it read or last committed. So a follower of an older run can
neither read a new run from its old offset nor write over the agent service's reset
(ruling 9), however the two interleave.

A session that says it runs while no run runs (terminated, failed, stopped) is settled idle
(`_settle_if_stale`), but never while a committed message waits for a run
(`ai_durable_inputs`, status pending): the agent service's send (or another replica that
takes it over) starts the run that takes it, and settling would drop it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy_durable.models import DECISIONS_QUERY
from scadbuddy_durable.tools import TIERS
from scadbuddy_durable.translate import Batch, Translator, bus_kind_of, durable_approval_id

PG_CHANNEL = "scadbuddy_events"  # agent/src/events/bus.ts

log = logging.getLogger(__name__)

_CLAIM = """
INSERT INTO ai_durable_streams (session_id, holder, lease_until)
SELECT id, %(me)s, now() + make_interval(secs => %(lease)s) FROM ai_sessions
WHERE mode = 'durable' AND status IN ('running', 'waiting_approval')
ON CONFLICT (session_id) DO UPDATE SET holder = %(me)s, lease_until = excluded.lease_until
WHERE ai_durable_streams.lease_until IS NULL OR ai_durable_streams.lease_until < now()
   OR ai_durable_streams.holder = %(me)s
RETURNING session_id, next_offset, chain
"""

# Settling a session whose run is gone: its row moves to idle only if nothing was logged
# since the projector looked (event_seq) and no committed message waits to run.
_SETTLE = """
WITH s AS (
  UPDATE ai_sessions SET event_seq = event_seq + %(n)s, status = 'idle', updated_at = now()
  WHERE id = %(id)s AND status IN ('running', 'waiting_approval') AND event_seq = %(seq)s
    AND NOT EXISTS (
      SELECT 1 FROM ai_durable_inputs WHERE session_id = %(id)s AND status = 'pending'
    )
  RETURNING event_seq - %(n)s AS base
)
INSERT INTO ai_session_events (session_id, seq, event)
SELECT %(id)s, s.base + e.ord, e.event
FROM s, unnest(%(texts)s::text[]) WITH ORDINALITY AS e(event, ord)
RETURNING seq
"""

# What a settled session's open approvals resolve with, unless a Stop ended its run.
RUN_ENDED = "the session's run ended"

_APPEND = """
WITH s AS (
  UPDATE ai_sessions SET event_seq = event_seq + %(n)s, status = coalesce(%(status)s, status),
         updated_at = now()
  WHERE id = %(id)s
  RETURNING event_seq - %(n)s AS base
)
INSERT INTO ai_session_events (session_id, seq, event)
SELECT %(id)s, s.base + e.ord, e.event
FROM s, unnest(%(texts)s::text[]) WITH ORDINALITY AS e(event, ord)
RETURNING seq
"""


# The plugin's live-output topic (temporalio.claude_agent_sdk._events.TOPIC, not exported;
# tests/test_projector.py checks it against the pin).
STREAM_TOPIC = "claude"


@dataclass(frozen=True)
class LatestRun:
    run_id: str
    #: The first run's id: Continue-As-New keeps it, a new start does not.
    chain: str
    status: WorkflowExecutionStatus | None

    @property
    def running(self) -> bool:
        return self.status == WorkflowExecutionStatus.RUNNING

    @property
    def stopped(self) -> bool:
        """Closed by a Stop: the workflow returns its state on cancellation (deviation 4)."""
        return self.status in (WorkflowExecutionStatus.COMPLETED, WorkflowExecutionStatus.CANCELED)


@dataclass(frozen=True)
class StreamRow:
    """What the settle compares against, read before the run is described."""

    status: str
    event_seq: int
    #: A committed message waits for a run to take it.
    pending: bool


class LeaseLost(Exception):
    """Another projector holds the session now, or the stream was reset under this one."""


async def follow_run(
    client: Client, workflow_id: str, run_id: str, *, from_offset: int
) -> AsyncIterator[dict[str, Any]]:
    """`follow_agent`, pinned to `run_id` until that run continues as new.

    `follow_agent` polls the latest run of the ID, so a follower left from a run that was
    terminated would go on reading the NEXT run (a new start, ruling 9) from the old run's
    offset. Pinned, its polls fail once that run closes, and the stream ends. The pin does
    not carry over Continue-As-New: then the stream client re-targets the ID's latest run,
    unpinned (workflow_streams `_follow_continue_as_new`), which is the successor unless a
    new start replaced it; `append_batch`'s chain and offset check refuses what such a
    follower would write after a reset, and `_follow` drops it once the run it saw closes.
    """
    stream = WorkflowStreamClient(client.get_workflow_handle(workflow_id, run_id=run_id), client=client)
    async for item in stream.subscribe(STREAM_TOPIC, from_offset=from_offset):
        data = item.data
        event = dict(data) if isinstance(data, dict) else {"type": "data", "data": data}
        event["offset"] = item.offset
        yield event


async def append_batch(
    conn: AsyncConnection[Any],
    session_id: str,
    batch: Batch,
    next_offset: int,
    *,
    holder: str,
    expected_offset: int,
    expected_chain: str | None = None,
    chain: str | None = None,
) -> int | None:
    """Appends `batch` and stores `next_offset` in `chain`, in one transaction; returns the last seq.

    None when `holder` no longer holds the lease, or the stored offset and chain are no
    longer `expected_offset` and `expected_chain` (what the follower read or last
    committed): the agent service reset them for a new run (ruling 9), or another follower
    moved them, so this follower's events are not the stream's next ones. Nothing is
    written then. A `session.result` takes the session's cost, turns and budget from its
    row, as classic fills them in.
    """
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE ai_durable_streams SET next_offset = %s, chain = coalesce(%s, chain)"
            " WHERE session_id = %s AND holder = %s AND next_offset = %s"
            " AND chain IS NOT DISTINCT FROM %s",
            (next_offset, chain, session_id, holder, expected_offset, expected_chain),
        )
        if cur.rowcount == 0:
            return None
        if not batch.events:
            return 0
        events = await _with_totals(conn, session_id, batch.events)
        texts = [json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in events]
        cur = await conn.execute(
            _APPEND, {"n": len(texts), "status": batch.status, "id": session_id, "texts": texts}
        )
        seqs = sorted(int(r[0]) for r in await cur.fetchall())
        if len(seqs) != len(texts):
            raise LookupError(f"session {session_id} does not exist")
    await _notify(conn, session_id, events, seqs[-1], holder)
    return seqs[-1]


async def settle_idle(
    conn: AsyncConnection[Any],
    session_id: str,
    events: list[dict[str, Any]],
    *,
    holder: str,
    event_seq: int,
) -> int | None:
    """Moves a session whose run is gone to idle with `events`, in one transaction.

    Only while `holder` holds the lease, the session's `event_seq` is still `event_seq`
    (nothing was logged since: a claim logs its `user.turn`) and no committed message is
    pending. Clears the stream's `sending`. Returns the last seq, or None when nothing
    changed.
    """
    texts = [json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in events]
    seqs: list[int] = []
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE ai_durable_streams SET sending = NULL WHERE session_id = %s AND holder = %s",
            (session_id, holder),
        )
        if cur.rowcount == 0:
            return None
        cur = await conn.execute(
            _SETTLE, {"n": len(texts), "id": session_id, "seq": event_seq, "texts": texts}
        )
        seqs = sorted(int(r[0]) for r in await cur.fetchall())
        if len(seqs) != len(texts):
            raise psycopg.Rollback()
    if not seqs:
        return None
    await _notify(conn, session_id, events, seqs[-1], holder)
    return seqs[-1]


async def log_events(conn: AsyncConnection[Any], session_id: str, events: list[dict[str, Any]]) -> int:
    """Appends `events` to the session's log in the caller's transaction; returns the last
    seq. The caller announces them (`notify_events`) once it committed."""
    texts = [json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in events]
    cur = await conn.execute(_APPEND, {"n": len(texts), "status": None, "id": session_id, "texts": texts})
    seqs = sorted(int(r[0]) for r in await cur.fetchall())
    if len(seqs) != len(texts):
        raise LookupError(f"session {session_id} does not exist")
    return seqs[-1]


async def notify_events(
    conn: AsyncConnection[Any], session_id: str, events: list[dict[str, Any]], seq: int, replica: str
) -> None:
    """The bus NOTIFY for committed `events` (agent/src/sessions/busEvents.ts)."""
    await _notify(conn, session_id, events, seq, replica)


async def _notify(
    conn: AsyncConnection[Any], session_id: str, events: list[dict[str, Any]], seq: int, holder: str
) -> None:
    kind, status = bus_kind_of(events)
    payload: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "kind": kind,
        "session_id": session_id,
        "seq": seq,
    }
    if status is not None:
        payload["status"] = status
    payload["replica"] = holder
    try:
        await conn.execute("SELECT pg_notify(%s, %s)", (PG_CHANNEL, json.dumps(payload)))
    except Exception as err:  # the log is durable and polled; a lost NOTIFY costs latency
        log.warning("session events: NOTIFY failed: %s", err)


async def _with_totals(
    conn: AsyncConnection[Any], session_id: str, events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not any(e.get("type") == "session.result" for e in events):
        return events
    cur = await conn.execute(
        "SELECT cost_usd, turns, budget_usd FROM ai_sessions WHERE id = %s FOR UPDATE", (session_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise LookupError(f"session {session_id} does not exist")
    totals = {"costUsd": row[0], "turns": row[1], "budgetUsd": row[2]}
    return [{**e, **totals} if e.get("type") == "session.result" else e for e in events]


_SCAN_PAGE = 200
_SETTLED = ("idle", "done", "failed")
_APPROVAL_TYPES = ("approval.required", "approval.resolved", "tool.result")


async def _turn_events(conn: AsyncConnection[Any], session_id: str) -> list[dict[str, Any]]:
    """The current turn's approval events (after the last settled `session.status`), oldest first.

    Decided in Python on each event's parsed `type` and `status` only: the text inside an
    event (a delta, a tool input, a user turn) is model- or user-controlled and may spell out
    anything, and Postgres's json and jsonb both refuse the `\\u0000` it may contain. Scans
    back from the end a page at a time, so it reads the current turn and at most a page more.
    """
    found: list[dict[str, Any]] = []
    before: int | None = None
    while True:
        cur = await conn.execute(
            "SELECT seq, event FROM ai_session_events WHERE session_id = %s"
            " AND (%s::bigint IS NULL OR seq < %s) ORDER BY seq DESC LIMIT %s",
            (session_id, before, before, _SCAN_PAGE),
        )
        rows = await cur.fetchall()
        for seq, text in rows:
            before = int(seq)
            try:
                event = json.loads(text)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "session.status" and event.get("status") in _SETTLED:
                return found[::-1]
            if kind in _APPROVAL_TYPES:
                found.append(event)
        if len(rows) < _SCAN_PAGE:
            return found[::-1]


class Projector:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        client: Client,
        *,
        holder: str,
        lease_s: float = 20,
        renew_s: float = 5,
        poll_s: float = 0.5,
        tiers: Mapping[str, str] = TIERS,
        secrets: Callable[[], Awaitable[Sequence[str]]] | None = None,
    ) -> None:
        self._pool = pool
        self._client = client
        self._holder = holder
        self._lease_s = lease_s
        self._renew_s = renew_s
        self._poll_s = poll_s
        self._tiers = tiers
        # The credentials redacted from everything logged (translate.py `redact`).
        self._secrets = secrets
        self._followers: dict[str, asyncio.Task[None]] = {}

    @property
    def following(self) -> set[str]:
        """Sessions this projector follows right now."""
        return {sid for sid, task in self._followers.items() if not task.done()}

    async def run(self, stop: asyncio.Event) -> None:
        """Claims, follows and renews until `stop`; then releases what it holds."""
        loop = asyncio.get_running_loop()
        renewed = loop.time()
        try:
            while not stop.is_set():
                try:
                    for sid, offset, chain in await self._claim():
                        if sid not in self.following:
                            self._followers[sid] = asyncio.create_task(self._follow(sid, offset, chain))
                    self._followers = {s: t for s, t in self._followers.items() if not t.done()}
                    if loop.time() - renewed >= self._renew_s:
                        await self._renew()
                        renewed = loop.time()
                except Exception:
                    log.exception("projector %s: claim failed", self._holder)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self._poll_s)
        finally:
            tasks = list(self._followers.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._followers = {}
        await self._release()

    async def _claim(self) -> list[tuple[str, int, str | None]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_CLAIM, {"me": self._holder, "lease": self._lease_s})
            return [(str(r[0]), int(r[1]), None if r[2] is None else str(r[2])) for r in await cur.fetchall()]

    async def _renew(self) -> None:
        held = list(self.following)
        if not held:
            return
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE ai_durable_streams SET lease_until = now() + make_interval(secs => %s)"
                " WHERE holder = %s AND session_id = ANY(%s::uuid[])",
                (self._lease_s, self._holder, held),
            )

    async def _release(self, session_id: str | None = None) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE ai_durable_streams SET holder = NULL, lease_until = NULL"
                " WHERE holder = %s AND (%s::uuid IS NULL OR session_id = %s::uuid)",
                (self._holder, session_id, session_id),
            )

    async def _open_approvals(self, session_id: str) -> tuple[list[str], list[str]]:
        """This turn's approvals in the log with no result yet, and those already resolved.

        The turn starts after the last settled `session.status`: an earlier turn's approvals
        were closed by its `done`, `error` or `cancelled`. An `approval.resolved` (the
        route's after a decision, or a stop's) means nobody owes it again on `cancelled`.
        """
        prefix = f"durable:{session_id}:"
        async with self._pool.connection() as conn:
            turn = await _turn_events(conn, session_id)
        open_ids: list[str] = []
        resolved: set[str] = set()
        for event in turn:
            kind = event.get("type")
            if kind == "approval.required":
                open_ids.append(str(event["tool"]))
            elif kind == "approval.resolved" and str(event.get("id", "")).startswith(prefix):
                resolved.add(str(event["id"]).removeprefix(prefix))
            elif kind == "tool.result" and event.get("id") in open_ids:
                open_ids.remove(event["id"])
                resolved.discard(event["id"])
        return open_ids, [i for i in open_ids if i in resolved]

    async def _resolved_now(self, session_id: str, wid: str) -> set[str]:
        """Before `cancelled`: approvals a person decided (the route logs their resolution)."""
        _, resolved = await self._open_approvals(session_id)
        decided = set(resolved)
        try:
            decisions: dict[str, str] = await self._client.get_workflow_handle(wid).query(DECISIONS_QUERY)
            decided.update(decisions)
        except Exception as err:  # the log alone answers when the run is gone
            log.info("projector %s: no decisions for %s: %s", self._holder, session_id, err)
        return decided

    async def _follow(self, session_id: str, offset: int, chain: str | None) -> None:
        """Follows the session's running run; `offset` counts in `chain` (None: the next run's)."""
        wid = f"session-{session_id}"
        try:
            while True:
                # Read before the describe: a claim that commits after this read then fails
                # the settle's compare-and-set (its `user.turn` moves event_seq).
                row = await self._stream_row(session_id)
                latest = await self._latest(wid)
                if latest is None or not latest.running:
                    # No run runs: nothing of a closed one is ours to log. A new run may be
                    # about to start (a send in flight), or none will (a terminate, a crash).
                    await self._settle_if_stale(session_id, wid, latest, row)
                    await self._release(session_id)
                    return
                # Rebuilt from the log each time: the stream is re-read from the committed offset.
                open_ids, resolved = await self._open_approvals(session_id)
                translator = Translator(
                    session_id, self._tiers, open_ids, resolved, secrets=await self._redacted()
                )
                try:
                    offset, final, chain = await self._drain(
                        session_id, wid, latest, translator, offset, chain
                    )
                except RPCError as err:
                    if err.status != RPCStatusCode.NOT_FOUND:
                        raise
                    final = False
                now = await self._latest(wid)
                if final or now is None or now.run_id != latest.run_id:
                    # Its turn ended, or its run closed without ending it (terminated,
                    # failed): a later run is followed by a later claim, from the offset
                    # and chain stored for it.
                    await self._release(session_id)
                    return
                await asyncio.sleep(self._poll_s)
        except LeaseLost:
            log.info("projector %s: lost the lease on %s", self._holder, session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("projector %s: following %s failed", self._holder, session_id)

    async def _redacted(self) -> Sequence[str]:
        return [] if self._secrets is None else await self._secrets()

    async def _latest(self, wid: str) -> LatestRun | None:
        """The ID's latest run, or None when it has none."""
        try:
            desc = await self._client.get_workflow_handle(wid).describe()
        except RPCError as err:
            if err.status != RPCStatusCode.NOT_FOUND:
                raise
            return None
        info = desc.raw_description.workflow_execution_info
        return LatestRun(run_id=desc.run_id, chain=info.first_run_id or desc.run_id, status=desc.status)

    async def _stream_row(self, session_id: str) -> StreamRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT s.status, s.event_seq, EXISTS (SELECT 1 FROM ai_durable_inputs i"
                "  WHERE i.session_id = s.id AND i.status = 'pending')"
                " FROM ai_sessions s JOIN ai_durable_streams d ON d.session_id = s.id WHERE s.id = %s",
                (session_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return StreamRow(str(row[0]), int(row[1]), bool(row[2]))

    async def _settle_if_stale(
        self, session_id: str, wid: str, latest: LatestRun | None, row: StreamRow | None
    ) -> None:
        """A session that says `running` or `waiting_approval` with no run running goes idle.

        Its run was stopped, terminated or failed. `row` was read BEFORE `latest` was
        described, and the settle is a compare-and-set on it (`settle_idle`), so a claim that
        commits after the read makes the settle refuse.

        Never while a committed message is pending (`ai_durable_inputs`): no run took it yet,
        and the agent service's send, or the replica that takes over a send whose sender is
        gone (manager.ts `resumeDurableSends`), starts the run that will. A message is never
        dropped by a settle; one that will not run is abandoned by whoever says so (a Stop,
        a refusal), with an `error` in the log.

        A run a Stop closed resolves its open approvals as a stop does (the `cancelled`
        event's translation, decisions from DECISIONS_QUERY or the log); any other end, or no
        run at all, as RUN_ENDED.
        """
        if row is None or row.status not in ("running", "waiting_approval") or row.pending:
            return
        open_ids, resolved = await self._open_approvals(session_id)
        if latest is not None and latest.stopped:
            translator = Translator(
                session_id, self._tiers, open_ids, resolved, secrets=await self._redacted()
            )
            translator.mark_resolved(await self._resolved_now(session_id, wid))
            events = translator.feed({"type": "cancelled", "offset": 0}).events
        else:
            events = [
                {
                    "v": 1,
                    "type": "approval.resolved",
                    "sessionId": session_id,
                    "id": durable_approval_id(session_id, call_id),
                    "approved": False,
                    "reason": RUN_ENDED,
                }
                for call_id in open_ids
                if call_id not in resolved
            ]
            events.append({"v": 1, "type": "session.status", "sessionId": session_id, "status": "idle"})
        async with self._pool.connection() as conn:
            settled = await settle_idle(
                conn, session_id, events, holder=self._holder, event_seq=row.event_seq
            )
        if settled is not None:
            log.info("projector %s: settled %s, whose run is gone, to idle", self._holder, session_id)

    async def _drain(
        self,
        session_id: str,
        wid: str,
        run: LatestRun,
        translator: Translator,
        offset: int,
        chain: str | None,
    ) -> tuple[int, bool, str | None]:
        """Follows `run` until the turn ends (True) or the stream does.

        `offset` and `chain` are what the stream row holds (read at the claim, or last
        committed); returns them as they are after the last commit.
        """
        stored_offset, stored_chain = offset, chain
        if chain is not None and chain != run.chain:
            offset = 0
        events = follow_run(self._client, wid, run.run_id, from_offset=offset)
        try:
            async for event in events:
                decided_by: str | None = None
                if event.get("type") == "tool_result" and event.get("status") == "rejected":
                    decisions: dict[str, str] = await self._client.get_workflow_handle(wid).query(
                        DECISIONS_QUERY
                    )
                    decided_by = decisions.get(str(event.get("id")))
                if event.get("type") == "cancelled":
                    translator.mark_resolved(await self._resolved_now(session_id, wid))
                batch = translator.feed(event, decided_by=decided_by)
                if not batch.events:
                    continue  # nothing to log; the offset moves with the next batch
                offset = int(event["offset"]) + 1
                async with self._pool.connection() as conn:
                    appended = await append_batch(
                        conn,
                        session_id,
                        batch,
                        offset,
                        holder=self._holder,
                        expected_offset=stored_offset,
                        expected_chain=stored_chain,
                        chain=run.chain,
                    )
                    if appended is None:
                        raise LeaseLost(session_id)
                stored_offset, stored_chain = offset, run.chain
                if batch.final:
                    return stored_offset, True, stored_chain
        finally:
            await events.aclose()  # type: ignore[attr-defined]
        return stored_offset, False, stored_chain
