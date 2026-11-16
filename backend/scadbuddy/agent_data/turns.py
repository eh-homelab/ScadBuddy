"""A durable turn's claim, placement, spend and end behind CRUD routes (#5299 step 9, #6449),
for ``api/crud_agent_turns.py`` (area ``session-turns``): the ``ai_sessions`` writes a turn
makes, each with its events in one transaction (#5299 D2).

Each function is the SQL of ``agent/src/sessions/turnRows.ts`` `PgTurnRows`, statement for
statement: a send's claim and give-back (``durable.ts``), a placement's claim, the only writer
of ``turn_attempt`` (``turnHost.ts`` ``#claim``), its spend, the turn's end with the queue's
head claimed in the same transaction (``temporal/session/finishActivity.ts``), and the end of a
turn whose workflow is gone (``durableSweep.ts`` ``endLostTurn``). The fences are the agent's:
a turn's end only while the row runs that turn (``durable_turn_id``), a lost turn's end only
while the row is as the sweep read it (``event_seq``), a placement's claim only while the row
runs its turn.

A turn's closing events come from the agent with its spend left out; the end words them with
the lineage's spend and budget read under the row's lock (``turnCore.ts`` ``wordTail``)."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from psycopg import Connection
from psycopg.types.json import Jsonb

from scadbuddy.agent_data import events as event_log
from scadbuddy.agent_data.sessions import POOL_BUDGET, POOL_COST

#: A durable row's statuses while it holds a turn (``durableSweep.ts`` ``TURN_STATUSES``).
RUNNING = ["running", "waiting_approval", "waiting_input"]
#: The code of the ``error`` event that says a queued message could not start.
QUEUED_DROPPED = "queued_message_dropped"
PROTOCOL_VERSION = 1
#: What a queue claim came to.
Outcome = Literal["started", "busy", "dropped"]
#: How long a write waits for its row's lock before it fails (``LockNotAvailable``, which the
#: route answers 503 ``row_busy``): a write that waited past the agent's request (30 s) would
#: commit after the agent gave up on it.
LOCK_TIMEOUT_MS = 10_000


@contextmanager
def _write(conn: Connection[Any]) -> Iterator[None]:
    """A write's transaction, its lock waits bounded by ``LOCK_TIMEOUT_MS``."""
    with conn.transaction():
        conn.execute(f"SET LOCAL lock_timeout = {int(LOCK_TIMEOUT_MS)}".encode())
        yield


class Logged:
    """Events appended in a transaction: their JSON texts and seqs."""

    def __init__(self, texts: list[str], seqs: list[int]) -> None:
        self.texts = texts
        self.seqs = seqs

    def wire(self) -> dict[str, Any]:
        return {"events": self.texts, "seqs": self.seqs}


def js_number(value: float) -> str:
    """A float as ``JSON.stringify`` writes it (ECMA-262 ``Number::toString``): the shortest
    digits that read back as it (Python's ``repr`` picks the same), in fixed notation for
    1e-6 <= |x| < 1e21 and as ``1.5e+21`` or ``1e-7`` outside it; NaN and the infinities as
    ``null``."""
    if math.isnan(value) or math.isinf(value):
        return "null"
    if value == 0:
        return "0"
    exact = Decimal(repr(value)).normalize()
    if Decimal("1e-6") <= abs(exact) < Decimal("1e21"):
        return format(exact, "f")
    sign, digits, exponent = exact.as_tuple()
    assert isinstance(exponent, int)
    text = "".join(map(str, digits)).rstrip("0") or "0"
    power = len(digits) + exponent - 1
    mantissa = text[0] + ("." + text[1:] if len(text) > 1 else "")
    return ("-" if sign else "") + mantissa + "e" + ("+" if power >= 0 else "-") + str(abs(power))


def _js(value: Any) -> str:
    if value is None or value is True or value is False or isinstance(value, (int, str)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float):
        return js_number(value)
    if isinstance(value, dict):
        return "{" + ",".join(f"{_js(str(k))}:{_js(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_js(v) for v in value) + "]"
    raise TypeError(f"{type(value).__name__} is not JSON")


def dumps(event: dict[str, Any]) -> str:
    """``JSON.stringify``'s text of an event: its numbers as JavaScript writes them."""
    return _js(event)


