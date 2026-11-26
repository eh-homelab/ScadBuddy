// Package gate is the tool-call gate's writes: backend/scadbuddy/agent_data/gate.py (removed in #7667),
// gate_validate.py (removed in #7667), gate_canonical.py (removed in #7667) and api/crud_agent_gate.py (removed in #7667), statement for statement
// (area gate). Its helpers build the session events the other gate areas write (pending-input,
// approvals, questions, escalations), key order included, as the Python API built them.
package gate

import (
	"context"
	"encoding/json"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/audit"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/events"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

const (
	protocolVersion = 1
	// InputChannel is what a parked call waits on, sent as a resolution commits.
	InputChannel = "scadbuddy_input"
	// ApprovalSummaryMax is the agent's APPROVAL_SUMMARY_MAX.
	ApprovalSummaryMax = 500 + 200 + 1
)

// System is ScadBuddy itself as a responder.
func System() jsonv.Object {
	return jsonv.Object{{Key: "kind", Value: "system"}, {Key: "id", Value: "scadbuddy"},
		{Key: "label", Value: "ScadBuddy"}}
}

var hopActions = map[string]string{
	"approved": "approve", "denied": "deny", "expired": "timeout", "cancelled": "cancelled",
}

// Logged is one batch of a session's events a transaction appended, with their seqs.
type Logged struct {
	SessionID string         `json:"session_id"`
	Events    []jsonv.Object `json:"events"`
	Seqs      []int64        `json:"seqs"`
}

// Batches is a write's appended batches, never null in an answer.
type Batches []Logged

// MarshalJSON writes no batches as [].
func (b Batches) MarshalJSON() ([]byte, error) {
	if b == nil {
		return []byte("[]"), nil
	}
	return httpx.Marshal([]Logged(b))
}

// Event is a panel-protocol event: its version first, then members.
func Event(members ...jsonv.Member) jsonv.Object {
	return append(jsonv.Object{{Key: "v", Value: protocolVersion}}, members...)
}

// M is a member.
func M(key string, value any) jsonv.Member { return jsonv.Member{Key: key, Value: value} }

// Append is the agent's appendEventsIn in tx; the batch goes into logged.
func Append(ctx context.Context, tx pgx.Tx, logged *Batches, session string, evs []jsonv.Object) error {
	if len(evs) == 0 {
		return nil
	}
	texts := make([]string, len(evs))
	for i, e := range evs {
		texts[i] = jsonv.Dumps(e)
	}
	seqs, err := events.AppendIn(ctx, tx, session, texts)
	if err != nil {
		return err
	}
	*logged = append(*logged, Logged{session, evs, seqs})
	return nil
}

// Cap is gate.py's _cap: at most limit code points, "…" when cut.
func Cap(text string, limit int) string {
	runes := []rune(text)
	if len(runes) <= limit {
		return text
	}
	return string(runes[:limit-1]) + "…"
}

// Head is Python's text[:n].
func Head(text string, n int) string {
	runes := []rune(text)
	if len(runes) <= n {
		return text
	}
	return string(runes[:n])
}

// ToolUseID is a durable request id's tool_use_id (the agent's parseRequestId), else nil.
func ToolUseID(requestID string) *string {
	prefix, rest, ok := strings.Cut(requestID, ":")
	if prefix != "durable" || !ok {
		return nil
	}
	parts := strings.SplitN(rest, ":", 3)
	if len(parts) != 3 || slices.Contains(parts, "") || !audit.IsUUID(parts[0]) {
		return nil
	}
	return &parts[2]
}

// Str is Python's str of a JSON value: what an f-string writes.
func Str(v any) string {
	switch t := v.(type) {
	case nil:
		return "None"
	case bool:
		if t {
			return "True"
		}
		return "False"
	case string:
		return t
	}
	return jsonv.Dumps(v)
}

// Truthy is Python's truth of a JSON value.
func Truthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case string:
		return t != ""
	case []any:
		return len(t) > 0
	case jsonv.Object:
		return len(t) > 0
	}
	if n, ok := v.(json.Number); ok {
		f, err := n.Float64()
		return err != nil || f != 0
	}
	return true
}

