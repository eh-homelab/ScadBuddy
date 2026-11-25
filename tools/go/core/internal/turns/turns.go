// Package turns is a durable turn's writes of its session's row (area session-turns):
// backend/scadbuddy/api/crud_agent_turns.py and agent_data/turns.py, statement for statement.
// Each write is one transaction with its events, its fence checked in it (#5299 D2), and its
// lock waits bounded: a write that waits past lockTimeout for its row's lock is refused 503
// row_busy, one that waits past poolWait for a connection 503 pool_busy, never committed late.
package turns

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/events"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

const (
	// nameMax is the longest owner id (crud_agent_sessions.py NAME_MAX).
	nameMax = 512
	// attemptMax is a placement's attempt; markKeyMax a mark's key (the turn id and a request
	// id: REQUEST_ID_MAX 500 in gate/crudStore.ts).
	attemptMax = 500
	markKeyMax = 1100
	tiersMax   = 16
	tierMax    = 64
	// tailMax is a turn's closing events and a queued turn's start: a handful each.
	tailMax       = 10
	eventCharsMax = 16 * 1024 * 1024
	bodyBytesMax  = 32 * 1024 * 1024
	// usdMax and turnsMax bound a spend's delta.
	usdMax   = 1_000_000.0
	turnsMax = 1_000_000
	// graceMax is the longest grace asked for, in milliseconds: a day.
	graceMax = 24 * 3600 * 1000
	staleMax = 1000
	// spendKeyMax is a spend's key: a token and a sequence number, or the attempt, turn id and end.
	spendKeyMax = attemptMax + 64
	// spendKeyTTL is how long a spend's key is kept, in seconds: far past a placement's resend.
	spendKeyTTL = 3600
	// queuedDropped is the code of the error event that says a queued message could not start.
	queuedDropped   = "queued_message_dropped"
	protocolVersion = 1
)

var (
	// lockTimeout bounds a write's lock waits (turns.LOCK_TIMEOUT_MS). With poolWait it stays
	// under the agent's request (sessions/turnRows.ts ROW_WRITE_TIMEOUT_MS, 30 s), so a write
	// never commits after the agent gave up on it. Change them together.
	lockTimeout = 10 * time.Second
	poolWait    = 10 * time.Second
)

// running are a durable row's statuses while it holds a turn (durableSweep.ts TURN_STATUSES).
var running = []string{"running", "waiting_approval", "waiting_input"}

var statuses = []string{"running", "waiting_input", "waiting_approval", "idle", "done", "failed"}

// The budget a session spends from, and what every session spending from it has spent: a
// fork spends from its lineage's root (#823; turnShared.ts POOL_BUDGET, POOL_COST).
const (
	root       = `coalesce(ai_sessions.budget_root_id, ai_sessions.id)`
	poolBudget = `coalesce((SELECT r.budget_usd FROM ai_sessions r WHERE r.id = ` + root +
		`), ai_sessions.budget_usd)`
	poolCost = `(SELECT sum(m.cost_usd) FROM ai_sessions m WHERE coalesce(m.budget_root_id, m.id) = ` +
		root + `)`
)

// Turns is the area's routes.
type Turns struct{ Pool *pgxpool.Pool }

// Reads are the area's GET routes: the read each running turn polls, the queue's last event
// and the running sweep's page.
func (t *Turns) Reads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/sessions/{session_id}/turn", Handle: t.state},
		{Method: "GET", Path: "/sessions/{session_id}/turn/queue", Handle: t.lastQueue},
		{Method: "GET", Path: "/turns/stale", Handle: t.stale},
	}
}

