// Package audit is the agent's audit log: backend/scadbuddy/agent_data/audit_log.py and
// api/crud_agent_audit.py, statement for statement (area audit). Insert is also every other
// area's audit row.
package audit

import (
	"context"
	"fmt"
	"math"
	"net/http"
	"regexp"
	"strings"
	"time"
	"unicode/utf16"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

const (
	retentionMinDays = 1
	retentionMaxDays = 3650
	defaultPage      = 50
	maxPage          = 200
)

// Kinds, Outcomes and Surfaces are the columns' values.
var (
	Kinds = []string{"tool_call", "resource", "approval", "credential", "plugin", "settings",
		"token", "memory", "http", "question", "turn", "payload_key"}
	Outcomes = []string{"ok", "error", "refused", "denied"}
	Surfaces = []string{"harness", "mcp", "http", "system"}
	Tiers    = []string{"read", "write", "outward"}
)

// Entry is the agent's AuditEntry, its texts already capped. A time is a time.Time or a text
// Postgres reads as a timestamptz.
type Entry struct {
	Kind, Action, Surface          string
	ActorKind, ActorID, ActorLabel string
	Outcome                        string
	ClientIP, SessionID, TurnID    *string
	ToolUseID, Tier, InputHash     *string
	InputSummary, ApprovalID       *string
	RequestID, Detail              *string
	StartedAt, FinishedAt          any
	CostUSD                        *float64
	CostPriced                     *bool
	CostEstimatedUSD               *float64
}

// Ptr is a pointer to s.
func Ptr(s string) *string { return &s }

// Cap is the agent's audit/log.ts cap: at most limit UTF-16 units, cut between code points,
// with "…" when cut.
func Cap(text string, limit int) string {
	units := utf16.Encode([]rune(text))
	if len(units) <= limit {
		return text
	}
	kept := units[:max(limit-1, 0)]
	if n := len(kept); n > 0 && kept[n-1] >= 0xd800 && kept[n-1] <= 0xdbff {
		kept = kept[:n-1]
	}
	return string(utf16.Decode(kept)) + "…"
}

var uuidAnyCase = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

// IsUUID is gate.py's _is_uuid: a UUID in any case.
func IsUUID(s string) bool { return uuidAnyCase.MatchString(s) }

func uuidOrNil(s *string) *string {
	if s != nil && IsUUID(*s) {
		return s
	}
	return nil
}

func usd(v *float64) *float64 {
	if v == nil || math.IsNaN(*v) || math.IsInf(*v, 0) {
		return nil
	}
	f := math.Max(*v, 0)
	return &f
}

// timeText is a time as the statement casts it.
func timeText(v any) *string {
	switch t := v.(type) {
	case nil:
		return nil
	case time.Time:
		s := t.Format(time.RFC3339Nano)
		return &s
	case jsonv.Time:
		s := t.Format(time.RFC3339Nano)
		return &s
	case string:
		return &t
	case *string:
		return t
	}
	panic(fmt.Sprintf("audit: no time in %T", v))
}

// Insert is AuditLog.insert: one row, a tool call's approver copied from its ai_approvals row
// or its request's approved response in the same statement.
func Insert(ctx context.Context, tx pgx.Tx, e Entry) error {
	started, finished := timeText(e.StartedAt), timeText(e.FinishedAt)
	toolCall := e.Kind == "tool_call"
	var responderOf *string
	if toolCall {
		responderOf = e.RequestID
	}
	by := func(field string) string {
		return fmt.Sprintf("COALESCE((SELECT decided_by_%s FROM ai_approvals WHERE id = $14::text::uuid"+
			" AND decision = 'approved' AND $15::boolean),"+
			" (SELECT responder->>'%s' FROM ai_input_responses WHERE request_id = $16::text"+
			" AND outcome = 'approved'))", field, field)
	}
	_, err := tx.Exec(ctx,
		`INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id,
		 principal_label, client_ip, session_id, turn_id, tool_use_id, tier, input_hash,
		 input_summary, approval_id, approved_by_kind, approved_by_id, approved_by_label,
		 request_id, outcome, detail, started_at, finished_at, duration_ms, cost_usd,
		 cost_priced, cost_estimated_usd)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8::text::uuid, $9::text::uuid, $10, $11, $12, $13,
		 $14::text::uuid, `+by("kind")+`, `+by("id")+`, `+by("label")+`,
		 $17, $18, $19, $20::text::timestamptz, $21::text::timestamptz,
		 CASE WHEN $20::text IS NULL OR $21::text IS NULL THEN NULL ELSE GREATEST(0,
		 trunc(extract(epoch FROM $21::text::timestamptz - $20::text::timestamptz) * 1000))::int END,
		 $22, $23, $24)`,
		e.Kind, e.Action, e.Surface, e.ActorKind, e.ActorID, e.ActorLabel, e.ClientIP,
		uuidOrNil(e.SessionID), uuidOrNil(e.TurnID), e.ToolUseID, e.Tier, e.InputHash,
		e.InputSummary, uuidOrNil(e.ApprovalID), toolCall, responderOf, e.RequestID, e.Outcome,
		e.Detail, started, finished, usd(e.CostUSD), e.CostPriced, usd(e.CostEstimatedUSD))
	return err
}

// Log is the area's routes.
type Log struct{ Pool *pgxpool.Pool }

// Routes are the area's routes.
func (l *Log) Routes() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/audit/record", Handle: l.record},
		{Method: "POST", Path: "/audit/list", Handle: l.list},
		{Method: "POST", Path: "/audit/prune", Handle: l.prune},
	}
}

