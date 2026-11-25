// Package events is a session's panel-protocol event log: backend/scadbuddy/agent_data/
// events.py and api/crud_agent_events.py, statement for statement (area session-events).
//
// Order without gaps: seq comes from ai_sessions.event_seq, bumped in the statement that
// inserts the events, so two writers of one session commit in seq order. Python still writes
// the log too (its gate, turns and workspace); the statement is the same there.
package events

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"slices"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

const (
	eventsMax     = 1000
	eventCharsMax = 16 * 1024 * 1024
	bodyBytesMax  = 32 * 1024 * 1024
	readMax       = 500
	turnIDMax     = 500
	maxSeq        = 1<<63 - 1
)

// forkEventTypes are the conversation events a fork takes; lifecycle events are the parent's.
var forkEventTypes = []string{
	"user.turn", "assistant.text.delta", "assistant.text.done", "tool.call", "tool.result",
}

// Log is the event log's routes.
type Log struct{ Pool *pgxpool.Pool }

// Reads are the area's GET routes.
func (l *Log) Reads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/sessions/{session_id}/events", Handle: l.read},
		{Method: "GET", Path: "/sessions/{session_id}/events/last-seq", Handle: l.lastSeq},
		{Method: "GET", Path: "/sessions/{session_id}/events/user-turn", Handle: l.userTurn},
		{Method: "GET", Path: "/sessions/{session_id}/events/turn", Handle: l.turn},
	}
}

// Writes are the area's POST routes.
func (l *Log) Writes() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/sessions/{session_id}/events", Handle: l.append, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/sessions/{session_id}/fork-log", Handle: l.forkLog, MaxBody: bodyBytesMax},
	}
}

type seqs struct {
	// Seqs are the events' seqs, oldest first; null when the fence refused them.
	Seqs []int64 `json:"seqs"`
}

type fence struct{ attempt, turnID string }

func (l *Log) append(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "events", "fence")
	if err != nil {
		return nil, err
	}
	texts := body.Texts("events", 1, eventsMax, eventCharsMax)
	var held *fence
	if f := body.Object("fence", "attempt", "turn_id"); f != nil {
		held = &fence{f.Text("attempt", 1, 500), f.Text("turn_id", 1, turnIDMax)}
		f.Done()
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	if err := checked(texts); err != nil {
		return nil, err
	}
	var out []int64
	err = pgx.BeginFunc(r.Context(), l.Pool, func(tx pgx.Tx) error {
		if held != nil {
			var one int
			err := tx.QueryRow(r.Context(),
				`SELECT 1 FROM ai_sessions WHERE id = $1::text::uuid AND turn_attempt = $2
				 AND durable_turn_id = $3::text::uuid FOR UPDATE`,
				session, held.attempt, held.turnID).Scan(&one)
			if errors.Is(err, pgx.ErrNoRows) {
				return nil
			}
			if err != nil {
				return err
			}
		}
		out, err = appendIn(r.Context(), tx, session, texts)
		return err
	})
	var pg *pgconn.PgError
	if errors.As(err, &pg) && pg.Code == "22P02" {
		return nil, httpx.WithReason(http.StatusUnprocessableEntity,
			"the fence's turn id is not a UUID", "turn_id")
	}
	if err != nil {
		return nil, err
	}
	return seqs{out}, nil
}

// checked is the 422 of a text that is no JSON object with a string type: the log's reads
// parse it as jsonb.
func checked(texts []string) error {
	for _, text := range texts {
		if !json.Valid([]byte(text)) {
			return &httpx.Problem{Status: http.StatusUnprocessableEntity, Detail: "an event is not JSON"}
		}
		if kind, ok := member(text, "type"); !ok || kind == nil {
			return &httpx.Problem{
				Status: http.StatusUnprocessableEntity, Detail: "an event is an object with a type",
			}
		}
	}
	return nil
}

// member is text's string member name: ok when text is an object, nil when it has none.
func member(text, name string) (value *string, ok bool) {
	var object map[string]json.RawMessage
	if json.Unmarshal([]byte(text), &object) != nil || object == nil {
		return nil, false
	}
	if json.Unmarshal(object[name], &value) != nil {
		return nil, true
	}
	return value, true
}

