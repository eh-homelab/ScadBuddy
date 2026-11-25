// Package escalations is a flow run's calls at the starting session's hop:
// backend/scadbuddy/agent_data/escalations.py and api/crud_agent_escalations.py, statement for
// statement (area escalations). Every write of an answer or a delivery names the parent session
// again in its WHERE, so a caller that names another session moves nothing.
package escalations

import (
	"context"
	"errors"
	"net/http"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/audit"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/gate"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

const (
	idsMax       = 1000
	graceMaxS    = 86_400
	claimLeaseS  = 300
	waitingLimit = 200
	skipReason   = "the starting session cannot be asked"
)

// Escalations is the area's routes.
type Escalations struct{ Pool *pgxpool.Pool }

// Routes are the area's routes.
func (e *Escalations) Routes() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/escalations/entry", Handle: e.entry},
		{Method: "POST", Path: "/escalations/pass-up", Handle: e.passUp},
		{Method: "POST", Path: "/escalations/claim-deny", Handle: e.claimDeny},
		{Method: "POST", Path: "/escalations/release-deny", Handle: e.releaseDeny},
		{Method: "POST", Path: "/escalations/move-due", Handle: e.moveDue},
		{Method: "POST", Path: "/escalations/waiting", Handle: e.waiting},
		{Method: "POST", Path: "/escalations/claim", Handle: e.claim},
		{Method: "POST", Path: "/escalations/unclaim", Handle: e.unclaim},
		{Method: "POST", Path: "/escalations/skip", Handle: e.skip},
		{Method: "POST", Path: "/escalations/deliveries/page", Handle: e.deliveriesPage},
		{Method: "POST", Path: "/escalations/deliveries/sent", Handle: e.deliveriesSent},
		{Method: "POST", Path: "/escalations/deliveries/forget", Handle: e.deliveriesForget},
		{Method: "GET", Path: "/escalations/run-entries", Handle: e.runEntries},
	}
}

// step is one step of an entry's way up (hops), as the agent's step writes it.
func step(action, actor string, reason *string) string {
	return jsonv.Dumps([]any{gate.Hop("parent", action, actor, reason)})
}

type done struct {
	Done bool `json:"done"`
}

func (e *Escalations) entry(r *http.Request, _ crud.Params) (any, error) {
	rid, err := crud.QueryText(r, "request_id", gate.RequestIDMax)
	if err != nil {
		return nil, err
	}
	type entry struct {
		SessionID       string  `json:"session_id"`
		Hop             *string `json:"hop"`
		Due             bool    `json:"due"`
		Claimed         bool    `json:"claimed"`
		ParentSession   *string `json:"parent_session"`
		ParentPrincipal *string `json:"parent_principal"`
	}
	rows, err := e.Pool.Query(r.Context(),
		`SELECT session_id::text AS session_id, hop, coalesce(hop_until <= now(), false) AS due,
		 coalesce(hop_until = 'infinity', false) AS claimed,
		 parent_session::text AS parent_session, parent_principal
		 FROM ai_pending_input WHERE request_id = $1 AND flow_run_id IS NOT NULL`, rid)
	if err != nil {
		return nil, err
	}
	found, err := pgx.CollectExactlyOneRow(rows, pgx.RowToAddrOfStructByPos[entry])
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	return struct {
		Entry *entry `json:"entry"`
	}{found}, nil
}

func (e *Escalations) passUp(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "request_id", "session", "reason")
	if err != nil {
		return nil, err
	}
	rid := b.Text("request_id", 1, gate.RequestIDMax)
	session := b.Text("session", 0, gate.NameMax)
	reason := b.OptText("reason", 0, gate.TextMax)
	if err := b.Err(); err != nil {
		return nil, err
	}
	moved := false
	if audit.IsUUID(session) {
		tag, err := e.Pool.Exec(r.Context(),
			`UPDATE ai_pending_input SET hop = 'person', hop_until = NULL, hops = hops || $1::text::jsonb
			 WHERE request_id = $2 AND hop = 'parent' AND parent_session = $3::text::uuid
			 AND hop_until > now() AND hop_until <> 'infinity'`,
			step("pass_up", "session:"+session, reason), rid, session)
		if err != nil {
			return nil, err
		}
		moved = tag.RowsAffected() > 0
	}
	return struct {
		Moved bool `json:"moved"`
	}{moved}, nil
}

