"""The tool-call gate's writes (#5299 step 10), for ``api/crud_agent_gate.py`` area ``gate``.

The agent's ``agent/src/gate/store.ts`` (durable-agents spec §6.6, "How a durable entry opens
and closes"), statement for statement: an entry opened (``open_input``), a run grant's
approval (``grant_input``), a flow's refusal (``deny_input``), an entry resolved
(``resolve_input``), a response decided against the row's record (``decide_input``) and the
record read (``read_input``). Each is one transaction (``open_input`` two, as the agent's: the
Reset's cancels first). The rows, the session's events, its status, the ``scadbuddy_input``
NOTIFY and the ``ai_audit`` row commit together, as they did in the agent's transaction.

The events are built here, as the agent built them (key order included, so a row reads the
same whichever process wrote it; ``agent/test/gateBackend.pg.test.ts`` compares the two), and
appended to ``ai_session_events``. Each function answers the batches it appended
(``Logged``), which the agent hands to its EventLog's ``committed`` once the answer is back:
its followers wake and ``session.*`` goes on the bus under its replica id, as after its own
transaction.

``grant_input`` reads the backend's ``flow_grants`` and ``flow_grant_uses`` under the grant
row's share lock, which the agent's role no longer needs (``auth/revoke.py``
``RETIRED_GRANTS``). ``decide_input`` runs the gate's validator (``gate_validate.py``, a copy
of agent-durable's, pinned by the shared vectors) on the row's ``gate`` under the row's lock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from psycopg import Connection
from psycopg.types.json import Jsonb

from scadbuddy.agent_data import events as event_log
from scadbuddy.agent_data.gate_canonical import response_hash
from scadbuddy.agent_data.gate_validate import (
    GateEntry,
    Principal,
    Question,
    RespondRefused,
    RespondRequest,
    Role,
    validate_respond,
)

PROTOCOL_VERSION = 1
#: What a parked call waits on (the agent's ``INPUT_CHANNEL``), sent as a resolution commits.
INPUT_CHANNEL = "scadbuddy_input"
SYSTEM: dict[str, str] = {"kind": "system", "id": "scadbuddy", "label": "ScadBuddy"}
#: The agent's ``APPROVAL_SUMMARY_MAX``: the arguments' 500, plus a resolving tool's line (#4442).
APPROVAL_SUMMARY_MAX = 500 + 200 + 1

Outcome = Literal["approved", "denied", "expired", "answered", "cancelled", "timed_out"]
Kind = Literal["approval", "answer"]

#: How a resolution reads in an entry's ``hops`` (spec 2026-10-11 §4.6).
_HOP_ACTIONS: dict[str, str] = {
    "approved": "approve",
    "denied": "deny",
    "expired": "timeout",
    "cancelled": "cancelled",
}


@dataclass
class Logged:
    """One batch of a session's events this transaction appended, with their seqs."""

    session_id: str
    events: list[dict[str, Any]]
    seqs: list[int]


@dataclass
class Written:
    """What a write did, and the batches it appended (none when it wrote nothing)."""

    done: bool
    logged: list[Logged] = field(default_factory=list)


@dataclass
class LiveCheck:
    """``decide_input`` before it writes: the agent must first say the row's run still runs."""

    workflow_id: str
    run_id: str


@dataclass
class Decided:
    kind: Kind
    outcome: Outcome
    logged: list[Logged]


def run_grant_responder(grant_id: str) -> dict[str, str]:
    return {"kind": "run_grant", "id": grant_id, "label": "Run grant"}


def flow_responder(run_id: str) -> dict[str, str]:
    return {"kind": "flow", "id": run_id, "label": "Flow"}


def _iso(at: datetime) -> str:
    """JavaScript's ``toISOString``: UTC, milliseconds, ``Z``."""
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _actor(responder: dict[str, Any]) -> str:
    kind = responder.get("kind") or ""
    if kind == "session":
        return f"session:{responder.get('id') or ''}"
    return kind if kind in ("browser", "system", "flow") else f"{kind}:{responder.get('id') or ''}"