// appendIn appends texts in the caller's transaction; their seqs, oldest first.
func appendIn(ctx context.Context, tx pgx.Tx, session string, texts []string) ([]int64, error) {
	if len(texts) == 0 {
		return []int64{}, nil
	}
	rows, err := tx.Query(ctx,
		`WITH s AS (
		   UPDATE ai_sessions SET event_seq = event_seq + $1, updated_at = now()
		   WHERE id = $2::text::uuid RETURNING event_seq - $1 AS base)
		 INSERT INTO ai_session_events (session_id, seq, event)
		 SELECT $2::text::uuid, s.base + e.ord, e.event
		 FROM s, unnest($3::text[]) WITH ORDINALITY AS e(event, ord)
		 RETURNING seq`,
		int64(len(texts)), session, texts)
	if err != nil {
		return nil, err
	}
	out, err := pgx.CollectRows(rows, pgx.RowTo[int64])
	if err != nil {
		return nil, err
	}
	if len(out) != len(texts) {
		return nil, httpx.WithReason(http.StatusNotFound,
			fmt.Sprintf("session %s does not exist", session), "session_gone")
	}
	slices.Sort(out)
	return out, nil
}

const chain = `WITH RECURSIVE chain (id, parent, cut, bound, depth) AS (
   SELECT id, fork_parent_id, fork_event_cut, NULL::bigint, 0 FROM ai_sessions
   WHERE id = $1::text::uuid
   UNION ALL
   SELECT s.id, s.fork_parent_id, s.fork_event_cut, LEAST(c.bound, c.cut), c.depth + 1
   FROM ai_sessions s JOIN chain c ON s.id = c.parent WHERE c.cut IS NOT NULL)`

type logged struct {
	Seq int64 `json:"seq"`
	// Event is the event's JSON text as it was logged.
	Event string `json:"event"`
	// Inherited is taken through the fork chain: the agent addresses it to this session.
	Inherited bool `json:"inherited"`
}

func (l *Log) read(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	after, _, err := crud.QueryInt(r, "after", 0, maxSeq)
	if err != nil {
		return nil, err
	}
	limit, given, err := crud.QueryInt(r, "limit", 1, readMax)
	if err != nil {
		return nil, err
	}
	if !given {
		limit = readMax
	}
	rows, err := l.Pool.Query(r.Context(),
		chain+` SELECT e.seq, e.event, c.depth > 0 FROM chain c
		 JOIN ai_session_events e ON e.session_id = c.id WHERE e.seq > $2
		 AND (c.depth = 0 OR (c.depth > 0 AND e.seq <= c.bound
		 AND e.event::jsonb->>'type' = ANY($3::text[])))
		 ORDER BY e.seq LIMIT $4`,
		session, after, forkEventTypes, limit)
	if err != nil {
		return nil, err
	}
	events, err := pgx.CollectRows(rows, pgx.RowToStructByPos[logged])
	if err != nil {
		return nil, err
	}
	return struct {
		Events []logged `json:"events"`
	}{nonNil(events)}, nil
}

func (l *Log) lastSeq(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	var seq int64
	err = l.Pool.QueryRow(r.Context(),
		`SELECT event_seq FROM ai_sessions WHERE id = $1::text::uuid`, session).Scan(&seq)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	return struct {
		Seq int64 `json:"seq"`
	}{seq}, nil
}

func (l *Log) userTurn(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	turn, err := crud.QueryText(r, "turn_id", turnIDMax)
	if err != nil {
		return nil, err
	}
	var event string
	err = l.Pool.QueryRow(r.Context(),
		`SELECT event FROM ai_session_events WHERE session_id = $1::text::uuid
		 AND event::jsonb->>'type' = 'user.turn' AND event::jsonb->>'turnId' = $2
		 ORDER BY seq DESC LIMIT 1`, session, turn).Scan(&event)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	var text *string
	if err == nil {
		text, _ = member(event, "text")
	}
	return struct {
		Text *string `json:"text"`
	}{text}, nil
}

