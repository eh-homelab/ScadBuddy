"""Every write about a durable entry (spec 2026-10-01 §6.6, "How a durable entry opens and closes").

``open_input`` and ``resolve_input`` are the only writers of ``ai_pending_input`` and
``ai_input_responses`` besides the agent service's orphan sweep, and each write
commits with the session-log events that report it, so a notification, the panel's
card and the badge's projection come from one write. A row is removed only through
one guarded ``DELETE … RETURNING``: whoever deletes it writes the outcome, the events
and the audit, and the loser writes nothing, so a request never gets two
``input.resolved``.

The events are the agent's panel protocol (agent/src/sessions/protocol.ts), appended
to ``ai_session_events`` exactly as its EventLog appends them, and announced on the
``scadbuddy_events`` channel as its SessionEventPublisher announces a batch, so the
agent's followers wake for them. A durable card's ``id`` is the entry's request id,
which the panel sends back to the respond route as it is.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row

from scadbuddy_durable.gate.ids import parse_durable_request_id

PROTOCOL_VERSION = 1
PG_CHANNEL = "scadbuddy_events"
REPLICA = "agent-durable"
SYSTEM: dict[str, str] = {"kind": "system", "id": "scadbuddy", "label": "ScadBuddy"}
APPROVAL_SUMMARY_MAX = 500

Outcome = Literal["approved", "denied", "expired", "answered", "cancelled", "timed_out"]
Kind = Literal["approval", "answer"]
_WAITING = ("waiting_approval", "waiting_input")
_SETTLED = ("idle", "done", "failed")


@dataclass(frozen=True)
class OpenInput:
    request_id: str
    session_id: str
    workflow_id: str
    workflow_run_id: str
    kind: Kind
    tool: str
    tool_use_id: str
    summary: str
    input_hash: str | None
    prompt: str
    requested_by: dict[str, str]
    responders: Sequence[str]
    # ISO 8601: when the entry's timer fires (at most 86 400 s after it parks).
    expires_at: str
    # The card the panel shows (question.asked's `questions`); None for an approval.
    questions: list[dict[str, Any]] | None = None
    # An attention request's {reason, on_timeout}; None otherwise.
    attention: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResolveInput:
    request_id: str
    outcome: Outcome
    responder: dict[str, str]
    response: Any | None = None
    reason: str | None = None


def _event(body: dict[str, Any]) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, **body}


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _entry(row: dict[str, Any]) -> dict[str, Any]:
    """The §6.6 entry shape (agent `InputEntry`) of a projection row."""
    entry: dict[str, Any] = {
        "id": row["request_id"],
        "kind": row["kind"],
        "session_id": str(row["session_id"]),
        "tool": row["tool"],
        "summary": row["summary"],
        "input_hash": row["input_hash"],
        "prompt": row["prompt"],
        "requested_by": row["requested_by"],
        "responders": list(row["responders"]),
        "created_at": _iso(row["created_at"]),
        "expires_at": _iso(row["expires_at"]),
    }
    if row["attention"] is not None:
        entry["attention"] = row["attention"]
    return entry


async def _rows(
    conn: psycopg.AsyncConnection[Any], sql: str, params: Sequence[Any]
) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def _append(
    conn: psycopg.AsyncConnection[Any], session_id: str, events: list[dict[str, Any]]
) -> None:
    """agent/src/sessions/eventLog.ts ``append``, then the batch's bus NOTIFY (on commit)."""
    if not events:
        return
    texts = [json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in events]
    cur = await conn.execute(
        """
        WITH s AS (
          UPDATE ai_sessions SET event_seq = event_seq + %(n)s, updated_at = now()
          WHERE id = %(sid)s
          RETURNING event_seq - %(n)s AS base
        )
        INSERT INTO ai_session_events (session_id, seq, event)
        SELECT %(sid)s, s.base + e.ord, e.event
        FROM s, unnest(%(texts)s::text[]) WITH ORDINALITY AS e(event, ord)
        RETURNING seq
        """,
        {"n": len(texts), "sid": session_id, "texts": texts},
    )
    seqs = [int(r[0]) for r in await cur.fetchall()]
    if len(seqs) != len(texts):
        raise LookupError(f"session {session_id} does not exist")
    statuses = [e["status"] for e in events if e["type"] == "session.status"]
    status = statuses[-1] if statuses else None
    if status in _WAITING:
        kind = "session.waiting"
    elif status in _SETTLED:
        kind = "session.done"
    else:
        kind = "session.message"
    payload: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "at": _iso(datetime.now(UTC)),
        "kind": kind,
        "session_id": session_id,
        "seq": max(seqs),
        "replica": REPLICA,
    }
    if status is not None:
        payload["status"] = status
    await conn.execute("SELECT pg_notify(%s, %s)", (PG_CHANNEL, json.dumps(payload)))