func actor(responder jsonv.Object) string {
	kind := ""
	if v, _ := responder.Get("kind"); Truthy(v) {
		kind = Str(v)
	}
	id := ""
	if v, _ := responder.Get("id"); Truthy(v) {
		id = Str(v)
	}
	if kind == "session" {
		return "session:" + id
	}
	if kind == "browser" || kind == "system" || kind == "flow" {
		return kind
	}
	return kind + ":" + id
}

// Hop is a step of an entry's hops.
func Hop(at, action, by string, reason *string) jsonv.Object {
	step := jsonv.Object{M("hop", at), M("action", action), M("actor", by),
		M("at", jsonv.ISO(time.Now()))}
	if reason != nil && *reason != "" {
		step = append(step, M("reason", *reason))
	}
	return step
}

// Row is an ai_pending_input row as the gate reads it.
type Row struct {
	RequestID, SessionID, WorkflowID, WorkflowRunID, Kind, Tool, Summary string
	InputHash                                                            *string
	Prompt                                                               string
	RequestedBy                                                          any
	Responders                                                           []string
	Attention                                                            any
	CreatedAt, ExpiresAt                                                 time.Time
	FlowRunID, Hop                                                       *string
	Hops                                                                 any
	Gate                                                                 any
	DecisionTraceparent                                                  *string
	Expired                                                              bool
}

// RowColumns are Row's columns, for a SELECT or a RETURNING.
const RowColumns = `request_id, session_id::text, workflow_id, workflow_run_id, kind, tool,
 summary, input_hash, prompt, requested_by::text, responders, attention::text, created_at,
 expires_at, flow_run_id, hop, hops::text, gate::text, decision_traceparent`

// ScanRows reads rows of RowColumns, and expired after them when withExpired.
func ScanRows(rows pgx.Rows, withExpired bool) ([]*Row, error) {
	defer rows.Close()
	var out []*Row
	for rows.Next() {
		r := &Row{}
		var by, attention, hops, gate *string
		dest := []any{&r.RequestID, &r.SessionID, &r.WorkflowID, &r.WorkflowRunID, &r.Kind,
			&r.Tool, &r.Summary, &r.InputHash, &r.Prompt, &by, &r.Responders, &attention,
			&r.CreatedAt, &r.ExpiresAt, &r.FlowRunID, &r.Hop, &hops, &gate, &r.DecisionTraceparent}
		if withExpired {
			dest = append(dest, &r.Expired)
		}
		if err := rows.Scan(dest...); err != nil {
			return nil, err
		}
		r.RequestedBy, r.Attention, r.Hops, r.Gate = parsed(by), parsed(attention), parsed(hops), parsed(gate)
		out = append(out, r)
	}
	return out, rows.Err()
}

func parsed(text *string) any {
	if text == nil {
		return nil
	}
	return jsonv.MustParse(*text)
}

// entryOf is the §6.6 entry shape (the agent's InputEntry) of a row.
func entryOf(r *Row) jsonv.Object {
	responders := r.Responders
	if responders == nil {
		responders = []string{}
	}
	e := jsonv.Object{M("id", r.RequestID), M("kind", r.Kind), M("session_id", r.SessionID),
		M("tool", r.Tool), M("summary", r.Summary), M("input_hash", nullable(r.InputHash)),
		M("prompt", r.Prompt), M("requested_by", r.RequestedBy), M("responders", responders),
		M("created_at", jsonv.ISO(r.CreatedAt)), M("expires_at", jsonv.ISO(r.ExpiresAt))}
	if r.Attention != nil {
		e = append(e, M("attention", r.Attention))
	}
	return e
}

func nullable(s *string) any {
	if s == nil {
		return nil
	}
	return *s
}