type turnEvents struct {
	// Here is whether the session still stands.
	Here bool `json:"here"`
	// After is the seq read through, for the next read; null while the turn's start is not logged.
	After  *int64   `json:"after"`
	Events []string `json:"events"`
}

func (l *Log) turn(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	turn, err := crud.QueryText(r, "turn_id", turnIDMax)
	if err != nil {
		return nil, err
	}
	after, given, err := crud.QueryInt(r, "after", 0, maxSeq)
	if err != nil {
		return nil, err
	}
	gone := turnEvents{Events: []string{}}
	if !given {
		var here bool
		var start *int64
		err := l.Pool.QueryRow(r.Context(),
			`SELECT EXISTS (SELECT 1 FROM ai_sessions WHERE id = $1::text::uuid), e.seq
			 FROM (SELECT 1) AS one
			 LEFT JOIN ai_session_events e ON e.session_id = $1::text::uuid
			 AND e.event::jsonb->>'type' = 'user.turn' AND e.event::jsonb->>'turnId' = $2
			 ORDER BY e.seq DESC NULLS LAST LIMIT 1`, session, turn).Scan(&here, &start)
		if err != nil {
			return nil, err
		}
		if !here {
			return gone, nil
		}
		if start == nil {
			return turnEvents{Here: true, Events: []string{}}, nil
		}
		after = *start
	}
	rows, err := l.Pool.Query(r.Context(),
		`SELECT EXISTS (SELECT 1 FROM ai_sessions WHERE id = $1::text::uuid), e.seq, e.event
		 FROM (SELECT 1) AS one LEFT JOIN LATERAL (
		   SELECT seq, event FROM ai_session_events WHERE session_id = $1::text::uuid
		   AND seq > $2 ORDER BY seq) e ON true`, session, after)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := turnEvents{After: &after, Events: []string{}}
	for rows.Next() {
		var seq *int64
		var event *string
		if err := rows.Scan(&out.Here, &seq, &event); err != nil {
			return nil, err
		}
		if seq != nil && event != nil {
			out.Events = append(out.Events, *event)
			after = *seq
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if !out.Here {
		return gone, nil
	}
	return out, nil
}

// forkLog is a new fork's own start, the parent's log taken through the cut, and its status,
// in one transaction under the parent's payload-key lock, shared.
func (l *Log) forkLog(r *http.Request, p crud.Params) (any, error) {
	child, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "parent_id", "cut", "started", "then")
	if err != nil {
		return nil, err
	}
	parent := body.UUID("parent_id")
	cut := body.Int("cut", 0)
	started := body.Texts("started", 1, 10, eventCharsMax)
	then := body.Texts("then", 0, 10, eventCharsMax)
	if err := body.Err(); err != nil {
		return nil, err
	}
	if err := checked(append(slices.Clone(started), then...)); err != nil {
		return nil, err
	}
	var out []int64
	err = pgx.BeginFunc(r.Context(), l.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		// payload_codec.py payload_key_lock.
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock_shared(hashtext($1))`,
			"scadbuddy:ai_payload_keys:session-"+parent); err != nil {
			return err
		}
		var one int
		err := tx.QueryRow(ctx, `SELECT 1 FROM ai_sessions
			WHERE id = $1::text::uuid AND fork_parent_id = $2::text::uuid`, child, parent).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return httpx.WithReason(http.StatusNotFound,
				fmt.Sprintf("session %s was forgotten while it was forked", parent), "parent_gone")
		}
		if err != nil {
			return err
		}
		first, err := appendIn(ctx, tx, child, started)
		if err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `UPDATE ai_sessions SET fork_event_cut = $1,
			event_seq = GREATEST(event_seq, $1) WHERE id = $2::text::uuid`, cut, child); err != nil {
			return err
		}
		last, err := appendIn(ctx, tx, child, then)
		out = append(first, last...)
		return err
	})
	if err != nil {
		return nil, err
	}
	return seqs{out}, nil
}

func nonNil[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}
