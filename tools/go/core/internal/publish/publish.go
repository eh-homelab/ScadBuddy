// Package publish is the agent's NOTIFYs and its last reads of a session's own events:
// backend/scadbuddy/api/crud_agent_publish.py (removed in #7667) and agent_data/events.py open_calls and
// reasoning_deltas, statement for statement (areas publish and session-reads).
package publish

import (
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"unicode/utf16"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

const (
	// EventsChannel is the bus's channel (core/events.py PG_CHANNEL).
	EventsChannel = "scadbuddy_events"
	// WarmEvictChannel tells every replica to evict a session's warm process.
	WarmEvictChannel = "scadbuddy_warm_evict"
	// bodyBytesMax is the event-log routes' bound (crud_agent_events.py (removed in #7667) BODY_BYTES_MAX).
	bodyBytesMax     = 32 * 1024 * 1024
	toolUseIDMax     = 500
	reasoningScanMax = 10_000
	turnIDMax        = 500
)

var (
	eventIDPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)
	uuidPattern    = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	statusPattern  = regexp.MustCompile(`^[a-z_]{1,32}$`)
	targetPattern  = regexp.MustCompile(`^(\*|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$`)
	busKinds       = []string{"session.started", "session.message", "session.waiting", "session.done", "session.owner"}
)

// Publish is the areas' routes.
type Publish struct{ Pool *pgxpool.Pool }

// Sends are area publish's routes.
func (p *Publish) Sends() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/publish/session-event", Handle: p.sessionEvent, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/publish/warm-evict", Handle: p.warmEvict, MaxBody: bodyBytesMax},
	}
}

// Reads are area session-reads' routes.
func (p *Publish) Reads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/sessions/{session_id}/open-calls", Handle: p.openCalls},
		{Method: "GET", Path: "/sessions/{session_id}/reasoning", Handle: p.reasoning},
	}
}

type sent struct {
	Sent bool `json:"sent"`
}

// sessionEvent is a bus event as busEvents.ts SessionBusEvent built it; status is left out
// when none, as model_dump(exclude_none=True) does.
type sessionEvent struct {
	ID        string  `json:"id"`
	At        string  `json:"at"`
	Kind      string  `json:"kind"`
	SessionID string  `json:"session_id"`
	Seq       int64   `json:"seq"`
	Status    *string `json:"status,omitempty"`
	Replica   string  `json:"replica"`
}

func (p *Publish) sessionEvent(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "id", "at", "kind", "session_id", "seq", "status", "replica")
	if err != nil {
		return nil, err
	}
	event := sessionEvent{
		ID:        body.Get("id").String(0, 1<<30, eventIDPattern),
		At:        body.Get("at").String(1, 40, nil),
		Kind:      body.Get("kind").Enum(busKinds...),
		SessionID: body.Get("session_id").String(0, 1<<30, uuidPattern),
		Seq:       body.Get("seq").Int(0, 1<<53, false),
		Status:    body.Get("status").OptString(0, 1<<30, statusPattern),
		Replica:   body.Get("replica").String(1, 200, nil),
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	payload, err := httpx.Marshal(event)
	if err != nil {
		return nil, err
	}
	if _, err := p.Pool.Exec(r.Context(), `SELECT pg_notify($1, $2)`, EventsChannel, ASCII(payload)); err != nil {
		return nil, err
	}
	return sent{true}, nil
}

// ASCII is compact JSON as Python's json.dumps writes it with ensure_ascii: every character
// past ASCII as a lower-case \u escape, one past the BMP as its surrogate pair.
func ASCII(compact []byte) string {
	var b strings.Builder
	for _, c := range string(compact) {
		switch {
		case c < 0x80:
			b.WriteRune(c)
		case c > 0xFFFF:
			hi, lo := utf16.EncodeRune(c)
			fmt.Fprintf(&b, `\u%04x\u%04x`, hi, lo)
		default:
			fmt.Fprintf(&b, `\u%04x`, c)
		}
	}
	return b.String()
}

func (p *Publish) warmEvict(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "target")
	if err != nil {
		return nil, err
	}
	target := body.Get("target").String(0, 1<<30, targetPattern)
	if err := body.Err(); err != nil {
		return nil, err
	}
	if _, err := p.Pool.Exec(r.Context(), `SELECT pg_notify($1, $2)`, WarmEvictChannel, target); err != nil {
		return nil, err
	}
	return sent{true}, nil
}

type call struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

const userTurn = "session_id = $1::uuid AND event::jsonb->>'type' = 'user.turn'" +
	" AND event::jsonb->>'turnId' = $2"