func (e *Escalations) claimDeny(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "request_id", "session")
	if err != nil {
		return nil, err
	}
	rid := b.Text("request_id", 1, gate.RequestIDMax)
	session := b.Text("session", 0, gate.NameMax)
	if err := b.Err(); err != nil {
		return nil, err
	}
	var held *jsonv.Time
	if audit.IsUUID(session) {
		err := pgx.BeginFunc(r.Context(), e.Pool, func(tx pgx.Tx) error {
			var until jsonv.Time
			err := tx.QueryRow(r.Context(),
				`UPDATE ai_pending_input p SET hop_until = 'infinity', hop_claimed_at = now()
				 FROM (SELECT request_id, hop_until FROM ai_pending_input WHERE request_id = $1
				 FOR UPDATE) old
				 WHERE p.request_id = old.request_id AND p.hop = 'parent'
				 AND p.parent_session = $2::text::uuid
				 AND p.hop_until > now() AND p.hop_until <> 'infinity'
				 RETURNING old.hop_until`, rid, session).Scan(&until)
			if errors.Is(err, pgx.ErrNoRows) {
				return nil
			}
			if err == nil {
				held = &until
			}
			return err
		})
		if err != nil {
			return nil, err
		}
	}
	return struct {
		HopUntil *jsonv.Time `json:"hop_until"`
	}{held}, nil
}

func (e *Escalations) releaseDeny(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "request_id", "session", "hop_until")
	if err != nil {
		return nil, err
	}
	rid := b.Text("request_id", 1, gate.RequestIDMax)
	session := b.Text("session", 0, gate.NameMax)
	until := b.Datetime("hop_until", false)
	if err := b.Err(); err != nil {
		return nil, err
	}
	released := false
	if audit.IsUUID(session) {
		tag, err := e.Pool.Exec(r.Context(),
			`UPDATE ai_pending_input SET hop_until = LEAST($1::text::timestamptz, expires_at),
			 hop_claimed_at = NULL
			 WHERE request_id = $2 AND hop = 'parent' AND parent_session = $3::text::uuid
			 AND hop_until = 'infinity'`, *until, rid, session)
		if err != nil {
			return nil, err
		}
		released = tag.RowsAffected() > 0
	}
	return done{released}, nil
}

func (e *Escalations) moveDue(r *http.Request, _ crud.Params) (any, error) {
	tag, err := e.Pool.Exec(r.Context(),
		`UPDATE ai_pending_input SET hop = 'person', hop_until = NULL, hop_claimed_at = NULL,
		 hops = hops || $1::text::jsonb
		 WHERE hop = 'parent' AND (hop_until <= now() OR (hop_until = 'infinity'
		 AND hop_claimed_at <= now() - make_interval(secs => $2)))`,
		step("timeout", "system", nil), claimLeaseS)
	if err != nil {
		return nil, err
	}
	return struct {
		Moved int64 `json:"moved"`
	}{tag.RowsAffected()}, nil
}

type waiting struct {
	RequestID       string     `json:"request_id"`
	FlowRunID       string     `json:"flow_run_id"`
	Tool            string     `json:"tool"`
	Summary         string     `json:"summary"`
	OutsideReason   *string    `json:"outside_reason"`
	HopUntil        jsonv.Time `json:"hop_until"`
	PastGrace       bool       `json:"past_grace"`
	SessionID       string     `json:"session_id"`
	ParentSession   string     `json:"parent_session"`
	ParentPrincipal string     `json:"parent_principal"`
}

func (e *Escalations) waiting(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "grace_s")
	if err != nil {
		return nil, err
	}
	if !b.Has("grace_s") {
		b.Text("grace_s", 0, 0)
	}
	grace := b.OptFloat("grace_s", 0, graceMaxS)
	if b.Has("grace_s") && grace == nil {
		b.Fail("grace_s", "Input should be a valid number")
	}
	if err := b.Err(); err != nil {
		return nil, err
	}
	rows, err := e.Pool.Query(r.Context(),
		`SELECT request_id, flow_run_id, tool, summary, outside_reason, hop_until,
		 created_at < now() - make_interval(secs => $1) AS past_grace,
		 session_id::text AS session_id, parent_session::text AS parent_session,
		 parent_principal FROM (
		 SELECT *, row_number() OVER (PARTITION BY parent_session ORDER BY created_at) AS nth
		 FROM ai_pending_input WHERE hop = 'parent' AND hop_until > now()
		 AND hop_until <> 'infinity' AND delivered_at IS NULL) waiting
		 ORDER BY nth, created_at LIMIT $2`, *grace, waitingLimit)
	if err != nil {
		return nil, err
	}
	calls, err := pgx.CollectRows(rows, pgx.RowToStructByPos[waiting])
	if err != nil {
		return nil, err
	}
	if calls == nil {
		calls = []waiting{}
	}
	return struct {
		Calls []waiting `json:"calls"`
	}{calls}, nil
}

