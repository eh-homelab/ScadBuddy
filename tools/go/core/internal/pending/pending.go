// Package pending is the gate's pending input and responses beyond its writers:
// backend/scadbuddy/agent_data/pending_input.py and api/crud_agent_pending.py, statement for
// statement (area pending-input).
package pending

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/audit"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/gate"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

const (
	entriesMax = 500
	runsMax    = 100
	// OrphanReason is why the sweep ended an entry (the agent's ORPHAN_REASON).
	OrphanReason = "the session's workflow run has ended"
	idsMax       = 1000
	// TraceparentMax is the longest W3C traceparent with room for a future version's.
	TraceparentMax = 256
	ageMaxS        = 86_400
)

// Pending is the area's routes.
type Pending struct{ Pool *pgxpool.Pool }

// Routes are the area's routes.
func (p *Pending) Routes() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/pending-input/entries", Handle: p.entries},
		{Method: "POST", Path: "/pending-input/in-run", Handle: p.inRun},
		{Method: "POST", Path: "/pending-input/decided", Handle: p.decided},
		{Method: "GET", Path: "/pending-input/tab-wait", Handle: p.tabWait},
		{Method: "GET", Path: "/pending-input/recorded", Handle: p.recorded},
		{Method: "GET", Path: "/pending-input/park-trace", Handle: p.parkTrace},
		{Method: "POST", Path: "/pending-input/decision-trace", Handle: p.decisionTrace},
		{Method: "POST", Path: "/pending-input/trace", Handle: p.trace},
		{Method: "GET", Path: "/pending-input/session", Handle: p.session},
		{Method: "GET", Path: "/pending-input/sweep/runs", Handle: p.sweepRuns},
		{Method: "POST", Path: "/pending-input/sweep/checked", Handle: p.sweepChecked},
		{Method: "POST", Path: "/pending-input/orphans/cancel", Handle: p.cancel},
		{Method: "GET", Path: "/pending-input/approval", Handle: p.approval},
	}
}

// QueryPattern is the required query parameter name, matching pattern.
func QueryPattern(r *http.Request, name string, pattern *regexp.Regexp) (string, error) {
	values := r.URL.Query()[name]
	if len(values) == 0 {
		return "", httpx.Invalid(httpx.Issue{Loc: []string{"query", name}, Msg: "Field required"})
	}
	s := values[len(values)-1]
	if !pattern.MatchString(s) {
		return "", httpx.Invalid(httpx.Issue{Loc: []string{"query", name},
			Msg: fmt.Sprintf("String should match pattern '%s'", pattern)})
	}
	return s, nil
}

// QueryIntRequired is the required integer query parameter name within [least, most].
func QueryIntRequired(r *http.Request, name string, least, most int64) (int64, error) {
	v, given, err := crud.QueryInt(r, name, least, most)
	if err == nil && !given {
		err = httpx.Invalid(httpx.Issue{Loc: []string{"query", name}, Msg: "Field required"})
	}
	return v, err
}

type entry struct {
	RequestID     string     `json:"request_id"`
	Kind          string     `json:"kind"`
	SessionID     string     `json:"session_id"`
	Tool          string     `json:"tool"`
	Summary       string     `json:"summary"`
	InputHash     *string    `json:"input_hash"`
	Prompt        string     `json:"prompt"`
	RequestedBy   any        `json:"requested_by"`
	Responders    []string   `json:"responders"`
	Attention     any        `json:"attention"`
	FlowRunID     *string    `json:"flow_run_id"`
	Hop           *string    `json:"hop"`
	OutsideReason *string    `json:"outside_reason"`
	CreatedAt     jsonv.Time `json:"created_at"`
	ExpiresAt     jsonv.Time `json:"expires_at"`
	Expiring      bool       `json:"expiring"`
}

func parsed(text *string) any {
	if text == nil {
		return nil
	}
	return jsonv.MustParse(*text)
}