def _hop(at: str, action: str, by: str, reason: str | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {"hop": at, "action": action, "actor": by, "at": _iso(datetime.now(UTC))}
    if reason:
        step["reason"] = reason
    return step


def _event(body: dict[str, Any]) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, **body}


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_use_id(request_id: str) -> str | None:
    """A durable request id's tool_use_id (the agent's ``parseRequestId``), else None."""
    prefix, sep, rest = request_id.partition(":")
    if prefix != "durable" or not sep:
        return None
    parts = rest.split(":", 2)
    if len(parts) != 3 or not all(parts) or not _is_uuid(parts[0]):
        return None
    return parts[2]


def _is_uuid(text: str) -> bool:
    hexes = text.split("-")
    if [len(h) for h in hexes] != [8, 4, 4, 4, 12]:
        return False
    return all(c in "0123456789abcdefABCDEF" for h in hexes for c in h)


def _entry(row: dict[str, Any]) -> dict[str, Any]:
    """The §6.6 entry shape (the agent's ``InputEntry``) of a row."""
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


def _dumps(event: dict[str, Any]) -> str:
    """``JSON.stringify``'s text of an event."""
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))


def _append(
    conn: Connection[Any], logged: list[Logged], session_id: str, events: list[dict[str, Any]]
) -> None:
    """The agent's ``appendEventsIn`` in this transaction; the batch goes into ``logged``."""
    if not events:
        return
    seqs = event_log.append_in(conn, session_id, [_dumps(e) for e in events])
    logged.append(Logged(session_id, events, seqs))