// Writes are the area's POST routes.
func (t *Turns) Writes() []crud.Route {
	routes := []crud.Route{
		{Path: "/send-claim", Handle: t.sendClaim},
		{Path: "/queueable", Handle: t.queueable},
		{Path: "/give-back", Handle: t.giveBack},
		{Path: "/interrupt", Handle: t.interrupt},
		{Path: "/placement", Handle: t.placement},
		{Path: "/spend", Handle: t.spend},
		{Path: "/finish", Handle: t.finish},
		{Path: "/begin", Handle: t.begin},
		{Path: "/queue", Handle: t.queueChanged},
		{Path: "/end-lost", Handle: t.endLost},
	}
	for i := range routes {
		routes[i].Method = "POST"
		routes[i].Path = "/sessions/{session_id}/turn" + routes[i].Path
		routes[i].MaxBody = bodyBytesMax
	}
	return routes
}

// conn is a pooled connection, waited for at most poolWait.
func (t *Turns) conn(ctx context.Context) (*pgxpool.Conn, error) {
	wait, cancel := context.WithTimeout(ctx, poolWait)
	defer cancel()
	c, err := t.Pool.Acquire(wait)
	if err != nil && ctx.Err() == nil && errors.Is(wait.Err(), context.DeadlineExceeded) {
		return nil, httpx.WithReason(http.StatusServiceUnavailable,
			"no database connection was free; send the write again", "pool_busy")
	}
	return c, err
}

// read runs fn on a pooled connection.
func (t *Turns) read(ctx context.Context, fn func(*pgxpool.Conn) error) error {
	c, err := t.conn(ctx)
	if err != nil {
		return err
	}
	defer c.Release()
	return rowBusy(fn(c))
}

// write runs fn in one transaction, its lock waits bounded by lockTimeout.
func (t *Turns) write(ctx context.Context, fn func(pgx.Tx) error) error {
	c, err := t.conn(ctx)
	if err != nil {
		return err
	}
	defer c.Release()
	return rowBusy(pgx.BeginFunc(ctx, c, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, fmt.Sprintf("SET LOCAL lock_timeout = %d",
			lockTimeout.Milliseconds())); err != nil {
			return err
		}
		return fn(tx)
	}))
}

func rowBusy(err error) error {
	var pg *pgconn.PgError
	if errors.As(err, &pg) && pg.Code == "55P03" {
		return httpx.WithReason(http.StatusServiceUnavailable,
			"the session's row stayed locked; send the write again", "row_busy")
	}
	return err
}

// logged is events appended in a transaction: their JSON texts as logged, and their seqs.
type logged struct {
	Events []string `json:"events"`
	Seqs   []int64  `json:"seqs"`
}

func appendTo(ctx context.Context, tx pgx.Tx, session string, texts []string) (*logged, error) {
	seqs, err := events.AppendIn(ctx, tx, session, texts)
	if err != nil {
		return nil, err
	}
	return &logged{texts, seqs}, nil
}

func statusEvent(session, status string) string {
	return dumps(newObject("v", protocolVersion, "type", "session.status", "sessionId", session,
		"status", status))
}

// tiersJSON is a turn's tiers as turn_tiers holds them, nil for none.
func tiersJSON(a author, tiers []string, given bool) (*string, error) {
	if !given {
		return nil, nil
	}
	b, err := json.Marshal(map[string]any{
		"by": map[string]string{"kind": a.kind, "id": a.id}, "tiers": tiers,
	})
	text := string(b)
	return &text, err
}

type claimed struct {
	PriorStatus string  `json:"prior_status"`
	Runtime     *string `json:"runtime"`
}