// ids is the list name of request ids, [] when absent.
func ids(b *crud.Body, name string, required bool) []string {
	if !b.Has(name) && !required {
		return []string{}
	}
	return b.TextItems(name, b.List(name, false, idsMax), 1, gate.RequestIDMax)
}

type calls struct {
	session    string
	child, own []string
}

func readCalls(r *http.Request) (*calls, error) {
	b, err := crud.ReadBody(r, "session", "child", "own")
	if err != nil {
		return nil, err
	}
	c := &calls{b.Text("session", 0, gate.NameMax), ids(b, "child", false), ids(b, "own", false)}
	return c, b.Err()
}

func (e *Escalations) claim(r *http.Request, _ crud.Params) (any, error) {
	c, err := readCalls(r)
	if err != nil {
		return nil, err
	}
	won := []string{}
	if audit.IsUUID(c.session) {
		err := pgx.BeginFunc(r.Context(), e.Pool, func(tx pgx.Tx) error {
			ctx := r.Context()
			if len(c.child) > 0 {
				rows, err := tx.Query(ctx,
					`UPDATE ai_pending_input SET delivered_at = now() WHERE request_id IN (
					 SELECT request_id FROM ai_pending_input WHERE request_id = ANY($1)
					 AND delivered_at IS NULL AND hop = 'parent' AND parent_session = $2::text::uuid
					 FOR UPDATE SKIP LOCKED) RETURNING request_id`, c.child, c.session)
				if err != nil {
					return err
				}
				claimed, err := pgx.CollectRows(rows, pgx.RowTo[string])
				if err != nil {
					return err
				}
				won = append(won, claimed...)
			}
			for _, rid := range c.own {
				var got string
				err := tx.QueryRow(ctx,
					`INSERT INTO ai_escalation_deliveries (request_id, session_id)
					 VALUES ($1, $2::text::uuid) ON CONFLICT (request_id) DO NOTHING
					 RETURNING request_id`, rid, c.session).Scan(&got)
				if errors.Is(err, pgx.ErrNoRows) {
					continue
				}
				if err != nil {
					return err
				}
				won = append(won, got)
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	return struct {
		Claimed []string `json:"claimed"`
	}{won}, nil
}

func (e *Escalations) unclaim(r *http.Request, _ crud.Params) (any, error) {
	c, err := readCalls(r)
	if err != nil {
		return nil, err
	}
	if !audit.IsUUID(c.session) {
		return done{true}, nil
	}
	err = pgx.BeginFunc(r.Context(), e.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		if len(c.child) > 0 {
			if _, err := tx.Exec(ctx,
				`UPDATE ai_pending_input SET delivered_at = NULL WHERE request_id = ANY($1)
				 AND hop = 'parent' AND parent_session = $2::text::uuid`, c.child, c.session); err != nil {
				return err
			}
		}
		if len(c.own) > 0 {
			_, err := tx.Exec(ctx,
				`DELETE FROM ai_escalation_deliveries WHERE request_id = ANY($1)
				 AND session_id = $2::text::uuid`, c.own, c.session)
			return err
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return done{true}, nil
}

func (e *Escalations) skip(r *http.Request, _ crud.Params) (any, error) {
	c, err := readCalls(r)
	if err != nil {
		return nil, err
	}
	if !audit.IsUUID(c.session) {
		return done{true}, nil
	}
	err = pgx.BeginFunc(r.Context(), e.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		if len(c.child) > 0 {
			reason := skipReason
			if _, err := tx.Exec(ctx,
				`UPDATE ai_pending_input SET hop = 'person', hop_until = NULL,
				 hops = hops || $1::text::jsonb
				 WHERE request_id = ANY($2) AND hop = 'parent' AND parent_session = $3::text::uuid
				 AND hop_until <> 'infinity'`,
				step("pass_up", "system", &reason), c.child, c.session); err != nil {
				return err
			}
		}
		for _, rid := range c.own {
			if _, err := tx.Exec(ctx,
				`INSERT INTO ai_escalation_deliveries (request_id, session_id)
				 VALUES ($1, $2::text::uuid) ON CONFLICT (request_id) DO NOTHING`, rid, c.session); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return done{true}, nil
}

type idList struct {
	IDs []string `json:"ids"`
}

func (e *Escalations) strings(ctx context.Context, query string, args ...any) (idList, error) {
	rows, err := e.Pool.Query(ctx, query, args...)
	if err != nil {
		return idList{}, err
	}
	found, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if found == nil {
		found = []string{}
	}
	return idList{found}, err
}

func (e *Escalations) deliveriesPage(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "after", "limit")
	if err != nil {
		return nil, err
	}
	after := b.Text("after", 0, gate.RequestIDMax)
	limit := b.IntIn("limit", 1, idsMax)
	if err := b.Err(); err != nil {
		return nil, err
	}
	return e.strings(r.Context(), `SELECT request_id FROM ai_escalation_deliveries
		WHERE request_id > $1 ORDER BY request_id LIMIT $2`, after, limit)
}

func readIDs(r *http.Request) ([]string, error) {
	b, err := crud.ReadBody(r, "ids")
	if err != nil {
		return nil, err
	}
	found := ids(b, "ids", true)
	return found, b.Err()
}

func (e *Escalations) deliveriesSent(r *http.Request, _ crud.Params) (any, error) {
	found, err := readIDs(r)
	if err != nil {
		return nil, err
	}
	return e.strings(r.Context(),
		`SELECT request_id FROM ai_escalation_deliveries WHERE request_id = ANY($1)`, found)
}

func (e *Escalations) deliveriesForget(r *http.Request, _ crud.Params) (any, error) {
	found, err := readIDs(r)
	if err != nil {
		return nil, err
	}
	if _, err := e.Pool.Exec(r.Context(),
		`DELETE FROM ai_escalation_deliveries WHERE request_id = ANY($1)`, found); err != nil {
		return nil, err
	}
	return done{true}, nil
}

type runEntry struct {
	RequestID     string     `json:"request_id"`
	Kind          string     `json:"kind"`
	SessionID     string     `json:"session_id"`
	Tool          string     `json:"tool"`
	Summary       string     `json:"summary"`
	InputHash     *string    `json:"input_hash"`
	Hop           *string    `json:"hop"`
	HopUntil      *string    `json:"hop_until"`
	OutsideReason *string    `json:"outside_reason"`
	Hops          any        `json:"hops"`
	CreatedAt     jsonv.Time `json:"created_at"`
	ExpiresAt     jsonv.Time `json:"expires_at"`
	Expiring      bool       `json:"expiring"`
}

func (e *Escalations) runEntries(r *http.Request, _ crud.Params) (any, error) {
	flow, err := crud.QueryText(r, "flow_run_id", gate.NameMax)
	if err != nil {
		return nil, err
	}
	rows, err := e.Pool.Query(r.Context(),
		`SELECT request_id, kind, session_id::text AS session_id, tool, summary, input_hash, hop,
		 to_json(hop_until) #>> '{}' AS hop_until, outside_reason, hops::text, created_at,
		 expires_at, (expires_at <= now()) AS expiring
		 FROM ai_pending_input WHERE flow_run_id = $1 ORDER BY created_at, request_id`, flow)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []runEntry{}
	for rows.Next() {
		var e runEntry
		var hops *string
		if err := rows.Scan(&e.RequestID, &e.Kind, &e.SessionID, &e.Tool, &e.Summary,
			&e.InputHash, &e.Hop, &e.HopUntil, &e.OutsideReason, &hops, &e.CreatedAt,
			&e.ExpiresAt, &e.Expiring); err != nil {
			return nil, err
		}
		if hops != nil {
			e.Hops = jsonv.MustParse(*hops)
		}
		out = append(out, e)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return struct {
		Entries []runEntry `json:"entries"`
	}{out}, nil
}