def _resolved(
    conn: Connection[Any],
    logged: list[Logged],
    row: dict[str, Any],
    outcome: Outcome,
    responder: dict[str, Any],
    response: Any,
    reason: str | None,
    digest: str | None = None,
) -> None:
    """Records the end of the entry ``row`` (already deleted, in this transaction)."""
    sid = str(row["session_id"])
    rid = row["request_id"]
    # An attention request's timer gives no reason; it gets classic's (#815).
    if outcome == "timed_out" and reason is None and row["attention"]:
        on_timeout = row["attention"].get("on_timeout")
        on_timeout = "proceed" if on_timeout is None else on_timeout
        reason = f"nobody replied in time (on_timeout: {on_timeout})"
    hops: list[dict[str, Any]] = list(row["hops"] or [])
    if row["flow_run_id"] is not None and row["hop"] is not None:
        # A run's entry keeps its way up, the resolution as its last step (§4.6).
        action = _HOP_ACTIONS.get(outcome, outcome)
        hops.append(_hop(row["hop"], action, _actor(responder), reason))
    # The tool and when it was asked, which the attention rate limit counts (#5003).
    conn.execute(
        "INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, response,"
        " responder, reason, hops, response_hash, tool, requested_at, decision_traceparent)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            rid,
            sid,
            row["kind"],
            outcome,
            None if response is None else Jsonb(response),
            Jsonb(responder),
            reason,
            Jsonb(hops),
            digest,
            row["tool"],
            row["created_at"],
            row.get("decision_traceparent"),
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
        answers = response.get("answers") if isinstance(response, dict) else None
        asked: dict[str, Any] = {
            "type": "question.resolved",
            "sessionId": sid,
            "id": rid,
            "answered": outcome == "answered",
        }
        if outcome == "answered" and isinstance(response, dict) and answers is not None:
            asked["answers"] = list(answers)
            asked["by"] = responder
        if reason and outcome != "answered":
            asked["reason"] = reason
        # #5003: a tab wait the tab's return ended.
        if (
            outcome == "cancelled"
            and isinstance(response, dict)
            and response.get("reconnected") is True
        ):
            asked["reconnected"] = True
        events.append(_event(asked))
    ended: dict[str, Any] = {
        "type": "input.resolved",
        "sessionId": sid,
        "id": rid,
        "kind": row["kind"],
        "outcome": outcome,
    }
    if reason:
        ended["reason"] = reason
    events.append(_event(ended))
    # The status follows what of the session is still parked, read under the session row's
    # lock, so two resolves that overlap cannot each see the other's entry still there (#3685).
    conn.execute("SELECT 1 FROM ai_sessions WHERE id = %s FOR UPDATE", (sid,))
    moved = conn.execute(
        "WITH next AS (SELECT CASE"
        " WHEN EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = %(sid)s"
        " AND kind = 'approval') THEN 'waiting_approval'"
        " WHEN EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = %(sid)s)"
        " THEN 'waiting_input' ELSE 'running' END AS status)"
        " UPDATE ai_sessions SET status = next.status, updated_at = now() FROM next"
        " WHERE id = %(sid)s AND ai_sessions.status IN ('waiting_approval', 'waiting_input')"
        " AND ai_sessions.status <> next.status RETURNING ai_sessions.status",
        {"sid": sid},
    ).fetchone()
    if moved is not None:
        events.append(
            _event({"type": "session.status", "sessionId": sid, "status": moved["status"]})
        )
    _append(conn, logged, sid, events)
    conn.execute("SELECT pg_notify(%s, %s)", (INPUT_CHANNEL, rid))
    if row["kind"] == "approval":
        _audit(conn, row, outcome, responder, reason)
    elif outcome != "answered":
        _audit_unanswered(conn, row, outcome, reason)


def _audit_unanswered(
    conn: Connection[Any], row: dict[str, Any], outcome: Outcome, reason: str | None
) -> None:
    """The ``question`` row for a question nobody answered (#1075): ScadBuddy's own."""
    action = outcome if outcome in ("expired", "timed_out") else "cancelled"
    detail = f"{row['tool']} question {row['request_id']}{': ' + reason if reason else ''}"
    started = row["created_at"]
    conn.execute(
        "INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id,"
        " principal_label, session_id, tool_use_id, tier, request_id, outcome, detail,"
        " started_at, finished_at, duration_ms)"
        " VALUES ('question', %s, 'system', %s, %s, %s, %s, %s, 'read', %s, 'refused', %s, %s,"
        " now(), GREATEST(0, (extract(epoch FROM now() - %s::timestamptz) * 1000)::int))",
        (
            action,
            SYSTEM["kind"],
            SYSTEM["id"],
            SYSTEM["label"],
            str(row["session_id"]),
            _tool_use_id(row["request_id"]),
            row["request_id"],
            detail[:500],
            started,
            started,
        ),
    )


def _audit(
    conn: Connection[Any],
    row: dict[str, Any],
    outcome: Outcome,
    responder: dict[str, Any],
    reason: str | None,
) -> None:
    """The ``approval`` row ApprovalService writes for a classic decision (audit/log.ts)."""
    person = responder.get("kind") != "system"
    by = responder if person else SYSTEM
    requested = (row["requested_by"] or {}).get("label")
    requested = "?" if requested is None else requested
    detail = f"{row['tool']}{': ' + reason if reason else ''} (requested by {requested})"
    audit_outcome = (
        "ok" if outcome == "approved" else "denied" if outcome == "denied" else "refused"
    )
    started = row["created_at"]
    conn.execute(
        "INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id,"
        " principal_label, session_id, tool_use_id, tier, input_hash, input_summary, request_id,"
        " outcome, detail, started_at, finished_at, duration_ms)"
        " VALUES ('approval', %s, %s, %s, %s, %s, %s, %s, 'outward', %s, %s, %s, %s, %s, %s,"
        " now(), GREATEST(0, (extract(epoch FROM now() - %s::timestamptz) * 1000)::int))",
        (
            outcome,
            "http" if person else "system",
            str(by["kind"])[:50],
            str(by["id"])[:200],
            str(by["label"])[:200],
            str(row["session_id"]),
            _tool_use_id(row["request_id"]),
            row["input_hash"],
            row["summary"][:1000],
            row["request_id"],
            audit_outcome,
            detail[:500],
            started,
            started,
        ),
    )


@dataclass(frozen=True)
class OpenInput:
    """The agent's ``OpenInput``, as ``api/crud_agent_gate.py`` takes it."""

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
    requested_by: dict[str, Any]
    responders: list[str]
    expires_at: datetime
    questions: list[Any] | None = None
    attention: dict[str, Any] | None = None
    flow_run_id: str | None = None
    hop: Literal["parent", "person"] | None = None
    hop_until: datetime | None = None
    parent_session: str | None = None
    parent_principal: str | None = None
    outside_reason: str | None = None
    gate: dict[str, Any] | None = None


def open_input(conn: Connection[Any], args: OpenInput) -> Written:
    """Opens the entry; ``done`` False when it was open already (a retried activity).

    First, in its own transaction, the session's entries of another workflow run (a Reset
    started a new one) are resolved ``cancelled``, reason ``reset``.
    """
    logged: list[Logged] = []
    with conn.transaction():
        # The session first, as every resolution takes it (``_lock_session_of``).
        conn.execute("SELECT 1 FROM ai_sessions WHERE id = %s FOR UPDATE", (args.session_id,))
        stale = conn.execute(
            "DELETE FROM ai_pending_input WHERE session_id = %s AND workflow_run_id <> %s"
            " RETURNING *",
            (args.session_id, args.workflow_run_id),
        ).fetchall()
        for old in stale:
            _resolved(conn, logged, old, "cancelled", SYSTEM, None, "reset")
    with conn.transaction():
        hops = [] if args.hop is None else [_hop(args.hop, "parked", "flow", args.outside_reason)]
        row = conn.execute(
            "INSERT INTO ai_pending_input (request_id, session_id, workflow_id, workflow_run_id,"
            " kind, tool, summary, input_hash, prompt, requested_by, responders, attention,"
            " expires_at, flow_run_id, hop, hop_until, parent_session, parent_principal,"
            " outside_reason, hops, gate)"
            " SELECT %(rid)s, %(sid)s::uuid, %(wid)s, %(run)s, %(kind)s, %(tool)s, %(summary)s,"
            " %(hash)s, %(prompt)s, %(by)s, %(responders)s::text[], %(attention)s::jsonb,"
            " %(expires)s, %(flow)s, %(hop)s, %(hop_until)s, %(parent)s::uuid,"
            " %(principal)s, %(outside)s, %(hops)s, %(gate)s::jsonb"
            # A retried open that lands after its entry was resolved opens nothing.
            " WHERE NOT EXISTS (SELECT 1 FROM ai_input_responses WHERE request_id = %(rid)s)"
            " ON CONFLICT (request_id) DO NOTHING RETURNING *",
            {
                "rid": args.request_id,
                "sid": args.session_id,
                "wid": args.workflow_id,
                "run": args.workflow_run_id,
                "kind": args.kind,
                "tool": args.tool,
                "summary": args.summary,
                "hash": args.input_hash,
                "prompt": args.prompt,
                "by": Jsonb(args.requested_by),
                "responders": list(args.responders),
                "attention": None if args.attention is None else Jsonb(args.attention),
                "expires": args.expires_at,
                "flow": args.flow_run_id,
                "hop": args.hop,
                "hop_until": args.hop_until,
                "parent": args.parent_session,
                "principal": args.parent_principal,
                "outside": args.outside_reason,
                "hops": Jsonb(hops),
                "gate": None if args.gate is None else Jsonb(args.gate),
            },
        ).fetchone()
        if row is None:
            return Written(False, logged)
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
                    "reason": args.attention.get("reason"),
                    "onTimeout": args.attention.get("on_timeout"),
                    "expiresAt": _iso(row["expires_at"]),
                }
            events.append(_event(asked))
        events.append(_event({"type": "input.requested", "sessionId": sid, "entry": _entry(row)}))
        status = "waiting_approval" if args.kind == "approval" else "waiting_input"
        changed = conn.execute(
            "UPDATE ai_sessions SET status = %s, updated_at = now()"
            " WHERE id = %s AND status <> %s RETURNING status",
            (status, sid, status),
        ).fetchone()
        if changed is not None:
            events.append(_event({"type": "session.status", "sessionId": sid, "status": status}))
        _append(conn, logged, sid, events)
    return Written(True, logged)