async def _resolved(
    conn: psycopg.AsyncConnection[Any],
    row: dict[str, Any],
    outcome: Outcome,
    responder: dict[str, str],
    response: Any | None,
    reason: str | None,
) -> None:
    """Records the end of the entry ``row`` (already deleted, in this transaction)."""
    sid = str(row["session_id"])
    rid = row["request_id"]
    await conn.execute(
        """
        INSERT INTO ai_input_responses
          (request_id, session_id, kind, outcome, response, responder, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            rid,
            sid,
            row["kind"],
            outcome,
            None if response is None else json.dumps(response),
            json.dumps(responder),
            reason,
        ),
    )
    person = responder.get("kind") != "system"
    events: list[dict[str, Any]] = []
    if row["kind"] == "approval":
        decided: dict[str, Any] = {
            "type": "approval.resolved",
            "sessionId": sid,
            "id": rid,
            "approved": outcome == "approved",
            "decision": outcome if outcome in ("approved", "denied", "expired") else "cancelled",
        }
        if person and outcome in ("approved", "denied"):
            decided["by"] = responder
        if reason and outcome not in ("approved", "denied"):
            decided["reason"] = reason
        events.append(_event(decided))
    else:
        asked: dict[str, Any] = {
            "type": "question.resolved",
            "sessionId": sid,
            "id": rid,
            "answered": outcome == "answered",
        }
        if outcome == "answered" and isinstance(response, dict) and "answers" in response:
            asked["answers"] = list(response["answers"])
            asked["by"] = responder
        if reason and outcome != "answered":
            asked["reason"] = reason
        events.append(_event(asked))
    resolved: dict[str, Any] = {
        "type": "input.resolved",
        "sessionId": sid,
        "id": rid,
        "kind": row["kind"],
        "outcome": outcome,
    }
    if reason:
        resolved["reason"] = reason
    events.append(_event(resolved))
    # Back to running once nothing of the session is parked any more.
    cur = await conn.execute(
        """
        UPDATE ai_sessions SET status = 'running', updated_at = now()
        WHERE id = %s AND status IN ('waiting_approval', 'waiting_input')
          AND NOT EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = %s)
        RETURNING status
        """,
        (sid, sid),
    )
    if await cur.fetchone():
        events.append(_event({"type": "session.status", "sessionId": sid, "status": "running"}))
    await _append(conn, sid, events)
    if row["kind"] == "approval":
        await _audit(conn, row, outcome, responder, reason)


async def _audit(
    conn: psycopg.AsyncConnection[Any],
    row: dict[str, Any],
    outcome: Outcome,
    responder: dict[str, str],
    reason: str | None,
) -> None:
    """The ``approval`` row ApprovalService writes for a classic decision (audit/log.ts)."""
    person = responder.get("kind") != "system"
    actor = responder if person else SYSTEM
    parsed = parse_durable_request_id(row["request_id"])
    requested = (row["requested_by"] or {}).get("label", "?")
    detail = f"{row['tool']}{': ' + reason if reason else ''} (requested by {requested})"
    audit_outcome = (
        "ok" if outcome == "approved" else "denied" if outcome == "denied" else "refused"
    )
    started: datetime = row["created_at"]
    await conn.execute(
        """
        INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id, principal_label,
                              session_id, tool_use_id, tier, input_hash, input_summary, request_id,
                              outcome, detail, started_at, finished_at, duration_ms)
        VALUES ('approval', %s, %s, %s, %s, %s, %s, %s, 'outward', %s, %s, %s, %s, %s, %s, now(),
                GREATEST(0, (extract(epoch FROM now() - %s) * 1000)::int))
        """,
        (
            outcome,
            "http" if person else "system",
            actor["kind"][:50],
            actor["id"][:200],
            actor["label"][:200],
            str(row["session_id"]),
            parsed[2] if parsed else None,
            row["input_hash"],
            row["summary"][:1000],
            row["request_id"],
            audit_outcome,
            detail[:500],
            started,
            started,
        ),
    )


async def open_input(conn: psycopg.AsyncConnection[Any], args: OpenInput) -> bool:
    """Opens the entry; False when it was open already (a retried activity adds nothing).

    First, in its own transaction, the session's entries of another workflow run (a
    Reset started a new one) are resolved ``cancelled``, reason ``reset``, so after a
    Reset the read shows exactly one entry for the call.
    """
    async with conn.transaction():
        stale = await _rows(
            conn,
            """
            DELETE FROM ai_pending_input WHERE session_id = %s AND workflow_run_id <> %s
            RETURNING *
            """,
            (args.session_id, args.workflow_run_id),
        )
        for old in stale:
            await _resolved(conn, old, "cancelled", SYSTEM, None, "reset")
    async with conn.transaction():
        inserted = await _rows(
            conn,
            """
            INSERT INTO ai_pending_input
              (request_id, session_id, workflow_id, workflow_run_id, kind, tool, summary,
               input_hash, prompt, requested_by, responders, attention, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
            ON CONFLICT (request_id) DO NOTHING
            RETURNING *
            """,
            (
                args.request_id,
                args.session_id,
                args.workflow_id,
                args.workflow_run_id,
                args.kind,
                args.tool,
                args.summary,
                args.input_hash,
                args.prompt,
                json.dumps(args.requested_by),
                list(args.responders),
                None if args.attention is None else json.dumps(args.attention),
                args.expires_at,
            ),
        )
        if not inserted:
            return False
        row = inserted[0]
        sid = args.session_id
        events: list[dict[str, Any]] = []
        if args.kind == "approval":
            events.append(
                _event(
                    {
                        "type": "approval.required",
                        "sessionId": sid,
                        "id": args.request_id,
                        "tool": args.tool_use_id,
                        "summary": _cap(f"{args.tool} {args.summary}", APPROVAL_SUMMARY_MAX),
                        "risk": "outward",
                    }
                )
            )
        else:
            asked: dict[str, Any] = {
                "type": "question.asked",
                "sessionId": sid,
                "id": args.request_id,
                "tool": args.tool_use_id,
                "questions": args.questions or [],
            }
            if args.attention is not None:
                asked["attention"] = {
                    "reason": args.attention["reason"],
                    "onTimeout": args.attention["on_timeout"],
                    "expiresAt": _iso(row["expires_at"]),
                }
            events.append(_event(asked))
        events.append(_event({"type": "input.requested", "sessionId": sid, "entry": _entry(row)}))
        status = "waiting_approval" if args.kind == "approval" else "waiting_input"
        cur = await conn.execute(
            """
            UPDATE ai_sessions SET status = %s, updated_at = now()
            WHERE id = %s AND status <> %s RETURNING status
            """,
            (status, sid, status),
        )
        if await cur.fetchone():
            events.append(_event({"type": "session.status", "sessionId": sid, "status": status}))
        await _append(conn, sid, events)
        return True


async def resolve_input(conn: psycopg.AsyncConnection[Any], args: ResolveInput) -> bool:
    """Resolves the entry; False when it was gone (resolved by another, or never opened)."""
    async with conn.transaction():
        deleted = await _rows(
            conn,
            "DELETE FROM ai_pending_input WHERE request_id = %s RETURNING *",
            (args.request_id,),
        )
        if not deleted:
            return False
        await _resolved(conn, deleted[0], args.outcome, args.responder, args.response, args.reason)
        return True