type done struct {
	Done bool `json:"done"`
}

func (l *Log) record(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "kind", "action", "surface", "actor", "outcome", "client_ip",
		"session_id", "turn_id", "tool_use_id", "tier", "input_hash", "input_summary",
		"approval_id", "request_id", "detail", "started_at", "finished_at", "cost_usd",
		"cost_priced", "cost_estimated_usd")
	if err != nil {
		return nil, err
	}
	e := Entry{
		Kind:    b.Choice("kind", Kinds...),
		Action:  b.Text("action", 0, 200),
		Surface: b.Choice("surface", Surfaces...),
		Outcome: b.Choice("outcome", Outcomes...),
	}
	if actor := b.Need("actor", "kind", "id", "label"); actor != nil {
		e.ActorKind, e.ActorID, e.ActorLabel =
			actor.Text("kind", 0, 50), actor.Text("id", 0, 200), actor.Text("label", 0, 200)
		actor.Done()
	}
	e.ClientIP = b.OptText("client_ip", 0, 200, nil)
	e.SessionID = b.OptText("session_id", 0, 200, nil)
	e.TurnID = b.OptText("turn_id", 0, 200, nil)
	e.ToolUseID = b.OptText("tool_use_id", 0, 1024, nil)
	e.Tier = b.OptChoice("tier", Tiers...)
	e.InputHash = b.OptText("input_hash", 0, 1024, nil)
	e.InputSummary = b.OptText("input_summary", 0, 1000, nil)
	e.ApprovalID = b.OptText("approval_id", 0, 200, nil)
	e.RequestID = b.OptText("request_id", 0, 500, nil)
	e.Detail = b.OptText("detail", 0, 500, nil)
	if s := b.Datetime("started_at", true); s != nil {
		e.StartedAt = *s
	}
	if s := b.Datetime("finished_at", true); s != nil {
		e.FinishedAt = *s
	}
	e.CostUSD = b.OptFloat("cost_usd", math.Inf(-1), math.Inf(1))
	e.CostPriced = b.OptBool("cost_priced")
	e.CostEstimatedUSD = b.OptFloat("cost_estimated_usd", math.Inf(-1), math.Inf(1))
	if err := b.Err(); err != nil {
		return nil, err
	}
	err = pgx.BeginFunc(r.Context(), l.Pool, func(tx pgx.Tx) error { return Insert(r.Context(), tx, e) })
	if err != nil {
		return nil, err
	}
	return done{true}, nil
}

// Row is an ai_audit row as the list reads it.
type Row struct {
	ID               string      `json:"id"`
	At               jsonv.Time  `json:"at"`
	Kind             string      `json:"kind"`
	Action           string      `json:"action"`
	Surface          string      `json:"surface"`
	PrincipalKind    string      `json:"principal_kind"`
	PrincipalID      string      `json:"principal_id"`
	PrincipalLabel   string      `json:"principal_label"`
	ClientIP         *string     `json:"client_ip"`
	SessionID        *string     `json:"session_id"`
	TurnID           *string     `json:"turn_id"`
	ToolUseID        *string     `json:"tool_use_id"`
	Tier             *string     `json:"tier"`
	InputHash        *string     `json:"input_hash"`
	InputSummary     *string     `json:"input_summary"`
	ApprovalID       *string     `json:"approval_id"`
	ApprovedByKind   *string     `json:"approved_by_kind"`
	ApprovedByID     *string     `json:"approved_by_id"`
	ApprovedByLabel  *string     `json:"approved_by_label"`
	Outcome          string      `json:"outcome"`
	Detail           *string     `json:"detail"`
	StartedAt        *jsonv.Time `json:"started_at"`
	FinishedAt       *jsonv.Time `json:"finished_at"`
	DurationMS       *int64      `json:"duration_ms"`
	CostUSD          *float64    `json:"cost_usd"`
	CostPriced       *bool       `json:"cost_priced"`
	CostEstimatedUSD *float64    `json:"cost_estimated_usd"`
}