def usd(amount: float) -> str:
    """``$`` and ``amount.toFixed(2)``: the exact value rounded, a tie away from zero."""
    return "$" + str(Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def word_tail(
    tail: list[dict[str, Any]], spent: float, budget: float, turns: int | None
) -> list[dict[str, Any]]:
    """``turnCore.ts`` ``wordTail``: the result and a budget error with the row's figures."""
    out = []
    for e in tail:
        if e.get("type") == "session.result":
            e = {**e, "costUsd": spent, "budgetUsd": budget}
            if turns is not None:
                e["turns"] = turns
        elif e.get("type") == "error" and e.get("code") == "error_max_budget_usd":
            e = {**e, "message": f"this chat used its {usd(budget)} budget ({usd(spent)} spent)"}
        out.append(e)
    return out


def _append(conn: Connection[Any], session_id: str, texts: list[str]) -> Logged:
    return Logged(texts, event_log.append_in(conn, session_id, texts))


def send_claim(
    conn: Connection[Any],
    session_id: str,
    author: tuple[str, str],
    tiers: dict[str, Any] | None,
    turn_id: str,
    bare: bool,
) -> dict[str, Any] | None:
    """``durable.ts`` ``#send``'s claim: the row runs ``turn_id`` while ``author`` owns it and it
    is free, not done, archived or spent. The status before, and the runtime."""
    with _write(conn):
        return _send_claim(conn, session_id, author, tiers, turn_id, bare)


def _send_claim(
    conn: Connection[Any],
    session_id: str,
    author: tuple[str, str],
    tiers: dict[str, Any] | None,
    turn_id: str,
    bare: bool,
) -> dict[str, Any] | None:
    row: dict[str, Any] | None = conn.execute(
        (
            "WITH prior AS (SELECT status AS prior_status FROM ai_sessions WHERE id = %(id)s"
            " FOR UPDATE)"
            " UPDATE ai_sessions SET status = 'running', turn_tiers = %(tiers)s,"
            " durable_turn_id = %(turn)s, turn_flow = %(bare)s, updated_at = now(),"
            " turn_started_at = now(), interrupt_requested = false,"
            " runtime = coalesce(runtime, 'unified')"
            " FROM prior"
            " WHERE id = %(id)s AND owner_kind = %(kind)s"
            " AND (owner_id = %(owner)s OR owner_kind = 'browser')"
            " AND status NOT IN ('running', 'waiting_approval', 'waiting_input', 'done')"
            f" AND archived_at IS NULL AND {POOL_COST} < {POOL_BUDGET}"
            " RETURNING prior.prior_status, ai_sessions.runtime"
        ).encode(),
        {
            "id": session_id,
            "kind": author[0],
            "owner": author[1],
            "tiers": None if tiers is None else Jsonb(tiers),
            "turn": turn_id,
            "bare": bare,
        },
    ).fetchone()
    return row


def queueable(conn: Connection[Any], session_id: str, author: tuple[str, str]) -> int | None:
    """``durable.ts`` ``#queue``'s read: the row's ``event_seq`` while it runs a turn ``author``
    may queue behind, else None."""
    row = conn.execute(
        (
            "SELECT event_seq AS seq FROM ai_sessions"
            " WHERE id = %s AND owner_kind = %s AND (owner_id = %s OR owner_kind = 'browser')"
            " AND status IN ('running', 'waiting_approval', 'waiting_input')"
            " AND runtime = 'unified'"
            f" AND archived_at IS NULL AND {POOL_COST} < {POOL_BUDGET}"
        ).encode(),
        (session_id, author[0], author[1]),
    ).fetchone()
    return None if row is None else int(row["seq"])


def state(conn: Connection[Any], session_id: str, grace_ms: int) -> dict[str, Any] | None:
    """The row's turn as the turn's readers read it: who runs it and where, whether a Stop asked
    for its end, and whether it was written to within ``grace_ms``."""
    row: dict[str, Any] | None = conn.execute(
        "SELECT status, durable_turn_id::text AS durable_turn_id, turn_attempt,"
        " interrupt_requested, owner_kind, runtime, event_seq::text AS event_seq, cost_usd,"
        " updated_at < now() - make_interval(secs => %s) AS settled"
        " FROM ai_sessions WHERE id = %s",
        (grace_ms / 1000, session_id),
    ).fetchone()
    return row


def give_back(conn: Connection[Any], session_id: str, prior: str) -> Logged | None:
    """``durable.ts`` ``#giveBack``: a refused send's claim given back to ``prior``, with its
    ``session.status``, in one transaction; None when the row no longer runs."""
    with _write(conn):
        cur = conn.execute(
            "UPDATE ai_sessions SET status = %s, durable_turn_id = NULL, updated_at = now()"
            " WHERE id = %s AND status = 'running'",
            (prior, session_id),
        )
        if cur.rowcount == 0:
            return None
        status = {"v": PROTOCOL_VERSION, "type": "session.status", "sessionId": session_id}
        return _append(conn, session_id, [dumps({**status, "status": prior})])


def interrupt(conn: Connection[Any], session_id: str) -> bool:
    """``durable.ts`` ``interrupt``'s flag, while a turn holds the row."""
    with _write(conn):
        cur = conn.execute(
            "UPDATE ai_sessions SET interrupt_requested = true"
            " WHERE id = %s AND durable_turn_id IS NOT NULL",
            (session_id,),
        )
        return cur.rowcount > 0


def placement(
    conn: Connection[Any], session_id: str, turn_id: str, attempt: str, mark_key: str
) -> int | None:
    """``turnHost.ts`` ``#claim``, the only writer of ``turn_attempt``: the placement takes the
    turn while the row runs it, and the transcript's mark is set once per ``mark_key``."""
    with _write(conn):
        row = conn.execute(
            "UPDATE ai_sessions SET turn_attempt = %(attempt)s,"
            " turn_mark = CASE WHEN turn_mark_turn = %(key)s THEN turn_mark ELSE ("
            " SELECT coalesce(max(e.id), 0) FROM ai_session_entries e"
            " WHERE e.session_id = ai_sessions.id::text AND e.subpath = ''"
            " ) END,"
            " turn_mark_turn = %(key)s"
            " WHERE id = %(id)s AND durable_turn_id = %(turn)s"
            " AND status IN ('running', 'waiting_approval', 'waiting_input')"
            " RETURNING turn_mark::text",
            {"attempt": attempt, "key": mark_key, "id": session_id, "turn": turn_id},
        ).fetchone()
        return None if row is None else int(row["turn_mark"])


#: How long a spend's key is kept (``ai_turn_spend_keys``): far past a placement's resend.
SPEND_KEY_TTL_S = 3600


def spend(
    conn: Connection[Any],
    session_id: str,
    cost_usd: float,
    unpriced_usd: float,
    turns: int,
    key: str,
) -> bool:
    """``turnHost.ts`` ``Spend.flush`` and ``#write``: what a placement spent, added whether or
    not it still holds the row, once per ``key`` (a resend of a write whose answer was lost adds
    nothing); False for a session that is gone."""
    # One statement: the row locked, the session's old keys let go, the key taken, and the spend
    # added only when the key was new.
    with _write(conn):
        row = conn.execute(
            "WITH s AS (SELECT id FROM ai_sessions WHERE id = %(id)s FOR UPDATE),"
            " old AS (DELETE FROM ai_turn_spend_keys WHERE session_id = %(id)s"
            " AND created_at < now() - make_interval(secs => %(ttl)s)),"
            " k AS (INSERT INTO ai_turn_spend_keys (session_id, key) SELECT id, %(key)s FROM s"
            " ON CONFLICT DO NOTHING RETURNING session_id),"
            " u AS (UPDATE ai_sessions SET cost_usd = cost_usd + %(cost)s,"
            " turns = turns + %(turns)s, unpriced_cost_usd = unpriced_cost_usd + %(unpriced)s,"
            " updated_at = now() WHERE id IN (SELECT session_id FROM k) RETURNING id)"
            " SELECT EXISTS (SELECT 1 FROM s) AS found, (SELECT count(*) FROM u) AS added",
            {
                "id": session_id,
                "ttl": SPEND_KEY_TTL_S,
                "key": key,
                "cost": cost_usd,
                "turns": turns,
                "unpriced": unpriced_usd,
            },
        ).fetchone()
        return row is not None and bool(row["found"])


def _why_not(row: dict[str, Any], author: tuple[str, str]) -> str | None:
    """``finishActivity.ts`` ``whyNot``: why the session cannot run a queued message now."""
    if row["status"] == "done":
        return "the session is done"
    if row["archived"]:
        return "the session is archived"
    if row["runtime"] == "durable":
        return "the session's conversation ended with the old session runtime"
    owner_kind, owner_id = row["owner_kind"], row["owner_id"]
    if not (owner_kind == author[0] and (owner_id == author[1] or owner_kind == "browser")):
        return "the session was handed off"
    if float(row["spent"] or 0) >= float(row["budget"]):
        return "the session has spent its budget"
    return None


def claim_queued(
    conn: Connection[Any], session_id: str, nxt: dict[str, Any]
) -> tuple[Outcome, Logged | None]:
    """``finishActivity.ts`` ``claimQueued`` in the caller's transaction: the row claimed for the
    queue's head, with its ``started`` events; ``busy`` while another turn runs; ``dropped``,
    with an ``error`` event that says why, when the session cannot run it."""
    row = conn.execute(
        (
            "SELECT status, durable_turn_id::text AS durable_turn_id, owner_kind, owner_id,"
            " archived_at IS NOT NULL AS archived, runtime,"
            f" {POOL_COST} AS spent, {POOL_BUDGET} AS budget"
            " FROM ai_sessions WHERE id = %s FOR UPDATE"
        ).encode(),
        (session_id,),
    ).fetchone()
    if row is None:
        return "dropped", None
    if row["durable_turn_id"] == nxt["turn_id"]:
        return "started", None
    if row["status"] in RUNNING:
        return "busy", None
    author = (nxt["author"]["kind"], nxt["author"]["id"])
    why = _why_not(row, author)
    if why is not None:
        error = {
            "v": PROTOCOL_VERSION,
            "type": "error",
            "sessionId": session_id,
            "code": QUEUED_DROPPED,
            "message": f"A queued message was not sent: {why}.",
        }
        return "dropped", _append(conn, session_id, [dumps(error)])
    tiers = nxt.get("tiers")
    conn.execute(
        "UPDATE ai_sessions SET status = 'running', turn_tiers = %s, durable_turn_id = %s,"
        " turn_flow = %s, updated_at = now(), turn_started_at = now(),"
        " interrupt_requested = false WHERE id = %s",
        (
            None
            if tiers is None
            else Jsonb({"by": {"kind": author[0], "id": author[1]}, "tiers": list(tiers)}),
            nxt["turn_id"],
            nxt["bare"],
            session_id,
        ),
    )
    return "started", _append(conn, session_id, list(nxt["started"]))


def finish(
    conn: Connection[Any],
    session_id: str,
    turn_id: str,
    tail: list[dict[str, Any]],
    nxt: dict[str, Any] | None,
) -> tuple[Logged | None, tuple[Outcome, Logged | None] | None]:
    """``finishActivity.ts``'s end: while the row runs ``turn_id``, the turn's closing events
    worded with the lineage's spend and budget, the status its last event names, then the
    queue's head claimed, in one transaction. None for an end already written, a turn the row no
    longer runs, or a session that is gone."""
    with _write(conn):
        row = conn.execute(
            (
                f"SELECT ai_sessions.turns, {POOL_BUDGET} AS budget, {POOL_COST} AS spent"
                " FROM ai_sessions WHERE id = %s AND status = ANY(%s) AND durable_turn_id = %s"
                " FOR UPDATE"
            ).encode(),
            (session_id, RUNNING, turn_id),
        ).fetchone()
        ended = None
        if row is not None:
            worded = word_tail(
                tail, float(row["spent"] or 0), float(row["budget"]), int(row["turns"])
            )
            conn.execute(
                "UPDATE ai_sessions SET status = %s, durable_turn_id = NULL,"
                " interrupt_requested = false, updated_at = now() WHERE id = %s",
                (worded[-1]["status"], session_id),
            )
            ended = _append(conn, session_id, [dumps(e) for e in worded])
        started = None if nxt is None else claim_queued(conn, session_id, nxt)
        return ended, started


def begin(
    conn: Connection[Any], session_id: str, nxt: dict[str, Any]
) -> tuple[Outcome, Logged | None]:
    """``session.begin_turn``: the queue's head claimed while no turn ran (#6386)."""
    with _write(conn):
        return claim_queued(conn, session_id, nxt)


def queue_changed(conn: Connection[Any], session_id: str, text: str) -> Logged | None:
    """``session.queue_changed``: the queue after a change, as a ``session.queue`` event, under
    the row's lock; None for a session that is gone."""
    with _write(conn):
        row = conn.execute(
            "SELECT 1 FROM ai_sessions WHERE id = %s FOR UPDATE", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return _append(conn, session_id, [text])


def last_queue(conn: Connection[Any], session_id: str) -> str | None:
    """``durable.ts`` ``queue``: the log's last ``session.queue`` event, as its JSON text."""
    row = conn.execute(
        "SELECT event FROM ai_session_events"
        " WHERE session_id = %s AND event::jsonb->>'type' = 'session.queue'"
        " ORDER BY seq DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return None if row is None else str(row["event"])


def stale(conn: Connection[Any], after: str, grace_ms: int, limit: int) -> list[dict[str, Any]]:
    """``durableSweep.ts``'s page: rows holding a turn with no write for ``grace_ms``, by id
    after ``after``."""
    rows = conn.execute(
        "SELECT id::text AS id, event_seq::text AS event_seq FROM ai_sessions"
        " WHERE status = ANY(%s) AND id::text > %s"
        " AND updated_at < now() - make_interval(secs => %s)"
        " ORDER BY id::text LIMIT %s",
        (RUNNING, after, grace_ms / 1000, limit),
    ).fetchall()
    return list(rows)


def end_lost(
    conn: Connection[Any], session_id: str, event_seq: int, tail: list[str]
) -> Logged | None:
    """``durableSweep.ts`` ``endLostTurn``: the row set ``idle`` with ``tail`` while it still
    holds a turn and its ``event_seq`` is the one the sweep read; None otherwise."""
    with _write(conn):
        row = conn.execute(
            "UPDATE ai_sessions SET status = 'idle', durable_turn_id = NULL, updated_at = now()"
            " WHERE id = %s AND status = ANY(%s) AND event_seq = %s RETURNING id",
            (session_id, RUNNING, event_seq),
        ).fetchone()
        if row is None:
            return None
        return _append(conn, session_id, tail)