func (p *Pending) entries(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "session_id", "owned_by", "all_approvals", "own_answers")
	if err != nil {
		return nil, err
	}
	sid := b.OptText("session_id", 0, 1<<20)
	if sid != nil {
		b.Pattern("session_id", *sid, gate.SessionPattern)
	}
	var kind, owner *string
	if o := b.Object("owned_by", "kind", "id"); o != nil {
		k, i := o.Text("kind", 0, gate.NameMax), o.Text("id", 0, gate.NameMax)
		kind, owner = &k, &i
		o.Done()
	}
	all := b.Bool("all_approvals", crud.False)
	own := b.Bool("own_answers", crud.False)
	if err := b.Err(); err != nil {
		return nil, err
	}
	rows, err := p.Pool.Query(r.Context(),
		`SELECT p.request_id, p.kind, p.session_id::text AS session_id, p.tool, p.summary,
		 p.input_hash, p.prompt, p.requested_by::text, p.responders, p.attention::text,
		 p.flow_run_id, p.hop, p.outside_reason, p.created_at, p.expires_at,
		 (p.expires_at <= now()) AS expiring
		 FROM ai_pending_input p JOIN ai_sessions s ON s.id = p.session_id
		 WHERE ($1::text::uuid IS NULL OR p.session_id = $1::text::uuid)
		 AND ($2::boolean
		  OR ($3::boolean AND p.kind = 'approval' AND p.flow_run_id IS NULL)
		  OR (s.owner_kind = $4::text AND s.owner_id = $5::text)
		  OR (s.creator_kind = $4::text AND s.creator_id = $5::text))
		 AND ($6::boolean OR p.kind = 'approval' OR p.flow_run_id IS NOT NULL
		  OR s.owner_kind = 'browser')
		 ORDER BY p.created_at, p.request_id LIMIT $7`,
		sid, kind == nil, all, kind, owner, !own, entriesMax)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []entry{}
	for rows.Next() {
		var e entry
		var by, attention *string
		if err := rows.Scan(&e.RequestID, &e.Kind, &e.SessionID, &e.Tool, &e.Summary,
			&e.InputHash, &e.Prompt, &by, &e.Responders, &attention, &e.FlowRunID, &e.Hop,
			&e.OutsideReason, &e.CreatedAt, &e.ExpiresAt, &e.Expiring); err != nil {
			return nil, err
		}
		e.RequestedBy, e.Attention = parsed(by), parsed(attention)
		if e.Responders == nil {
			e.Responders = []string{}
		}
		out = append(out, e)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return struct {
		Entries []entry `json:"entries"`
	}{out}, nil
}

func (p *Pending) ids(r *http.Request, query string) (any, error) {
	b, err := crud.ReadBody(r, "request_ids")
	if err != nil {
		return nil, err
	}
	ids := b.TextItems("request_ids", b.List("request_ids", false, idsMax), 1, gate.RequestIDMax)
	if err := b.Err(); err != nil {
		return nil, err
	}
	found := []string{}
	if len(ids) > 0 {
		rows, err := p.Pool.Query(r.Context(), query, ids)
		if err != nil {
			return nil, err
		}
		if found, err = pgx.CollectRows(rows, pgx.RowTo[string]); err != nil {
			return nil, err
		}
		if found == nil {
			found = []string{}
		}
	}
	return struct {
		RequestIDs []string `json:"request_ids"`
	}{found}, nil
}

func (p *Pending) inRun(r *http.Request, _ crud.Params) (any, error) {
	return p.ids(r, `SELECT request_id FROM ai_pending_input WHERE request_id = ANY($1::text[])
		AND flow_run_id IS NOT NULL`)
}

func (p *Pending) decided(r *http.Request, _ crud.Params) (any, error) {
	return p.ids(r, `SELECT request_id FROM ai_input_responses WHERE request_id = ANY($1::text[])`)
}

func (p *Pending) tabWait(r *http.Request, _ crud.Params) (any, error) {
	sid, err := QueryPattern(r, "session_id", gate.SessionPattern)
	if err != nil {
		return nil, err
	}
	var waits bool
	err = p.Pool.QueryRow(r.Context(), `SELECT EXISTS (SELECT 1 AS n FROM ai_pending_input p
		JOIN ai_sessions s ON s.id = p.session_id AND s.runtime = 'unified'
		WHERE p.session_id = $1::text::uuid AND p.attention->>'reason' = 'tab_disconnected'
		LIMIT 1)`, sid).Scan(&waits)
	if err != nil {
		return nil, err
	}
	return struct {
		Waits bool `json:"waits"`
	}{waits}, nil
}

type recorded struct {
	Kind                string  `json:"kind"`
	Outcome             string  `json:"outcome"`
	Response            any     `json:"response"`
	ResponseHash        *string `json:"response_hash"`
	Responder           any     `json:"responder"`
	Reason              *string `json:"reason"`
	DecisionTraceparent *string `json:"decision_traceparent"`
}