// openCalls is the session's own calls with a tool.call and no tool.result, oldest first.
// With turn_id, those after that turn's last user.turn (after seq 0 when it is not logged).
func (p *Publish) openCalls(r *http.Request, params crud.Params) (any, error) {
	session, err := params.UUID("session_id")
	if err != nil {
		return nil, err
	}
	var turn *string
	if _, given := r.URL.Query()["turn_id"]; given {
		t, err := crud.QueryText(r, "turn_id", turnIDMax)
		if err != nil {
			return nil, err
		}
		turn = &t
	}
	rows, err := p.Pool.Query(r.Context(), `WITH start AS (SELECT coalesce((SELECT seq FROM ai_session_events
		WHERE $2::text IS NOT NULL AND `+userTurn+` ORDER BY seq DESC LIMIT 1), 0) AS seq)
		SELECT e.event::jsonb->>'type' AS type, e.event::jsonb->>'id' AS id,
		e.event::jsonb->>'name' AS name
		FROM ai_session_events e, start WHERE e.session_id = $1::uuid
		AND e.seq > start.seq AND e.event::jsonb->>'type' IN ('tool.call', 'tool.result')
		ORDER BY e.seq`, session, turn)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	// A dict's order: a call ended and called again goes last.
	var order []string
	names := map[string]string{}
	for rows.Next() {
		var kind, id, name *string
		if err := rows.Scan(&kind, &id, &name); err != nil {
			return nil, err
		}
		key := ""
		if id != nil {
			key = *id
		}
		if *kind == "tool.call" {
			if _, held := names[key]; !held {
				order = append(order, key)
			}
			names[key] = ""
			if name != nil {
				names[key] = *name
			}
		} else if _, held := names[key]; held {
			delete(names, key)
			for i, k := range order {
				if k == key {
					order = append(order[:i], order[i+1:]...)
					break
				}
			}
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	calls := []call{}
	for _, id := range order {
		calls = append(calls, call{id, names[id]})
	}
	return struct {
		Calls []call `json:"calls"`
	}{calls}, nil
}

type deltaRow struct {
	Message *string `json:"message"`
	Delta   *string `json:"delta"`
}

type reasoning struct {
	// Logged is whether the call's tool.call is within the log's last scan events.
	Logged bool       `json:"logged"`
	Deltas []deltaRow `json:"deltas"`
}

// reasoning is the assistant.text.delta events since the turn's last user.turn before the
// call's tool.call, oldest first, within the log's last scan events.
func (p *Publish) reasoning(r *http.Request, params crud.Params) (any, error) {
	session, err := params.UUID("session_id")
	if err != nil {
		return nil, err
	}
	id, err := crud.QueryText(r, "tool_use_id", toolUseIDMax)
	if err != nil {
		return nil, err
	}
	scan, given, err := crud.QueryInt(r, "scan", 0, reasoningScanMax)
	if err != nil {
		return nil, err
	}
	if !given {
		scan = 2000
	}
	ctx := r.Context()
	var floor int64
	var at *int64
	err = p.Pool.QueryRow(ctx, `WITH top AS (SELECT coalesce(max(seq), 0) - $3::bigint AS floor
		FROM ai_session_events WHERE session_id = $1::uuid)
		SELECT top.floor, (SELECT seq FROM ai_session_events
		WHERE session_id = $1::uuid AND seq > top.floor
		AND strpos(event, $2) > 0 AND event::jsonb ->> 'type' = 'tool.call'
		AND event::jsonb ->> 'id' = $2 ORDER BY seq DESC LIMIT 1) AS seq FROM top`,
		session, id, scan).Scan(&floor, &at)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	if at == nil {
		return reasoning{Deltas: []deltaRow{}}, nil
	}
	from := floor
	err = p.Pool.QueryRow(ctx, `SELECT seq FROM ai_session_events WHERE session_id = $1::uuid
		AND seq > $2::bigint AND seq < $3::bigint AND strpos(event, '"user.turn"') > 0
		AND event::jsonb ->> 'type' = 'user.turn' ORDER BY seq DESC LIMIT 1`,
		session, floor, *at).Scan(&from)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	rows, err := p.Pool.Query(ctx, `SELECT event::jsonb ->> 'messageId' AS message,
		event::jsonb ->> 'delta' AS delta
		FROM ai_session_events WHERE session_id = $1::uuid
		AND seq < $2::bigint AND seq > $3::bigint
		AND strpos(event, '"assistant.text.delta"') > 0
		AND event::jsonb ->> 'type' = 'assistant.text.delta' ORDER BY seq`, session, *at, from)
	if err != nil {
		return nil, err
	}
	deltas, err := pgx.CollectRows(rows, pgx.RowToStructByPos[deltaRow])
	if err != nil {
		return nil, err
	}
	if deltas == nil {
		deltas = []deltaRow{}
	}
	return reasoning{Logged: true, Deltas: deltas}, nil
}