def _stand(
    request_id: str,
    session_id: str,
    requested_by: dict[str, Any],
    tool: str,
    input_hash: str,
    summary: str,
) -> dict[str, Any]:
    """The audit's view of a call no entry was opened for: the grant and deny paths'."""
    return {
        "request_id": request_id,
        "session_id": session_id,
        "requested_by": requested_by,
        "tool": tool,
        "input_hash": input_hash,
        "summary": summary,
        "created_at": datetime.now(UTC),
    }


def grant_input(
    conn: Connection[Any],
    *,
    request_id: str,
    session_id: str,
    tool: str,
    summary: str,
    input_hash: str,
    grant_id: str,
    requested_by: dict[str, Any],
) -> bool:
    """Records the run grant's approval of a call, only if the backend recorded the grant's use
    of this request id, by this session, for this very hash, and the grant is in force: one
    transaction under the grant row's share lock, so a revocation lands wholly before or after
    it. Nothing of the verdict is taken on trust. False when it does not hold."""
    responder = run_grant_responder(grant_id)
    with conn.transaction():
        holds = conn.execute(
            "SELECT 1 FROM flow_grants g JOIN flow_grant_uses u ON u.grant_id = g.id"
            " WHERE g.id = %s AND u.request_id = %s AND u.session_id = %s"
            " AND u.input_hash = %s AND g.status = 'approved' AND g.revoked_at IS NULL"
            " AND now() < g.expires_at FOR SHARE OF g",
            (grant_id, request_id, session_id, input_hash),
        ).fetchone()
        if holds is None:
            return False
        inserted = conn.execute(
            "INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder)"
            " VALUES (%s, %s, 'approval', 'approved', %s)"
            " ON CONFLICT (request_id) DO NOTHING RETURNING request_id",
            (request_id, session_id, Jsonb(responder)),
        ).fetchone()
        if inserted is None:
            # A retried activity: the same grant's approval is there already.
            held = conn.execute(
                "SELECT outcome, responder FROM ai_input_responses WHERE request_id = %s",
                (request_id,),
            ).fetchone()
            if held is None:
                return False
            return bool(held["outcome"] == "approved" and held["responder"] == responder)
        row = _stand(request_id, session_id, requested_by, tool, input_hash, summary)
        _audit(conn, row, "approved", responder, None)
    return True


