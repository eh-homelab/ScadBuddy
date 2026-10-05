"""Segment accounting and AgentState snapshots on the agent's `ai_durable_*` tables."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from temporalio import activity
from temporalio.converter import DataConverter

from scadbuddy_durable.models import InFlight, SnapshotInput

SAVE_SNAPSHOT = "durable_save_snapshot"


@dataclass(frozen=True)
class SessionLimits:
    budget_usd: float
    cost_usd: float
    max_turns: int


class Segments:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def record(
        self, session_id: str, segment_index: int, attempt: int, claude_session_id: str, cost_usd: float
    ) -> None:
        """Idempotent on (session, segment, attempt); every attempt's cost counts."""
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO ai_durable_segments"
                " (session_id, segment_index, attempt, claude_session_id, cost_usd)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (session_id, segment_index, attempt, claude_session_id, cost_usd),
            )
            await conn.execute(
                "UPDATE ai_sessions SET"
                " cost_usd = (SELECT coalesce(sum(cost_usd), 0) FROM ai_durable_segments"
                "             WHERE session_id = %(id)s),"
                " turns = (SELECT count(DISTINCT segment_index) FROM ai_durable_segments"
                "          WHERE session_id = %(id)s),"
                " updated_at = now() WHERE id = %(id)s",
                {"id": session_id},
            )

    async def limits(self, session_id: str) -> SessionLimits:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT budget_usd, cost_usd, max_turns FROM ai_sessions WHERE id = %s", (session_id,)
            )
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"no session {session_id}")
        return SessionLimits(budget_usd=row[0], cost_usd=row[1], max_turns=row[2])


@dataclass(frozen=True)
class SnapshotRow:
    state_json: str
    in_flight: list[InFlight]
    saved_at: datetime
    version: int


class Snapshots:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def save(self, inp: SnapshotInput) -> None:
        """Upsert unless an equal or newer version is stored (plan ruling 15)."""
        payload = DataConverter.default.payload_converter.to_payload(inp.state)
        state_json = payload.data.decode()
        in_flight = [{"id": c.id, "name": c.name, "status": c.status} for c in inp.in_flight]
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO ai_durable_snapshots (session_id, version, state, in_flight)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (session_id) DO UPDATE SET version = excluded.version,"
                " state = excluded.state, in_flight = excluded.in_flight, saved_at = now()"
                " WHERE excluded.version >= ai_durable_snapshots.version",
                (inp.session_id, inp.version, state_json, Jsonb(in_flight)),
            )

    async def latest(self, session_id: str) -> SnapshotRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT state, in_flight, saved_at, version FROM ai_durable_snapshots WHERE session_id = %s",
                (session_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return SnapshotRow(
            state_json=row[0],
            in_flight=[InFlight(c["id"], c["name"], c["status"]) for c in row[1]],
            saved_at=row[2],
            version=row[3],
        )


def make_save_snapshot(snapshots: Snapshots) -> Callable[[SnapshotInput], Awaitable[None]]:
    """The `save_snapshot` activity bound to a `Snapshots`, for the worker to register."""

    @activity.defn(name=SAVE_SNAPSHOT)
    async def save_snapshot(inp: SnapshotInput) -> None:
        await snapshots.save(inp)

    return save_snapshot