func (p *Pending) recorded(r *http.Request, _ crud.Params) (any, error) {
	rid, err := crud.QueryText(r, "request_id", gate.RequestIDMax)
	if err != nil {
		return nil, err
	}
	var rec recorded
	var response *string
	var responder string
	err = p.Pool.QueryRow(r.Context(), `SELECT kind, outcome, response::text, response_hash,
		responder::text, reason, decision_traceparent FROM ai_input_responses
		WHERE request_id = $1`, rid).Scan(&rec.Kind, &rec.Outcome, &response,
		&rec.ResponseHash, &responder, &rec.Reason, &rec.DecisionTraceparent)
	out := struct {
		Recorded *recorded `json:"recorded"`
	}{}
	if errors.Is(err, pgx.ErrNoRows) {
		return out, nil
	}
	if err != nil {
		return nil, err
	}
	rec.Response, rec.Responder = parsed(response), jsonv.MustParse(responder)
	out.Recorded = &rec
	return out, nil
}

type parkTrace struct {
	Tool        string     `json:"tool"`
	CreatedAt   jsonv.Time `json:"created_at"`
	Traceparent *string    `json:"traceparent"`
}

func (p *Pending) parkTrace(r *http.Request, _ crud.Params) (any, error) {
	rid, err := crud.QueryText(r, "request_id", gate.RequestIDMax)
	if err != nil {
		return nil, err
	}
	var t parkTrace
	err = p.Pool.QueryRow(r.Context(), `SELECT tool, created_at, traceparent FROM ai_pending_input
		WHERE request_id = $1`, rid).Scan(&t.Tool, &t.CreatedAt, &t.Traceparent)
	out := struct {
		Trace *parkTrace `json:"trace"`
	}{}
	if errors.Is(err, pgx.ErrNoRows) {
		return out, nil
	}
	if err != nil {
		return nil, err
	}
	out.Trace = &t
	return out, nil
}

type done struct {
	Done bool `json:"done"`
}

func (p *Pending) decisionTrace(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "request_id", "traceparent")
	if err != nil {
		return nil, err
	}
	rid := b.Text("request_id", 1, gate.RequestIDMax)
	traceparent := b.Text("traceparent", 1, TraceparentMax)
	if err := b.Err(); err != nil {
		return nil, err
	}
	err = pgx.BeginFunc(r.Context(), p.Pool, func(tx pgx.Tx) error {
		_, err := tx.Exec(r.Context(), `UPDATE ai_pending_input SET decision_traceparent = $1
			WHERE request_id = $2`, traceparent, rid)
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{true}, nil
}

func (p *Pending) trace(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "request_id", "traceparent", "session_id", "attempt", "turn_id")
	if err != nil {
		return nil, err
	}
	rid := b.Text("request_id", 1, gate.RequestIDMax)
	traceparent := b.Text("traceparent", 1, TraceparentMax)
	sid := b.Text("session_id", 1, gate.NameMax)
	attempt := b.Text("attempt", 1, gate.NameMax)
	turn := b.Text("turn_id", 1, gate.NameMax)
	if err := b.Err(); err != nil {
		return nil, err
	}
	marked := false
	if audit.IsUUID(sid) && audit.IsUUID(turn) {
		err = pgx.BeginFunc(r.Context(), p.Pool, func(tx pgx.Tx) error {
			ctx := r.Context()
			var one int
			err := tx.QueryRow(ctx, `SELECT 1 FROM ai_sessions WHERE id = $1::text::uuid
				AND turn_attempt = $2 AND durable_turn_id = $3::text::uuid FOR SHARE`,
				sid, attempt, turn).Scan(&one)
			if errors.Is(err, pgx.ErrNoRows) {
				return nil
			}
			if err != nil {
				return err
			}
			tag, err := tx.Exec(ctx, `UPDATE ai_pending_input SET traceparent = $1
				WHERE request_id = $2 AND session_id = $3::text::uuid`, traceparent, rid, sid)
			marked = err == nil && tag.RowsAffected() > 0
			return err
		})
		if err != nil {
			return nil, err
		}
	}
	return struct {
		Marked bool `json:"marked"`
	}{marked}, nil
}

type gateSession struct {
	OwnerKind   string `json:"owner_kind"`
	OwnerID     string `json:"owner_id"`
	OwnerLabel  string `json:"owner_label"`
	CreatorKind string `json:"creator_kind"`
	CreatorID   string `json:"creator_id"`
}