def deny_input(
    conn: Connection[Any],
    *,
    request_id: str,
    session_id: str,
    run_id: str,
    tool: str,
    summary: str,
    input_hash: str,
    reason: str | None,
    requested_by: dict[str, Any],
) -> bool:
    """Records the flow's refusal of a call (``on_outside="deny"``); False when the request id
    was resolved already (a retried activity writes nothing twice)."""
    responder = flow_responder(run_id)
    with conn.transaction():
        inserted = conn.execute(
            "INSERT INTO ai_input_responses"
            " (request_id, session_id, kind, outcome, responder, reason, hops)"
            " VALUES (%s, %s, 'approval', 'denied', %s, %s, %s)"
            " ON CONFLICT (request_id) DO NOTHING RETURNING request_id",
            (
                request_id,
                session_id,
                Jsonb(responder),
                reason,
                Jsonb([_hop("person", "deny", "flow", reason)]),
            ),
        ).fetchone()
        if inserted is None:
            return False
        row = _stand(request_id, session_id, requested_by, tool, input_hash, summary)
        _audit(conn, row, "denied", responder, reason)
    return True


def _lock_session_of(conn: Connection[Any], request_id: str) -> None:
    """The lock of the session ``request_id``'s entry is in, before anything of the entry: two
    resolutions in one session then queue on it, never deadlock (the agent's ``lockSessionOf``)."""
    conn.execute(
        "SELECT 1 FROM ai_sessions WHERE id ="
        " (SELECT session_id FROM ai_pending_input WHERE request_id = %s) FOR UPDATE",
        (request_id,),
    )


def resolve_input(
    conn: Connection[Any],
    *,
    request_id: str,
    outcome: Outcome,
    responder: dict[str, Any],
    response: Any = None,
    digest: str | None = None,
    reason: str | None = None,
) -> Written:
    """Resolves the entry; ``done`` False when it was gone (resolved by another, or never
    opened). ``digest`` is the hash of the response as the person sent it (#3724)."""
    logged: list[Logged] = []
    with conn.transaction():
        _lock_session_of(conn, request_id)
        row = conn.execute(
            "DELETE FROM ai_pending_input WHERE request_id = %s RETURNING *", (request_id,)
        ).fetchone()
        if row is None:
            return Written(False)
        _resolved(conn, logged, row, outcome, responder, response, reason, digest)
    return Written(True, logged)


