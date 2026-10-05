"""Each running durable session's live output into `ai_session_events` (plan ruling 14).

One projector per agent-durable process. It claims a lease on a session's
`ai_durable_streams` row while the session is `running` or `waiting_approval`, follows the
workflow's live output from the stored offset, and appends each translated batch with the
status it leaves and the next offset in ONE transaction (the SQL of
agent/src/sessions/eventLog.ts `append`), so a crash replays from the last committed offset
and never logs an event twice. After each commit it sends the batch's `session.*` NOTIFY
(busEvents.ts `SessionEventPublisher`'s payload, without the throttle: batches are per
event group, not per token).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from temporalio.claude_agent_sdk import follow_agent
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy_durable.models import DECISIONS_QUERY
from scadbuddy_durable.tools import TIERS
from scadbuddy_durable.translate import Batch, Translator, bus_kind_of

PG_CHANNEL = "scadbuddy_events"  # agent/src/events/bus.ts

log = logging.getLogger(__name__)

_CLAIM = """
INSERT INTO ai_durable_streams (session_id, holder, lease_until)
SELECT id, %(me)s, now() + make_interval(secs => %(lease)s) FROM ai_sessions
WHERE mode = 'durable' AND status IN ('running', 'waiting_approval')
ON CONFLICT (session_id) DO UPDATE SET holder = %(me)s, lease_until = excluded.lease_until
WHERE ai_durable_streams.lease_until IS NULL OR ai_durable_streams.lease_until < now()
   OR ai_durable_streams.holder = %(me)s
RETURNING session_id, next_offset
"""

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


class LeaseLost(Exception):
    """Another projector holds the session now."""


async def append_batch(
    conn: AsyncConnection[Any], session_id: str, batch: Batch, next_offset: int, *, holder: str
) -> int | None:
    """Appends `batch` and stores `next_offset`, in one transaction; returns the last seq.

    None when `holder` no longer holds the lease: nothing is written. A `session.result`
    takes the session's cost, turns and budget from its row, as classic fills them in.
    """
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE ai_durable_streams SET next_offset = %s WHERE session_id = %s AND holder = %s",
            (next_offset, session_id, holder),
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
    kind, status = bus_kind_of(events)
    payload: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "kind": kind,
        "session_id": session_id,
        "seq": seqs[-1],
    }
    if status is not None:
        payload["status"] = status
    payload["replica"] = holder
    try:
        await conn.execute("SELECT pg_notify(%s, %s)", (PG_CHANNEL, json.dumps(payload)))
    except Exception as err:  # the log is durable and polled; a lost NOTIFY costs latency
        log.warning("session events: NOTIFY failed: %s", err)
    return seqs[-1]


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
    ) -> None:
        self._pool = pool
        self._client = client
        self._holder = holder
        self._lease_s = lease_s
        self._renew_s = renew_s
        self._poll_s = poll_s
        self._tiers = tiers
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
                    for sid, offset in await self._claim():
                        if sid not in self.following:
                            self._followers[sid] = asyncio.create_task(self._follow(sid, offset))
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

    async def _claim(self) -> list[tuple[str, int]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_CLAIM, {"me": self._holder, "lease": self._lease_s})
            return [(str(r[0]), int(r[1])) for r in await cur.fetchall()]

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
            cur = await conn.execute(
                "SELECT event FROM ai_session_events WHERE session_id = %(id)s AND seq > coalesce("
                "  (SELECT max(seq) FROM ai_session_events WHERE session_id = %(id)s"
                "   AND event LIKE '%%\"session.status\"%%'"
                "   AND (event LIKE '%%\"idle\"%%' OR event LIKE '%%\"done\"%%'"
                "        OR event LIKE '%%\"failed\"%%')), 0)"
                " AND (event LIKE '%%\"approval.%%' OR event LIKE '%%\"tool.result\"%%')"
                " ORDER BY seq",
                {"id": session_id},
            )
            rows = await cur.fetchall()
        open_ids: list[str] = []
        resolved: set[str] = set()
        for (text,) in rows:
            event = json.loads(text)
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

    async def _follow(self, session_id: str, offset: int) -> None:
        wid = f"session-{session_id}"
        try:
            while True:
                # Rebuilt from the log each time: the stream is re-read from the committed offset.
                open_ids, resolved = await self._open_approvals(session_id)
                translator = Translator(session_id, self._tiers, open_ids, resolved)
                try:
                    offset, final = await self._drain(session_id, wid, translator, offset)
                except RPCError as err:
                    if err.status != RPCStatusCode.NOT_FOUND:
                        raise
                    final = False  # not started yet: the Update that starts it is on its way
                if final:
                    await self._release(session_id)
                    return
                await asyncio.sleep(self._poll_s)
        except LeaseLost:
            log.info("projector %s: lost the lease on %s", self._holder, session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("projector %s: following %s failed", self._holder, session_id)

    async def _drain(
        self, session_id: str, wid: str, translator: Translator, offset: int
    ) -> tuple[int, bool]:
        """Follows until the turn ends (True) or the stream does; returns the next offset."""
        events = follow_agent(self._client, wid, from_offset=offset)
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
                    if await append_batch(conn, session_id, batch, offset, holder=self._holder) is None:
                        raise LeaseLost(session_id)
                if batch.final:
                    return offset, True
        finally:
            await events.aclose()  # type: ignore[attr-defined]
        return offset, False