func (p *Pending) session(r *http.Request, _ crud.Params) (any, error) {
	sid, err := crud.QueryText(r, "session_id", gate.NameMax)
	if err != nil {
		return nil, err
	}
	out := struct {
		Session *gateSession `json:"session"`
	}{}
	if !audit.IsUUID(sid) {
		return out, nil
	}
	var s gateSession
	err = p.Pool.QueryRow(r.Context(), `SELECT owner_kind, owner_id, owner_label, creator_kind,
		creator_id FROM ai_sessions WHERE id = $1::text::uuid`, sid).Scan(&s.OwnerKind,
		&s.OwnerID, &s.OwnerLabel, &s.CreatorKind, &s.CreatorID)
	if errors.Is(err, pgx.ErrNoRows) {
		return out, nil
	}
	if err != nil {
		return nil, err
	}
	out.Session = &s
	return out, nil
}

type run struct {
	WorkflowID    string `json:"workflow_id"`
	WorkflowRunID string `json:"workflow_run_id"`
}

func (p *Pending) sweepRuns(r *http.Request, _ crud.Params) (any, error) {
	age, err := QueryIntRequired(r, "min_age_s", 0, ageMaxS)
	if err != nil {
		return nil, err
	}
	recheck, err := QueryIntRequired(r, "recheck_s", 0, ageMaxS)
	if err != nil {
		return nil, err
	}
	rows, err := p.Pool.Query(r.Context(), `SELECT DISTINCT workflow_id, workflow_run_id
		FROM ai_pending_input WHERE created_at < now() - make_interval(secs => $1)
		AND (last_checked_at IS NULL OR last_checked_at < now() - make_interval(secs => $2))
		LIMIT $3`, float64(age), float64(recheck), runsMax)
	if err != nil {
		return nil, err
	}
	runs, err := pgx.CollectRows(rows, pgx.RowToStructByPos[run])
	if err != nil {
		return nil, err
	}
	if runs == nil {
		runs = []run{}
	}
	return struct {
		Runs []run `json:"runs"`
	}{runs}, nil
}

// runRef is a RunRef body object: its workflow id and run id.
func runRef(n *crud.Nested) (string, string) {
	defer n.Done()
	return n.Text("workflow_id", 0, gate.NameMax), n.Text("run_id", 0, gate.NameMax)
}

func (p *Pending) sweepChecked(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "workflow_id", "run_id")
	if err != nil {
		return nil, err
	}
	wid, rid := b.Text("workflow_id", 0, gate.NameMax), b.Text("run_id", 0, gate.NameMax)
	if err := b.Err(); err != nil {
		return nil, err
	}
	err = pgx.BeginFunc(r.Context(), p.Pool, func(tx pgx.Tx) error {
		_, err := tx.Exec(r.Context(), `UPDATE ai_pending_input SET last_checked_at = now()
			WHERE workflow_id = $1 AND workflow_run_id = $2`, wid, rid)
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{true}, nil
}

type cancelled struct {
	Removed int          `json:"removed"`
	Logged  gate.Batches `json:"logged"`
}

func (p *Pending) cancel(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "run", "session_id", "reason")
	if err != nil {
		return nil, err
	}
	var wid, rid string
	n := b.Object("run", "workflow_id", "run_id")
	if n != nil {
		wid, rid = runRef(n)
	}
	sid := b.OptText("session_id", 0, 1<<20)
	if sid != nil {
		b.Pattern("session_id", *sid, gate.SessionPattern)
	}
	reason := b.TextOr("reason", OrphanReason, 0, gate.TextMax)
	if err := b.Err(); err != nil {
		return nil, err
	}
	if (n == nil) == (sid == nil) {
		return nil, &httpx.Problem{Status: http.StatusUnprocessableEntity,
			Detail: "name exactly one of run and session_id"}
	}
	ctx := r.Context()
	conn, err := p.Pool.Acquire(ctx)
	if err != nil {
		return nil, err
	}
	defer conn.Release()
	var rows pgx.Rows
	if n != nil {
		rows, err = conn.Query(ctx, `SELECT request_id FROM ai_pending_input WHERE workflow_id = $1
			AND workflow_run_id = $2`, wid, rid)
	} else {
		rows, err = conn.Query(ctx, `SELECT request_id FROM ai_pending_input
			WHERE session_id = $1::text::uuid`, *sid)
	}
	if err != nil {
		return nil, err
	}
	ids, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return nil, err
	}
	out := cancelled{}
	for _, id := range ids {
		err := pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
			gone, err := cancelOne(ctx, tx, &out.Logged, id, reason)
			if gone {
				out.Removed++
			}
			return err
		})
		if err != nil {
			return nil, err
		}
	}
	return out, nil
}