def _who(value: Any) -> Principal | None:
    if not isinstance(value, dict):
        return None
    return Principal(kind=str(value.get("kind")), id=str(value.get("id")))


def gate_entry(row: dict[str, Any], record: dict[str, Any]) -> GateEntry:
    """The validator's entry of a row and its ``gate`` (the agent's ``GateRecord``, camelCase),
    in state ``resolving`` once the row is past its timer, as the agent's ``decideInput``."""
    questions = record.get("questions")
    options = record.get("options")
    return GateEntry(
        id=row["request_id"],
        kind=row["kind"],
        state="resolving" if row["expired"] else "pending",
        input_hash=record.get("inputHash"),
        requested_by=_who(record.get("requestedBy")),
        session_owner=_who(record.get("sessionOwner")),
        session_creator=_who(record.get("sessionCreator")),
        questions=(
            None
            if questions is None
            else tuple(
                Question(str(q.get("question")), bool(q.get("multiSelect"))) for q in questions
            )
        ),
        options=None if options is None else tuple(str(o) for o in options),
        in_run=bool(record.get("inRun", False)),
    )


def decide_input(
    conn: Connection[Any],
    *,
    request_id: str,
    response: Any,
    responder: dict[str, Any],
    role: Role,
    live_run: str | None,
    check_live: bool,
    digest: str | None = None,
) -> Decided | LiveCheck | Literal["workflow"]:
    """Decides a request whose row is its record (R5, the agent's ``decideInput``): under the
    row's lock the validator against the row's ``gate``, then the row removed and its
    resolution written in the same transaction. Raises ``RespondRefused`` (``stale`` for no row).

    ``workflow`` when its workflow holds the entry (no ``gate``). With ``check_live`` the row's
    run must be ``live_run``, which the agent has just found running: otherwise the answer is
    ``LiveCheck`` and nothing is written, so a response the validator refuses costs no call to
    Temporal, as before. ``digest`` is the agent's hash of the response as the person sent
    it, before its strings were made well formed (#3724); without one, the hash of ``response``."""
    logged: list[Logged] = []
    with conn.transaction():
        _lock_session_of(conn, request_id)
        row = conn.execute(
            "SELECT *, expires_at <= now() AS expired FROM ai_pending_input"
            " WHERE request_id = %s FOR UPDATE",
            (request_id,),
        ).fetchone()
        if row is None:
            raise RespondRefused(
                "stale", f"no pending input {request_id}: it is stale or was never asked"
            )
        record = row["gate"]
        if not isinstance(record, dict):
            return "workflow"
        valid = validate_respond(
            gate_entry(row, record),
            RespondRequest(
                request_id=request_id,
                response=response,
                responder=Principal(kind=str(responder["kind"]), id=str(responder["id"])),
                role=role,
            ),
        )
        if check_live and row["workflow_run_id"] != live_run:
            return LiveCheck(workflow_id=row["workflow_id"], run_id=row["workflow_run_id"])
        if valid.decision is not None:
            outcome: Outcome = "approved" if valid.decision == "approve" else "denied"
            recorded: dict[str, Any] = {"decision": valid.decision}
        else:
            outcome = "answered"
            recorded = {"answers": list(valid.answers)}
        conn.execute("DELETE FROM ai_pending_input WHERE request_id = %s", (request_id,))
        digest = response_hash(response) if digest is None else digest
        _resolved(conn, logged, row, outcome, responder, recorded, None, digest)
        return Decided(kind=row["kind"], outcome=outcome, logged=logged)


def read_input(conn: Connection[Any], request_id: str) -> dict[str, Any] | None:
    """The response row of ``request_id`` (what the workflow's ``gate.read_input`` returns), or
    None while the request is open or was never asked."""
    row: dict[str, Any] | None = conn.execute(
        "SELECT outcome, response, responder, reason FROM ai_input_responses WHERE request_id = %s",
        (request_id,),
    ).fetchone()
    return row