const columns = `id::text AS id, at, kind, action, surface, principal_kind, principal_id,
 principal_label, client_ip, session_id::text AS session_id, turn_id::text AS turn_id,
 tool_use_id, tier, input_hash, input_summary, approval_id::text AS approval_id,
 approved_by_kind, approved_by_id, approved_by_label, outcome, detail, started_at, finished_at,
 duration_ms, cost_usd, cost_priced, cost_estimated_usd`

func (l *Log) list(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "kind", "outcome", "surface", "action", "session_id", "principal",
		"since", "until", "before", "limit")
	if err != nil {
		return nil, err
	}
	filters := []struct {
		column string
		value  *string
		cast   string
	}{
		{"kind", b.OptChoice("kind", Kinds...), ""},
		{"outcome", b.OptChoice("outcome", Outcomes...), ""},
		{"surface", b.OptChoice("surface", Surfaces...), ""},
		{"action", b.OptText("action", 0, 200, nil), ""},
		{"session_id", b.OptText("session_id", 0, 200, nil), "::text::uuid"},
		{"principal_id", b.OptText("principal", 0, 200, nil), ""},
	}
	since, until := b.Datetime("since", true), b.Datetime("until", true)
	before := b.OptIntRange("before", 0, math.MaxInt64)
	limit := b.OptIntRange("limit", math.MinInt64, math.MaxInt64)
	if err := b.Err(); err != nil {
		return nil, err
	}
	if s := filters[4].value; s != nil && !IsUUID(*s) {
		return page{Rows: []Row{}}, nil
	}
	var clauses []string
	var args []any
	arg := func(v any) string {
		args = append(args, v)
		return fmt.Sprintf("$%d", len(args))
	}
	for _, f := range filters {
		if f.value != nil {
			clauses = append(clauses, f.column+" = "+arg(*f.value)+f.cast)
		}
	}
	if since != nil {
		clauses = append(clauses, "at >= "+arg(*since)+"::text::timestamptz")
	}
	if until != nil {
		clauses = append(clauses, "at < "+arg(*until)+"::text::timestamptz")
	}
	if before != nil {
		clauses = append(clauses, "id < "+arg(*before))
	}
	n := int64(defaultPage)
	if limit != nil {
		n = *limit
	}
	n = min(max(n, 1), maxPage)
	where := ""
	if len(clauses) > 0 {
		where = "WHERE " + strings.Join(clauses, " AND ")
	}
	rows, err := l.Pool.Query(r.Context(), "SELECT "+columns+" FROM ai_audit "+where+
		" ORDER BY ai_audit.id DESC LIMIT "+arg(n+1), args...)
	if err != nil {
		return nil, err
	}
	found, err := pgx.CollectRows(rows, pgx.RowToStructByPos[Row])
	if err != nil {
		return nil, err
	}
	out := page{Rows: found}
	if int64(len(found)) > n {
		out.Rows = found[:n]
		out.Next = &found[n-1].ID
	}
	if out.Rows == nil {
		out.Rows = []Row{}
	}
	return out, nil
}

type page struct {
	Rows []Row   `json:"rows"`
	Next *string `json:"next"`
}

func (l *Log) prune(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "days")
	if err != nil {
		return nil, err
	}
	days := b.IntRange("days", math.MinInt64, math.MaxInt64)
	if err := b.Err(); err != nil {
		return nil, err
	}
	days = min(max(days, retentionMinDays), retentionMaxDays)
	var deleted int64
	err = pgx.BeginFunc(r.Context(), l.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(r.Context(), `SELECT set_config('scadbuddy.audit_prune', 'on', true)`); err != nil {
			return err
		}
		tag, err := tx.Exec(r.Context(),
			`DELETE FROM ai_audit WHERE at < now() - ($1 * interval '1 day')`, days)
		deleted = tag.RowsAffected()
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Deleted int64 `json:"deleted"`
	}{deleted}, nil
}
