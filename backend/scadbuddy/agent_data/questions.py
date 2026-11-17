"""The agent's questions and `done` summaries (#5299 step 10), for ``api/crud_agent_questions.py``
area ``questions``: the SQL of the agent's ``gate/questions.ts`` `QuestionService` and
``gate/doneSummary.ts`` ``loadDoneSummary``, statement for statement.

Each write is one transaction with its session events and its audit rows, so a write never
lands without them: a summary's dismissal (``answer``), and a turn's ``done`` post (``post_done``),
which writes only while the turn's placement holds the session (``turn_attempt`` and
``durable_turn_id``, read ``FOR UPDATE`` first) and the session is still the browser user's. The
attention rate limit's count (``attention_count``) is read only while its caller holds the lease
``attention-rate`` (``agent_data/leases.py``), so two API-path requests at the limit cannot both
pass; without the lease it refuses, never counts. During a roll an agent pod still on SQL takes
``ATTENTION_RATE_LOCK`` and does not read the lease, so it can count while a lease is held: the
acquire's try-lock only makes a lease ``busy`` while such a pod counts.

The summary's text is the agent's: it reads the facts here (``done_facts``), redacts the turn's
secrets and writes the text, which never leaves the agent with a secret in it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from psycopg import Connection
from psycopg.types.json import Jsonb

from scadbuddy.agent_data import audit_log, gate, leases

#: The most summaries one list answers (the agent's ``PENDING_CAP``).
PENDING_CAP = 500
#: A turn's most ``done`` posts (the agent's ``DONE_POSTS_PER_TURN``).
DONE_POSTS_PER_TURN = 3
#: The attention limits' window (the agent's ``ATTENTION_RATE_WINDOW_S``).
ATTENTION_RATE_WINDOW_S = 600
#: The lease the count is read under, and the SQL path's lock it stands for.
ATTENTION_LEASE = "attention-rate"
ATTENTION_RATE_LOCK = "scadbuddy:attention-rate"
#: The agent's ``WAIT_FOR_USER`` and ``ATTENTION_TOOL``.
WAIT_FOR_USER = "wait_for_user"
ATTENTION_TOOL = "mcp__scadbuddy_questions__request_user_attention"
REPLACED = "replaced by a newer request for the same reason"
#: The agent's ``isDoneSummary``.
DONE = "(q.attention_reason IS NOT DISTINCT FROM 'done' AND q.expires_at IS NULL)"


class LeaseLostError(Exception):
    """The caller does not hold the lease the count is read under."""


class RefusedError(Exception):
    def __init__(self, code: Literal["not_found", "conflict", "invalid"], message: str) -> None:
        super().__init__(message)
        self.code = code


def entry(conn: Connection[Any], question_id: str) -> dict[str, Any] | None:
    if not gate._is_uuid(question_id):
        return None
    row: dict[str, Any] | None = conn.execute(
        "SELECT session_id::text AS session_id, kind, questions, outcome, reason, tool"
        " FROM ai_questions WHERE id = %s",
        (question_id,),
    ).fetchone()
    return row


def pending(conn: Connection[Any], kind: str, principal: str) -> list[dict[str, Any]]:
    """The undismissed ``done`` summaries of the sessions ``principal`` owns, newest first, one
    past the cap (so the caller can say there were more)."""
    return conn.execute(
        "SELECT q.id::text AS id, q.session_id::text AS session_id, q.kind, q.tool, q.tool_use_id,"
        " q.questions, q.attention_reason, q.on_timeout, q.summary, q.created_at, q.expires_at"
        " FROM ai_questions q JOIN ai_sessions s ON s.id = q.session_id"
        " WHERE s.owner_kind = %(k)s AND (s.owner_id = %(i)s OR s.owner_kind = 'browser')"
        f" AND q.outcome IS NULL AND {DONE}"
        " ORDER BY q.created_at DESC, q.id DESC LIMIT %(limit)s",
        {"k": kind, "i": principal, "limit": PENDING_CAP + 1},
    ).fetchall()


def gate_answer(conn: Connection[Any], request_id: str) -> dict[str, Any] | None:
    """An AgentSession's answered entry's response row, for its ``question`` audit row."""
    row: dict[str, Any] | None = conn.execute(
        "SELECT session_id::text AS session_id, tool, requested_at, response"
        " FROM ai_input_responses WHERE request_id = %s AND kind = 'answer'"
        " AND outcome = 'answered'",
        (request_id,),
    ).fetchone()
    return row


def _question_audit(
    conn: Connection[Any],
    *,
    action: str,
    surface: str,
    actor: dict[str, str],
    outcome: str,
    session_id: str,
    detail: str,
    started_at: datetime,
    turn_id: str | None = None,
    tool_use_id: str | None = None,
    input_hash: str | None = None,
    client_ip: str | None = None,
) -> None:
    audit_log.insert(
        conn,
        audit_log.Entry(
            kind="question",
            action=action,
            surface=surface,
            actor_kind=audit_log.cap(actor["kind"], 50),
            actor_id=audit_log.cap(actor["id"], 200),
            actor_label=audit_log.cap(actor["label"], 200),
            outcome=outcome,
            client_ip=client_ip,
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            tier="read",
            input_hash=input_hash,
            # The agent's SQL path wrote safeDetail(detail, []): redact with no secrets changes
            # nothing, so that is cap(detail, 500), the same text.
            detail=audit_log.cap(detail, 500),
            started_at=started_at,
            finished_at=datetime.now(UTC),
        ),
    )


def _answers(n: int) -> str:
    return f"{n} answer{'' if n == 1 else 's'}"


def answer(
    conn: Connection[Any],
    *,
    question_id: str,
    session_id: str,
    answers: list[str],
    by: dict[str, str],
    input_hash: str | None,
    surface: str,
    client_ip: str | None,
) -> list[gate.Logged]:
    """A ``done`` summary's dismissal by the user (``QuestionService.answer``), with its events
    and its ``question`` audit row (the answers as the agent's hash, never their text)."""
    if not (gate._is_uuid(question_id) and gate._is_uuid(session_id)):
        raise RefusedError("not_found", f"no question {question_id} in this session")
    logged: list[gate.Logged] = []
    with conn.transaction():
        conn.execute("SELECT 1 FROM ai_sessions WHERE id = %s FOR UPDATE", (session_id,))
        asked = conn.execute(
            "SELECT questions, outcome FROM ai_questions WHERE id = %s AND session_id = %s",
            (question_id, session_id),
        ).fetchone()
        if asked is None:
            raise RefusedError("not_found", f"no question {question_id} in this session")
        if asked["outcome"] is not None:
            raise RefusedError(
                "conflict", f"question {question_id} is no longer waiting for an answer"
            )
        if len(answers) != len(asked["questions"]) or any(not a.strip() for a in answers):
            raise RefusedError(
                "invalid",
                f"question {question_id} needs one answer for each of its"
                f" {len(asked['questions'])} questions",
            )
        row = conn.execute(
            "UPDATE ai_questions SET outcome = 'answered', answers = %s, resolved_at = now(),"
            " answered_by_kind = %s, answered_by_id = %s, answered_by_label = %s"
            " WHERE id = %s AND session_id = %s AND outcome IS NULL"
            " RETURNING turn_id::text AS turn_id, tool, tool_use_id, created_at",
            (Jsonb(answers), by["kind"], by["id"], by["label"], question_id, session_id),
        ).fetchone()
        if row is None:
            raise RefusedError(
                "conflict", f"question {question_id} is no longer waiting for an answer"
            )
        events = [
            gate._event(
                {
                    "type": "question.resolved",
                    "sessionId": session_id,
                    "id": question_id,
                    "answered": True,
                    "answers": answers,
                    "by": by,
                }
            ),
            gate._event(
                {
                    "type": "input.resolved",
                    "sessionId": session_id,
                    "id": f"question:{question_id}",
                    "kind": "answer",
                    "outcome": "answered",
                }
            ),
        ]
        gate._append(conn, logged, session_id, events)
        _question_audit(
            conn,
            action="answered",
            surface=surface,
            actor=by,
            outcome="ok",
            session_id=session_id,
            turn_id=row["turn_id"],
            tool_use_id=row["tool_use_id"],
            input_hash=input_hash,
            client_ip=client_ip,
            detail=f"{row['tool']} question {question_id}: {_answers(len(answers))}",
            started_at=row["created_at"],
        )
    return logged


def attention_count(conn: Connection[Any], *, own: bool, lease_token: str) -> int:
    """The gate's ``wait_for_user`` entries in the window, open or resolved: the model's own
    requests (``own``) or the tab waits (call id ``tab:<tool_use id>``). Only while
    ``lease_token`` holds ``attention-rate``, locked for this transaction."""
    if not gate._is_uuid(lease_token):
        raise LeaseLostError
    with conn.transaction():
        if not leases.held_in(conn, ATTENTION_LEASE, lease_token):
            raise LeaseLostError
        row = conn.execute(
            "SELECT (SELECT count(*) FROM ai_pending_input WHERE tool = %(t)s"
            " AND created_at > now() - make_interval(secs => %(w)s)"
            " AND (request_id LIKE %(tab)s) <> %(own)s)"
            " + (SELECT count(*) FROM ai_input_responses WHERE tool = %(t)s"
            " AND requested_at > now() - make_interval(secs => %(w)s)"
            " AND (request_id LIKE %(tab)s) <> %(own)s) AS n",
            {"t": WAIT_FOR_USER, "w": ATTENTION_RATE_WINDOW_S, "tab": "%:tab:%", "own": own},
        ).fetchone()
        return int(row["n"]) if row is not None else 0


def done_facts(
    conn: Connection[Any], session_id: str, turn_id: str, since: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``loadDoneSummary``'s two reads: the windows nobody answered in, and the touches since
    ``since`` (Postgres's text of the claim's time) or since the earliest window."""
    if not (gate._is_uuid(session_id) and gate._is_uuid(turn_id)):
        return [], []
    with conn.transaction():
        away = conn.execute(
            "WITH RECURSIVE turns (id) AS ("
            " SELECT %(turn)s::uuid UNION"
            " SELECT a.turn_id FROM ai_approvals a JOIN turns t ON a.resume_turn_id = t.id"
            " WHERE a.session_id = %(sid)s AND a.turn_id IS NOT NULL)"
            " SELECT id, away_from, away_until, away_from::text AS from_text,"
            " away_until::text AS until_text FROM ("
            " SELECT q.id::text AS id, q.created_at AS away_from, reply.at AS away_until"
            " FROM ai_questions q CROSS JOIN LATERAL ("
            " SELECT min(at) AS at FROM ("
            " SELECT resolved_at AS at FROM ai_questions WHERE session_id = %(sid)s"
            " AND turn_id = q.turn_id AND outcome = 'answered' AND resolved_at > q.created_at"
            " UNION ALL SELECT decided_at FROM ai_approvals WHERE session_id = %(sid)s"
            " AND turn_id = q.turn_id AND decision IN ('approved', 'denied')"
            " AND decided_by_kind = 'browser' AND decided_at > q.created_at) replies) reply"
            " WHERE q.session_id = %(sid)s AND q.turn_id IN (SELECT id FROM turns)"
            " AND q.kind = 'attention' AND q.outcome = 'timed_out'"
            " UNION ALL"
            " SELECT right(substring(r.request_id FROM '[^:]*$'), 8), r.requested_at, reply.at"
            " FROM ai_input_responses r CROSS JOIN LATERAL ("
            " SELECT min(created_at) AS at FROM ai_input_responses WHERE session_id = %(sid)s"
            " AND requested_at >= %(since)s::text::timestamptz AND created_at > r.requested_at"
            " AND (outcome = 'answered' OR (kind = 'approval' AND outcome IN ('approved',"
            " 'denied') AND responder->>'kind' = 'browser'))) reply"
            " WHERE r.session_id = %(sid)s AND r.tool = %(wait)s AND r.outcome = 'timed_out'"
            " AND r.requested_at >= %(since)s::text::timestamptz"
            " ) windows ORDER BY away_from, id",
            {"turn": turn_id, "sid": session_id, "since": since, "wait": WAIT_FOR_USER},
        ).fetchall()
        bounds = [{"f": w["from_text"], "u": w["until_text"]} for w in away]
        earliest = min((w["away_from"] for w in away), default=None)
        touches = conn.execute(
            "SELECT at, at >= %(since)s::text::timestamptz AS in_turn,"
            " EXISTS (SELECT 1 FROM jsonb_to_recordset(%(bounds)s) AS w(f timestamptz,"
            " u timestamptz) WHERE at >= w.f AND (w.u IS NULL OR at < w.u)) AS in_away,"
            " coalesce(at < %(first)s::text::timestamptz, false) AS before_asked,"
            " tool, resource_type, resource_id, action, model_slug"
            " FROM ai_session_resources WHERE session_id = %(sid)s"
            " AND (at >= %(since)s::text::timestamptz OR at >= %(earliest)s::timestamptz)"
            " ORDER BY id",
            {
                "since": since,
                "bounds": Jsonb(bounds),
                "first": bounds[0]["f"] if bounds else None,
                "sid": session_id,
                "earliest": earliest,
            },
        ).fetchall()
    return list(away), list(touches)


@dataclass(frozen=True)
class DonePost:
    """A turn's ``done`` summary, its card redacted and its text written by the agent."""

    question_id: str
    session_id: str
    turn_id: str
    attempt: str
    tool: str
    tool_use_id: str
    questions: list[dict[str, Any]]
    summary: str
    unattended: bool


@dataclass
class Posted:
    asked: bool
    limited: str | None
    logged: list[gate.Logged]


def _question_entry(p: DonePost, created_at: datetime) -> dict[str, Any]:
    """The agent's ``questionEntry`` of a ``done`` summary."""
    return {
        "id": f"question:{p.question_id}",
        "kind": "answer",
        "session_id": p.session_id,
        "tool": p.tool,
        "summary": "",
        "input_hash": None,
        "prompt": "\n".join(str(q.get("question", "")) for q in p.questions),
        "requested_by": None,
        "responders": ["browser"],
        "created_at": gate._iso(created_at),
        "expires_at": None,
        "attention": {"reason": "done", "on_timeout": None, "summary": p.summary},
    }


def post_done(conn: Connection[Any], p: DonePost) -> Posted:
    """``QuestionService.gate``'s post: only while the turn's placement holds the session and
    the session is the browser user's; at most ``DONE_POSTS_PER_TURN`` a turn; a summary of the
    same kind the turn or an attended one replaces is cancelled first."""
    none = Posted(asked=False, limited=None, logged=[])
    if not (
        gate._is_uuid(p.session_id) and gate._is_uuid(p.turn_id) and gate._is_uuid(p.question_id)
    ):
        return none
    logged: list[gate.Logged] = []
    sid, turn = p.session_id, p.turn_id
    with conn.transaction():
        owner = conn.execute(
            "SELECT owner_kind FROM ai_sessions WHERE id = %s AND durable_turn_id = %s"
            " AND turn_attempt = %s FOR UPDATE",
            (sid, turn, p.attempt),
        ).fetchone()
        if owner is None or owner["owner_kind"] != "browser":
            return none
        count = conn.execute(
            "SELECT count(*)::int AS n FROM ai_questions WHERE session_id = %s AND turn_id = %s"
            " AND kind = 'attention' AND attention_reason = 'done'",
            (sid, turn),
        ).fetchone()
        if (count["n"] if count else 0) >= DONE_POSTS_PER_TURN:
            limited = (
                f"The summary was not posted: this turn already posted {DONE_POSTS_PER_TURN},"
                " the most a turn may."
            )
            _question_audit(
                conn,
                action="rate_limited",
                surface="system",
                actor=gate.SYSTEM,
                outcome="refused",
                session_id=sid,
                turn_id=turn,
                tool_use_id=p.tool_use_id,
                detail=f"{p.tool} done summary refused: {limited}",
                started_at=datetime.now(UTC),
            )
            return Posted(asked=False, limited=limited, logged=[])
        superseded = conn.execute(
            "UPDATE ai_questions SET outcome = 'cancelled', reason = %(r)s, resolved_at = now()"
            " WHERE session_id = %(sid)s AND kind = 'attention' AND attention_reason = 'done'"
            " AND outcome IS NULL AND (NOT unattended OR turn_id = %(turn)s)"
            " AND (tool = %(at)s) = %(same)s"
            " RETURNING id::text AS id, turn_id::text AS turn_id, tool, tool_use_id, created_at",
            {
                "r": REPLACED,
                "sid": sid,
                "turn": turn,
                "at": ATTENTION_TOOL,
                "same": p.tool == ATTENTION_TOOL,
            },
        ).fetchall()
        events: list[dict[str, Any]] = []
        for r in superseded:
            events.append(
                gate._event(
                    {
                        "type": "question.resolved",
                        "sessionId": sid,
                        "id": r["id"],
                        "answered": False,
                        "reason": REPLACED,
                    }
                )
            )
            events.append(
                gate._event(
                    {
                        "type": "input.resolved",
                        "sessionId": sid,
                        "id": f"question:{r['id']}",
                        "kind": "answer",
                        "outcome": "cancelled",
                        "reason": REPLACED,
                    }
                )
            )
        inserted = conn.execute(
            "INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind,"
            " attention_reason, summary, unattended) VALUES (%s, %s, %s, %s, %s, %s, 'attention',"
            " 'done', %s, %s) RETURNING created_at",
            (
                p.question_id,
                sid,
                turn,
                p.tool,
                p.tool_use_id,
                Jsonb(p.questions),
                p.summary,
                p.unattended,
            ),
        ).fetchone()
        created_at = inserted["created_at"] if inserted else datetime.now(UTC)
        events.append(
            gate._event(
                {
                    "type": "question.asked",
                    "sessionId": sid,
                    "id": p.question_id,
                    "tool": p.tool_use_id,
                    "questions": p.questions,
                    "attention": {"reason": "done", "summary": p.summary},
                }
            )
        )
        events.append(
            gate._event(
                {
                    "type": "input.requested",
                    "sessionId": sid,
                    "entry": _question_entry(p, created_at),
                }
            )
        )
        gate._append(conn, logged, sid, events)
        for r in superseded:
            _question_audit(
                conn,
                action="cancelled",
                surface="system",
                actor=gate.SYSTEM,
                outcome="refused",
                session_id=sid,
                turn_id=r["turn_id"],
                tool_use_id=r["tool_use_id"],
                detail=f"{r['tool']} attention request {r['id']}: {REPLACED}",
                started_at=r["created_at"],
            )
    return Posted(asked=True, limited=None, logged=logged)