// sendClaim is durable.ts #send's claim: the row runs turn_id while author owns it and it is
// free, not done, archived or spent. The status before, and the runtime.
func (t *Turns) sendClaim(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	f, o, err := readForm(r, "author", "turn_id", "tiers", "bare")
	if err != nil {
		return nil, err
	}
	who, turn := o.author("author"), o.turnID("turn_id")
	tiers, given := o.tiers("tiers")
	bare := o.boolean("bare")
	if err := f.err(); err != nil {
		return nil, err
	}
	tiersText, err := tiersJSON(who, tiers, given)
	if err != nil {
		return nil, err
	}
	var out *claimed
	err = t.write(r.Context(), func(tx pgx.Tx) error {
		var row claimed
		err := tx.QueryRow(r.Context(),
			`WITH prior AS (SELECT status AS prior_status FROM ai_sessions WHERE id = $1::text::uuid
			 FOR UPDATE)
			 UPDATE ai_sessions SET status = 'running', turn_tiers = $2::jsonb,
			 durable_turn_id = $3::text::uuid, turn_flow = $4, updated_at = now(),
			 turn_started_at = now(), interrupt_requested = false,
			 runtime = coalesce(runtime, 'unified')
			 FROM prior
			 WHERE id = $1::text::uuid AND owner_kind = $5
			 AND (owner_id = $6 OR owner_kind = 'browser')
			 AND status NOT IN ('running', 'waiting_approval', 'waiting_input', 'done')
			 AND archived_at IS NULL AND `+poolCost+` < `+poolBudget+`
			 RETURNING prior.prior_status, ai_sessions.runtime`,
			session, tiersText, turn, bare, who.kind, who.id).Scan(&row.PriorStatus, &row.Runtime)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		out = &row
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Claimed *claimed `json:"claimed"`
	}{out}, nil
}

// queueable is durable.ts #queue's read: the row's event_seq while it runs a turn author may
// queue behind, else null.
func (t *Turns) queueable(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	f, o, err := readForm(r, "author")
	if err != nil {
		return nil, err
	}
	who := o.author("author")
	if err := f.err(); err != nil {
		return nil, err
	}
	var seq *int64
	err = t.read(r.Context(), func(c *pgxpool.Conn) error {
		var s int64
		err := c.QueryRow(r.Context(),
			`SELECT event_seq AS seq FROM ai_sessions
			 WHERE id = $1::text::uuid AND owner_kind = $2 AND (owner_id = $3 OR owner_kind = 'browser')
			 AND status IN ('running', 'waiting_approval', 'waiting_input')
			 AND runtime = 'unified'
			 AND archived_at IS NULL AND `+poolCost+` < `+poolBudget,
			session, who.kind, who.id).Scan(&s)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		seq = &s
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Seq *int64 `json:"seq"`
	}{seq}, nil
}

type state struct {
	Status             string  `json:"status"`
	DurableTurnID      *string `json:"durable_turn_id"`
	TurnAttempt        *string `json:"turn_attempt"`
	InterruptRequested bool    `json:"interrupt_requested"`
	OwnerKind          string  `json:"owner_kind"`
	Runtime            *string `json:"runtime"`
	EventSeq           string  `json:"event_seq"`
	CostUSD            float64 `json:"cost_usd"`
	// Settled is no write for grace_ms.
	Settled bool `json:"settled"`
}

// state is the row's turn as the turn's readers read it: who runs it and where, whether a Stop
// asked for its end, and whether it was written to within grace_ms.
func (t *Turns) state(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	grace, _, err := crud.QueryInt(r, "grace_ms", 0, graceMax)
	if err != nil {
		return nil, err
	}
	var found *state
	err = t.read(r.Context(), func(c *pgxpool.Conn) error {
		rows, err := c.Query(r.Context(),
			`SELECT status, durable_turn_id::text, turn_attempt, interrupt_requested, owner_kind,
			 runtime, event_seq::text, cost_usd::float8,
			 updated_at < now() - make_interval(secs => $1) AS settled
			 FROM ai_sessions WHERE id = $2::text::uuid`, float64(grace)/1000, session)
		if err != nil {
			return err
		}
		found, err = pgx.CollectExactlyOneRow(rows, pgx.RowToAddrOfStructByPos[state])
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Turn *state `json:"turn"`
	}{found}, nil
}

type maybeLogged struct {
	Logged *logged `json:"logged"`
}

// giveBack is durable.ts #giveBack: a refused send's claim of turn_id given back to prior, with
// its session.status; null when the row no longer runs that turn. With no turn_id (an agent pod
// older than the fence, during a roll) the row is given back while it runs any turn.
func (t *Turns) giveBack(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	f, o, err := readForm(r, "prior", "turn_id")
	if err != nil {
		return nil, err
	}
	prior := o.literal("prior", statuses...)
	var turn *string
	if raw := o.optional("turn_id"); raw != nil {
		id := f.turnID([]string{"body", "turn_id"}, raw)
		turn = &id
	}
	if err := f.err(); err != nil {
		return nil, err
	}
	var out *logged
	err = t.write(r.Context(), func(tx pgx.Tx) error {
		tag, err := tx.Exec(r.Context(),
			`UPDATE ai_sessions SET status = $1, durable_turn_id = NULL, updated_at = now()
			 WHERE id = $2::text::uuid AND status = 'running'
			 AND ($3::text::uuid IS NULL OR durable_turn_id = $3::text::uuid)`,
			prior, session, turn)
		if err != nil || tag.RowsAffected() == 0 {
			return err
		}
		out, err = appendTo(r.Context(), tx, session, []string{statusEvent(session, prior)})
		return err
	})
	if err != nil {
		return nil, err
	}
	return maybeLogged{out}, nil
}

// interrupt is durable.ts interrupt's flag, while a turn holds the row.
func (t *Turns) interrupt(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	var changed bool
	err = t.write(r.Context(), func(tx pgx.Tx) error {
		tag, err := tx.Exec(r.Context(),
			`UPDATE ai_sessions SET interrupt_requested = true
			 WHERE id = $1::text::uuid AND durable_turn_id IS NOT NULL`, session)
		changed = tag.RowsAffected() > 0
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Changed bool `json:"changed"`
	}{changed}, nil
}

// placement is turnHost.ts #claim, the only writer of turn_attempt: the placement takes the
// turn while the row runs it, and the transcript's mark is set once per mark_key.
func (t *Turns) placement(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	f, o, err := readForm(r, "turn_id", "attempt", "mark_key")
	if err != nil {
		return nil, err
	}
	turn, attempt := o.turnID("turn_id"), o.text("attempt", 1, attemptMax)
	key := o.text("mark_key", 1, markKeyMax)
	if err := f.err(); err != nil {
		return nil, err
	}
	var mark *int64
	err = t.write(r.Context(), func(tx pgx.Tx) error {
		var m int64
		err := tx.QueryRow(r.Context(),
			`UPDATE ai_sessions SET turn_attempt = $1,
			 turn_mark = CASE WHEN turn_mark_turn = $2 THEN turn_mark ELSE (
			 SELECT coalesce(max(e.id), 0) FROM ai_session_entries e
			 WHERE e.session_id = ai_sessions.id::text AND e.subpath = ''
			 ) END,
			 turn_mark_turn = $2
			 WHERE id = $3::text::uuid AND durable_turn_id = $4::text::uuid
			 AND status IN ('running', 'waiting_approval', 'waiting_input')
			 RETURNING turn_mark`,
			attempt, key, session, turn).Scan(&m)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		mark = &m
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Mark *int64 `json:"mark"`
	}{mark}, nil
}

// spend is turnHost.ts Spend.flush and #write: what a placement spent, added whether or not it
// still holds the row, once per key (a resend of a write whose answer was lost adds nothing);
// false for a session that is gone.
func (t *Turns) spend(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	f, o, err := readForm(r, "key", "cost_usd", "unpriced_usd", "turns")
	if err != nil {
		return nil, err
	}
	key := o.text("key", 1, spendKeyMax)
	cost, unpriced := o.number("cost_usd", -usdMax, usdMax), o.number("unpriced_usd", -usdMax, usdMax)
	turns := o.integer("turns", -turnsMax, turnsMax)
	if err := f.err(); err != nil {
		return nil, err
	}
	var written bool
	// One statement: the row locked, the session's old keys let go (never the key sent: a resend
	// of an old key still finds it, and adds nothing), the key taken, and the spend added only
	// when the key was new.
	err = t.write(r.Context(), func(tx pgx.Tx) error {
		return tx.QueryRow(r.Context(),
			`WITH s AS (SELECT id FROM ai_sessions WHERE id = $1::text::uuid FOR UPDATE),
			 old AS (DELETE FROM ai_turn_spend_keys WHERE session_id = $1::text::uuid
			 AND created_at < now() - make_interval(secs => $2) AND key <> $3),
			 k AS (INSERT INTO ai_turn_spend_keys (session_id, key) SELECT id, $3 FROM s
			 ON CONFLICT DO NOTHING RETURNING session_id),
			 u AS (UPDATE ai_sessions SET cost_usd = cost_usd + $4::float8,
			 turns = turns + $5, unpriced_cost_usd = unpriced_cost_usd + $6::float8,
			 updated_at = now() WHERE id IN (SELECT session_id FROM k) RETURNING id)
			 SELECT EXISTS (SELECT 1 FROM s) AS found, (SELECT count(*) FROM u) AS added`,
			session, float64(spendKeyTTL), key, cost, turns, unpriced).Scan(&written, new(int64))
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Written bool `json:"written"`
	}{written}, nil
}

type begun struct {
	Outcome string  `json:"outcome"`
	Logged  *logged `json:"logged"`
}

// whyNot is finishActivity.ts whyNot: why the session cannot run a queued message now.
func whyNot(status string, archived bool, runtime *string, ownerKind, ownerID string,
	spent, budget float64, a author) string {
	switch {
	case status == "done":
		return "the session is done"
	case archived:
		return "the session is archived"
	case runtime != nil && *runtime == "durable":
		return "the session's conversation ended with the old session runtime"
	case !(ownerKind == a.kind && (ownerID == a.id || ownerKind == "browser")):
		return "the session was handed off"
	case spent >= budget:
		return "the session has spent its budget"
	}
	return ""
}

// claimQueued is finishActivity.ts claimQueued in the caller's transaction: the row claimed
// for the queue's head, with its started events; busy while another turn runs; dropped, with
// an error event that says why, when the session cannot run it.
func claimQueued(ctx context.Context, tx pgx.Tx, session string, n *next) (begun, error) {
	var status, ownerKind, ownerID string
	var turn, runtime *string
	var archived bool
	var spent *float64
	var budget float64
	err := tx.QueryRow(ctx,
		`SELECT status, durable_turn_id::text AS durable_turn_id, owner_kind, owner_id,
		 archived_at IS NOT NULL AS archived, runtime,
		 `+poolCost+` AS spent, `+poolBudget+` AS budget
		 FROM ai_sessions WHERE id = $1::text::uuid FOR UPDATE`, session).
		Scan(&status, &turn, &ownerKind, &ownerID, &archived, &runtime, &spent, &budget)
	if errors.Is(err, pgx.ErrNoRows) {
		return begun{Outcome: "dropped"}, nil
	}
	if err != nil {
		return begun{}, err
	}
	if turn != nil && *turn == n.turnID {
		return begun{Outcome: "started"}, nil
	}
	for _, s := range running {
		if status == s {
			return begun{Outcome: "busy"}, nil
		}
	}
	if spent == nil {
		spent = new(float64)
	}
	if why := whyNot(status, archived, runtime, ownerKind, ownerID, *spent, budget, n.author); why != "" {
		event := dumps(newObject("v", protocolVersion, "type", "error", "sessionId", session,
			"code", queuedDropped, "message", "A queued message was not sent: "+why+"."))
		out, err := appendTo(ctx, tx, session, []string{event})
		return begun{"dropped", out}, err
	}
	tiers, err := tiersJSON(n.author, n.tiers, n.tiered)
	if err != nil {
		return begun{}, err
	}
	if _, err := tx.Exec(ctx,
		`UPDATE ai_sessions SET status = 'running', turn_tiers = $1::jsonb,
		 durable_turn_id = $2::text::uuid, turn_flow = $3, updated_at = now(),
		 turn_started_at = now(), interrupt_requested = false WHERE id = $4::text::uuid`,
		tiers, n.turnID, n.bare, session); err != nil {
		return begun{}, err
	}
	out, err := appendTo(ctx, tx, session, n.started)
	return begun{"started", out}, err
}

// finish is finishActivity.ts's end: while the row runs turn_id, the turn's closing events
// worded with the lineage's spend and budget, the status its last event names, then the
// queue's head claimed, in one transaction. ended is null for an end already written, a turn
// the row no longer runs, or a session that is gone.
func (t *Turns) finish(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	f, o, err := readForm(r, "turn_id", "tail", "next")
	if err != nil {
		return nil, err
	}
	turn, texts := o.turnID("turn_id"), o.closing("tail")
	var head *next
	if raw := o.optional("next"); raw != nil {
		head = f.next([]string{"body", "next"}, raw)
	}
	if err := f.err(); err != nil {
		return nil, err
	}
	tail := make([]any, len(texts))
	for i, text := range texts {
		if tail[i], err = parse(text); err != nil {
			return nil, err
		}
	}
	var out struct {
		// Ended is the turn's end as written, or null when the row no longer runs it.
		Ended *logged `json:"ended"`
		// Next is the queue's head, when one was given.
		Next *begun `json:"next"`
	}
	err = t.write(r.Context(), func(tx pgx.Tx) error {
		ctx := r.Context()
		var turns int64
		var budget float64
		var spent *float64
		err := tx.QueryRow(ctx,
			`SELECT ai_sessions.turns, `+poolBudget+` AS budget, `+poolCost+` AS spent
			 FROM ai_sessions WHERE id = $1::text::uuid AND status = ANY($2::text[])
			 AND durable_turn_id = $3::text::uuid FOR UPDATE`,
			session, running, turn).Scan(&turns, &budget, &spent)
		switch {
		case errors.Is(err, pgx.ErrNoRows):
		case err != nil:
			return err
		default:
			if spent == nil {
				spent = new(float64)
			}
			worded := wordTail(tail, *spent, budget, turns)
			if _, err := tx.Exec(ctx,
				`UPDATE ai_sessions SET status = $1, durable_turn_id = NULL,
				 interrupt_requested = false, updated_at = now() WHERE id = $2::text::uuid`,
				worded[len(worded)-1].(*object).get("status"), session); err != nil {
				return err
			}
			written := make([]string, len(worded))
			for i, e := range worded {
				written[i] = dumps(e)
			}
			if out.Ended, err = appendTo(ctx, tx, session, written); err != nil {
				return err
			}
		}
		if head != nil {
			started, err := claimQueued(ctx, tx, session, head)
			out.Next = &started
			return err
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// begin is session.begin_turn: the queue's head claimed while no turn ran (#6386).
func (t *Turns) begin(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	f, o, err := readForm(r, "next")
	if err != nil {
		return nil, err
	}
	var head *next
	if raw, ok := o.get("next"); ok {
		head = f.next([]string{"body", "next"}, raw)
	}
	if err := f.err(); err != nil {
		return nil, err
	}
	var out begun
	err = t.write(r.Context(), func(tx pgx.Tx) error {
		out, err = claimQueued(r.Context(), tx, session, head)
		return err
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// queueChanged is session.queue_changed: the queue after a change, as a session.queue event,
// under the row's lock; null for a session that is gone.
func (t *Turns) queueChanged(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	f, o, err := readForm(r, "event")
	if err != nil {
		return nil, err
	}
	var text string
	if raw, ok := o.get("event"); ok {
		loc := []string{"body", "event"}
		var valid bool
		if text, valid = f.event(loc, raw); valid {
			event, _ := parse(text)
			if event.(*object).get("type") != "session.queue" {
				f.fail(loc, "Value error, the event is a session.queue")
			}
		}
	}
	if err := f.err(); err != nil {
		return nil, err
	}
	var out *logged
	err = t.write(r.Context(), func(tx pgx.Tx) error {
		var one int
		err := tx.QueryRow(r.Context(),
			`SELECT 1 FROM ai_sessions WHERE id = $1::text::uuid FOR UPDATE`, session).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		out, err = appendTo(r.Context(), tx, session, []string{text})
		return err
	})
	if err != nil {
		return nil, err
	}
	return maybeLogged{out}, nil
}

// lastQueue is durable.ts queue: the log's last session.queue event, as its JSON text.
func (t *Turns) lastQueue(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	var event *string
	err = t.read(r.Context(), func(c *pgxpool.Conn) error {
		var e string
		err := c.QueryRow(r.Context(),
			`SELECT event FROM ai_session_events
			 WHERE session_id = $1::text::uuid AND event::jsonb->>'type' = 'session.queue'
			 ORDER BY seq DESC LIMIT 1`, session).Scan(&e)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		event = &e
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Event *string `json:"event"`
	}{event}, nil
}

type staleRow struct {
	ID       string `json:"id"`
	EventSeq string `json:"event_seq"`
}

// stale is durableSweep.ts's page: rows holding a turn with no write for grace_ms, by id
// after after.
func (t *Turns) stale(r *http.Request, _ crud.Params) (any, error) {
	after := ""
	if values := r.URL.Query()["after"]; len(values) > 0 {
		after = values[len(values)-1]
		if n := len([]rune(after)); n > 36 {
			return nil, httpx.Invalid(httpx.Issue{
				Loc: []string{"query", "after"}, Msg: "String should have at most 36 characters",
			})
		}
	}
	grace, _, err := crud.QueryInt(r, "grace_ms", 0, graceMax)
	if err != nil {
		return nil, err
	}
	limit, given, err := crud.QueryInt(r, "limit", 1, staleMax)
	if err != nil {
		return nil, err
	}
	if !given {
		limit = 10
	}
	var rows []staleRow
	err = t.read(r.Context(), func(c *pgxpool.Conn) error {
		found, err := c.Query(r.Context(),
			`SELECT id::text AS id, event_seq::text AS event_seq FROM ai_sessions
			 WHERE status = ANY($1::text[]) AND id::text > $2
			 AND updated_at < now() - make_interval(secs => $3)
			 ORDER BY id::text LIMIT $4`,
			running, after, float64(grace)/1000, limit)
		if err != nil {
			return err
		}
		rows, err = pgx.CollectRows(found, pgx.RowToStructByPos[staleRow])
		return err
	})
	if err != nil {
		return nil, err
	}
	if rows == nil {
		rows = []staleRow{}
	}
	return struct {
		Rows []staleRow `json:"rows"`
	}{rows}, nil
}

// endLost is durableSweep.ts endLostTurn: the row set idle with tail while it still holds a
// turn and its event_seq is the one the sweep read; null otherwise.
func (t *Turns) endLost(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	f, o, err := readForm(r, "event_seq", "tail")
	if err != nil {
		return nil, err
	}
	seq, tail := o.integer("event_seq", 0, 1<<63-1), o.closing("tail")
	if err := f.err(); err != nil {
		return nil, err
	}
	var out *logged
	err = t.write(r.Context(), func(tx pgx.Tx) error {
		var id string
		err := tx.QueryRow(r.Context(),
			`UPDATE ai_sessions SET status = 'idle', durable_turn_id = NULL, updated_at = now()
			 WHERE id = $1::text::uuid AND status = ANY($2::text[]) AND event_seq = $3
			 RETURNING id::text`, session, running, seq).Scan(&id)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		out, err = appendTo(r.Context(), tx, session, tail)
		return err
	})
	if err != nil {
		return nil, err
	}
	return maybeLogged{out}, nil
}