// cancelOne resolves one entry cancelled in tx; false when the workflow resolved it first.
func cancelOne(ctx context.Context, tx pgx.Tx, logged *gate.Batches, request, reason string) (bool, error) {
	var sid, rid, kind, tool, summary string
	var hash, by *string
	var created time.Time
	err := tx.QueryRow(ctx, `DELETE FROM ai_pending_input WHERE request_id = $1 RETURNING
		request_id, session_id::text AS session_id, kind, tool, summary, input_hash,
		requested_by::text, created_at`, request).Scan(&rid, &sid, &kind, &tool, &summary,
		&hash, &by, &created)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO ai_input_responses (request_id, session_id, kind,
		outcome, responder, reason, tool, requested_at) VALUES ($1, $2::text::uuid, $3,
		'cancelled', $4::text::jsonb, $5, $6, $7)`,
		rid, sid, kind, jsonv.Dumps(gate.System()), reason, tool, created); err != nil {
		return false, err
	}
	m := gate.M
	var first jsonv.Object
	if kind == "approval" {
		first = gate.Event(m("type", "approval.resolved"), m("sessionId", sid), m("id", rid),
			m("approved", false), m("decision", "cancelled"), m("reason", reason))
	} else {
		first = gate.Event(m("type", "question.resolved"), m("sessionId", sid), m("id", rid),
			m("answered", false), m("reason", reason))
	}
	ended := gate.Event(m("type", "input.resolved"), m("sessionId", sid), m("id", rid),
		m("kind", kind), m("outcome", "cancelled"))
	if reason != "" {
		ended = append(ended, m("reason", reason))
	}
	evs := []jsonv.Object{first, ended}
	tag, err := tx.Exec(ctx, `UPDATE ai_sessions SET status = 'idle', updated_at = now()
		WHERE id = $1::text::uuid AND status IN ('waiting_approval', 'waiting_input')
		AND NOT EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = $1::text::uuid)`, sid)
	if err != nil {
		return false, err
	}
	if tag.RowsAffected() > 0 {
		evs = append(evs, gate.Event(m("type", "session.status"), m("sessionId", sid), m("status", "idle")))
	}
	if err := gate.Append(ctx, tx, logged, sid, evs); err != nil {
		return false, err
	}
	if kind == "approval" {
		label := "?"
		if o, ok := parsed(by).(jsonv.Object); ok {
			if v, _ := o.Get("label"); v != nil {
				label = gate.Str(v)
			}
		}
		detail := tool + ": " + reason + " (requested by " + label + ")"
		sys := gate.System()
		err := audit.Insert(ctx, tx, audit.Entry{
			Kind: "approval", Action: "cancelled", Surface: "system",
			ActorKind: str(sys, "kind"), ActorID: str(sys, "id"), ActorLabel: str(sys, "label"),
			Outcome: "refused", SessionID: &sid, ToolUseID: gate.ToolUseID(rid),
			Tier: audit.Ptr("outward"), InputHash: hash,
			InputSummary: audit.Ptr(audit.Cap(summary, 1000)),
			RequestID:    audit.Ptr(audit.Cap(rid, 500)), Detail: audit.Ptr(audit.Cap(detail, 500)),
			StartedAt: created, FinishedAt: time.Now(),
		})
		if err != nil {
			return false, err
		}
	}
	return true, nil
}

func str(o jsonv.Object, key string) string {
	s, _ := o.Text(key)
	return s
}

func (p *Pending) approval(r *http.Request, _ crud.Params) (any, error) {
	rid, err := crud.QueryText(r, "request_id", gate.RequestIDMax)
	if err != nil {
		return nil, err
	}
	hash, err := crud.QueryText(r, "input_hash", gate.NameMax)
	if err != nil {
		return nil, err
	}
	var responder string
	err = p.Pool.QueryRow(r.Context(), `SELECT r.responder::text FROM ai_input_responses r
		JOIN ai_audit a ON a.request_id = r.request_id AND a.kind = 'approval'
		AND a.action = 'approved'
		WHERE r.request_id = $1 AND r.kind = 'approval' AND r.outcome = 'approved'
		AND a.input_hash = $2 LIMIT 1`, rid, hash).Scan(&responder)
	out := struct {
		Responder any `json:"responder"`
	}{}
	if errors.Is(err, pgx.ErrNoRows) {
		return out, nil
	}
	if err != nil {
		return nil, err
	}
	v := jsonv.MustParse(responder)
	if _, ok := v.(jsonv.Object); !ok {
		v = jsonv.Object{}
	}
	out.Responder = v
	return out, nil
}