// Resolved records the end of the entry r, already deleted in tx.
func Resolved(ctx context.Context, tx pgx.Tx, logged *Batches, r *Row, outcome string,
	responder jsonv.Object, response any, reason, digest *string) error {
	attention, _ := r.Attention.(jsonv.Object)
	if outcome == "timed_out" && reason == nil && Truthy(r.Attention) {
		onTimeout, _ := attention.Get("on_timeout")
		if onTimeout == nil {
			onTimeout = "proceed"
		}
		reason = audit.Ptr("nobody replied in time (on_timeout: " + Str(onTimeout) + ")")
	}
	hops, _ := r.Hops.([]any)
	hops = slices.Clone(hops)
	if hops == nil {
		hops = []any{}
	}
	if r.FlowRunID != nil && r.Hop != nil {
		action, ok := hopActions[outcome]
		if !ok {
			action = outcome
		}
		hops = append(hops, Hop(*r.Hop, action, actor(responder), reason))
	}
	var responseText *string
	if response != nil {
		responseText = audit.Ptr(jsonv.Dumps(response))
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, response,
		 responder, reason, hops, response_hash, tool, requested_at, decision_traceparent)
		 VALUES ($1, $2::text::uuid, $3, $4, $5::text::jsonb, $6::text::jsonb, $7,
		 $8::text::jsonb, $9, $10, $11, $12)`,
		r.RequestID, r.SessionID, r.Kind, outcome, responseText, jsonv.Dumps(responder), reason,
		jsonv.Dumps(hops), digest, r.Tool, r.CreatedAt, r.DecisionTraceparent); err != nil {
		return err
	}
	kind, _ := responder.Text("kind")
	person := kind != "system"
	sid, rid := r.SessionID, r.RequestID
	given := reason != nil && *reason != ""
	var evs []jsonv.Object
	if r.Kind == "approval" {
		decision := "cancelled"
		if outcome == "approved" || outcome == "denied" || outcome == "expired" {
			decision = outcome
		}
		decided := Event(M("type", "approval.resolved"), M("sessionId", sid), M("id", rid),
			M("approved", outcome == "approved"), M("decision", decision))
		if person && (outcome == "approved" || outcome == "denied") {
			decided = append(decided, M("by", responder))
		}
		if given && outcome != "approved" && outcome != "denied" {
			decided = append(decided, M("reason", *reason))
		}
		evs = append(evs, decided)
	} else {
		body, isObject := response.(jsonv.Object)
		var answers any
		if isObject {
			answers, _ = body.Get("answers")
		}
		asked := Event(M("type", "question.resolved"), M("sessionId", sid), M("id", rid),
			M("answered", outcome == "answered"))
		if outcome == "answered" && isObject && answers != nil {
			asked = append(asked, M("answers", list(answers)), M("by", responder))
		}
		if given && outcome != "answered" {
			asked = append(asked, M("reason", *reason))
		}
		if outcome == "cancelled" && isObject {
			if v, _ := body.Get("reconnected"); v == true {
				asked = append(asked, M("reconnected", true))
			}
		}
		evs = append(evs, asked)
	}
	ended := Event(M("type", "input.resolved"), M("sessionId", sid), M("id", rid),
		M("kind", r.Kind), M("outcome", outcome))
	if given {
		ended = append(ended, M("reason", *reason))
	}
	evs = append(evs, ended)
	// The status follows what of the session is still parked, read under the session row's
	// lock, so two resolves that overlap cannot each see the other's entry still there (#3685).
	if _, err := tx.Exec(ctx, `SELECT 1 FROM ai_sessions WHERE id = $1::text::uuid FOR UPDATE`, sid); err != nil {
		return err
	}
	var moved string
	err := tx.QueryRow(ctx,
		`WITH next AS (SELECT CASE
		 WHEN EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = $1::text::uuid
		 AND kind = 'approval') THEN 'waiting_approval'
		 WHEN EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = $1::text::uuid)
		 THEN 'waiting_input' ELSE 'running' END AS status)
		 UPDATE ai_sessions SET status = next.status, updated_at = now() FROM next
		 WHERE id = $1::text::uuid AND ai_sessions.status IN ('waiting_approval', 'waiting_input')
		 AND ai_sessions.status <> next.status RETURNING ai_sessions.status`, sid).Scan(&moved)
	switch {
	case err == nil:
		evs = append(evs, Event(M("type", "session.status"), M("sessionId", sid), M("status", moved)))
	case err != pgx.ErrNoRows:
		return err
	}
	if err := Append(ctx, tx, logged, sid, evs); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `SELECT pg_notify($1, $2)`, InputChannel, rid); err != nil {
		return err
	}
	if r.Kind == "approval" {
		return Audit(ctx, tx, r, outcome, responder, reason)
	}
	if outcome != "answered" {
		return auditUnanswered(ctx, tx, r, outcome, reason)
	}
	return nil
}

// list is Python's list of a JSON value.
func list(v any) []any {
	switch t := v.(type) {
	case []any:
		return t
	case jsonv.Object:
		keys := make([]any, len(t))
		for i, m := range t {
			keys[i] = m.Key
		}
		return keys
	case string:
		chars := []any{}
		for _, r := range t {
			chars = append(chars, string(r))
		}
		return chars
	}
	return []any{v}
}

func suffix(reason *string) string {
	if reason == nil || *reason == "" {
		return ""
	}
	return ": " + *reason
}

func auditUnanswered(ctx context.Context, tx pgx.Tx, r *Row, outcome string, reason *string) error {
	action := "cancelled"
	if outcome == "expired" || outcome == "timed_out" {
		action = outcome
	}
	detail := r.Tool + " question " + r.RequestID + suffix(reason)
	_, err := tx.Exec(ctx,
		`INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id,
		 principal_label, session_id, tool_use_id, tier, request_id, outcome, detail,
		 started_at, finished_at, duration_ms)
		 VALUES ('question', $1, 'system', 'system', 'scadbuddy', 'ScadBuddy', $2::text::uuid, $3,
		 'read', $4, 'refused', $5, $6, now(),
		 GREATEST(0, (extract(epoch FROM now() - $6::timestamptz) * 1000)::int))`,
		action, r.SessionID, ToolUseID(r.RequestID), r.RequestID, Head(detail, 500), r.CreatedAt)
	return err
}

// Audit is the approval row ApprovalService writes for a classic decision (audit/log.ts).
func Audit(ctx context.Context, tx pgx.Tx, r *Row, outcome string, responder jsonv.Object, reason *string) error {
	kind, _ := responder.Text("kind")
	person := kind != "system"
	by, surface := responder, "http"
	if !person {
		by, surface = System(), "system"
	}
	requested := any(nil)
	if o, ok := r.RequestedBy.(jsonv.Object); ok {
		requested, _ = o.Get("label")
	}
	label := "?"
	if requested != nil {
		label = Str(requested)
	}
	detail := r.Tool + suffix(reason) + " (requested by " + label + ")"
	result := "refused"
	switch outcome {
	case "approved":
		result = "ok"
	case "denied":
		result = "denied"
	}
	get := func(key string) string {
		v, _ := by.Get(key)
		return Str(v)
	}
	_, err := tx.Exec(ctx,
		`INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id,
		 principal_label, session_id, tool_use_id, tier, input_hash, input_summary, request_id,
		 outcome, detail, started_at, finished_at, duration_ms)
		 VALUES ('approval', $1, $2, $3, $4, $5, $6::text::uuid, $7, 'outward', $8, $9, $10, $11,
		 $12, $13, now(), GREATEST(0, (extract(epoch FROM now() - $13::timestamptz) * 1000)::int))`,
		outcome, surface, Head(get("kind"), 50), Head(get("id"), 200), Head(get("label"), 200),
		r.SessionID, ToolUseID(r.RequestID), r.InputHash, Head(r.Summary, 1000), r.RequestID,
		result, Head(detail, 500), r.CreatedAt)
	return err
}

// LockSessionOf locks the session request's entry is in, before anything of the entry: two
// resolutions in one session then queue on it, never deadlock.
func LockSessionOf(ctx context.Context, tx pgx.Tx, request string) error {
	_, err := tx.Exec(ctx, `SELECT 1 FROM ai_sessions WHERE id =
		(SELECT session_id FROM ai_pending_input WHERE request_id = $1) FOR UPDATE`, request)
	return err
}
