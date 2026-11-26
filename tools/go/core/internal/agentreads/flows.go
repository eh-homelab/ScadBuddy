package agentreads

import (
	"bytes"
	"encoding/json"
	"errors"
	"log/slog"
	"math"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

const (
	idMax              = 200
	requestIDMax       = 1024
	inputHashMax       = 256
	stillWaitingMax    = 500
	parentHopPageMax   = 200
	waitingRunsMax     = 200
	stillWaitingBodyMx = 8 * 1024 * 1024 // api/limits.py MAX_TEXT_BODY_BYTES
)

// grantInForce: a run grant's approval holds only while the grant is in force.
const grantInForce = "EXISTS (SELECT 1 FROM flow_grants g WHERE 'grant:' || g.id = d.responder" +
	" AND g.status = 'approved' AND g.revoked_at IS NULL AND now() < g.expires_at)"

// approved is one approval of the call with this hash.
const approved = "FROM workflow_run_decisions d WHERE d.request_id = $1" +
	" AND d.kind = 'approval' AND d.outcome = 'approved' AND d.input_hash = $2"

// parentApprovalLive is a parked approval at hop parent whose hop has not timed out by
// Postgres's clock; a malformed hop_until is that call skipped.
const parentApprovalLive = "w->>'hop' = 'parent' AND w->>'kind' = 'approval'" +
	" AND CASE WHEN pg_input_is_valid(w->>'hop_until', 'timestamptz')" +
	" THEN (w->>'hop_until')::timestamptz > now() ELSE false END"

// Flows are area flows' routes: reads only.
func (a *Reads) Flows() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/flow-runs/{run_id}/starter", Handle: a.runStarter},
		{Method: "GET", Path: "/flow-decisions", Handle: a.decisionChecks},
		{Method: "GET", Path: "/flow-grants/in-force", Handle: a.grantInForce},
		{Method: "GET", Path: "/flow-runs/{run_id}/queue", Handle: a.runQueue},
		{Method: "GET", Path: "/flow-runs/{run_id}/steps", Handle: a.runSteps},
		{Method: "GET", Path: "/flow-runs/waiting", Handle: a.waitingRuns},
		{Method: "GET", Path: "/flow-runs/parent-hop-approvals", Handle: a.parentHopApprovals},
		{Method: "POST", Path: "/flow-runs/parent-hop/waiting", Handle: a.stillWaiting, MaxBody: stillWaitingBodyMx},
	}
}

// runID is the path's run id, 1 to idMax characters.
func runID(p crud.Params) (string, error) {
	id := p["run_id"]
	if n := len([]rune(id)); n < 1 || n > idMax {
		return "", httpx.Invalid(httpx.Issue{Loc: []string{"path", "run_id"},
			Msg: "String should have at most 200 characters"})
	}
	return id, nil
}

// decode is a jsonb value as Python reads it, its numbers kept as written.
func decode(raw json.RawMessage) any {
	if raw == nil {
		return nil
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	_ = dec.Decode(&v)
	return v
}

func text(v any) *string {
	if s, ok := v.(string); ok {
		return &s
	}
	return nil
}

// objects are the JSON objects of a list; none when raw is not a list.
func objects(v any) []map[string]any {
	list, _ := v.([]any)
	var out []map[string]any
	for _, item := range list {
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

func dict(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

type startedBy struct {
	Kind      *string `json:"kind"`
	Principal *string `json:"principal"`
	Session   *string `json:"session"`
}

type runStarter struct {
	Open       bool      `json:"open"`
	Unrecorded bool      `json:"unrecorded"`
	StartedBy  startedBy `json:"started_by"`
}

// runStarter is who started a run, whether execution is its open execution, and whether the
// row has not recorded it. Under the row's share lock: a Reset holds it FOR UPDATE.
func (a *Reads) runStarter(r *http.Request, p crud.Params) (any, error) {
	id, err := runID(p)
	if err != nil {
		return nil, err
	}
	execution, err := crud.QueryString(r, "execution", 0, idMax, true)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var found *runStarter
	err = pgx.BeginFunc(ctx, a.Pool, func(tx pgx.Tx) error {
		var by json.RawMessage
		var out runStarter
		err := tx.QueryRow(ctx, `SELECT started_by,
			status IN ('running', 'waiting') AND NOT ($1 = ANY(superseded_run_ids)) AS open,
			workflow_run_id IS DISTINCT FROM $1
			AND NOT ($1 = ANY(superseded_run_ids)) AS unrecorded
			FROM workflow_runs WHERE id = $2 FOR SHARE`, *execution, id).Scan(&by, &out.Open, &out.Unrecorded)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		m := dict(decode(by))
		out.StartedBy = startedBy{text(m["kind"]), text(m["principal"]), text(m["session"])}
		found = &out
		return nil
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Run *runStarter `json:"run"`
	}{found}, nil
}

func (a *Reads) decisionChecks(r *http.Request, _ crud.Params) (any, error) {
	request, err := crud.QueryString(r, "request_id", 1, requestIDMax, true)
	issues := crud.Issues(err)
	hash, err := crud.QueryString(r, "input_hash", 1, inputHashMax, true)
	if issues = append(issues, crud.Issues(err)...); len(issues) > 0 {
		return nil, httpx.Invalid(issues...)
	}
	var out struct {
		Approved           bool `json:"approved"`
		ApprovedUnresolved bool `json:"approved_unresolved"`
		ByPerson           bool `json:"by_person"`
		Lapsed             bool `json:"lapsed"`
	}
	err = a.Pool.QueryRow(r.Context(), `SELECT EXISTS (SELECT 1 `+approved+`
		AND (d.responder NOT LIKE 'grant:%' OR `+grantInForce+`)) AS approved,
		EXISTS (SELECT 1 `+approved+`
		AND (d.responder LIKE 'grant:%' OR d.response ? 'narrowed_by')
		AND (d.responder NOT LIKE 'grant:%' OR `+grantInForce+`)) AS approved_unresolved,
		EXISTS (SELECT 1 `+approved+` AND d.responder = 'browser') AS by_person,
		EXISTS (SELECT 1 FROM workflow_run_decisions d
		JOIN flow_grants g ON 'grant:' || g.id = d.responder
		WHERE d.request_id = $1 AND d.kind = 'approval'
		AND d.outcome = 'approved' AND d.input_hash = $2
		AND (g.status <> 'approved' OR g.revoked_at IS NOT NULL OR g.expires_at IS NULL
		OR now() >= g.expires_at)) AS lapsed`, *request, *hash).
		Scan(&out.Approved, &out.ApprovedUnresolved, &out.ByPerson, &out.Lapsed)
	if err != nil {
		return nil, err
	}
	return out, nil
}

func (a *Reads) grantInForce(r *http.Request, _ crud.Params) (any, error) {
	grant, err := crud.QueryString(r, "grant_id", 0, idMax, true)
	if err != nil {
		return nil, err
	}
	var one int
	err = a.Pool.QueryRow(r.Context(), `SELECT 1 FROM flow_grants WHERE id = $1 AND status = 'approved'
		AND revoked_at IS NULL AND now() < expires_at`, *grant).Scan(&one)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	return struct {
		InForce bool `json:"in_force"`
	}{err == nil}, nil
}

type hopOut struct {
	Hop    *string `json:"hop"`
	Action *string `json:"action"`
	Actor  *string `json:"actor"`
	At     *string `json:"at"`
	Reason *string `json:"reason"`
}

type waitingCall struct {
	CallID   string  `json:"call_id"`
	Kind     string  `json:"kind"`
	Fn       string  `json:"fn"`
	Prompt   *string `json:"prompt"`
	Since    *string `json:"since"`
	Hop      *string `json:"hop"`
	HopUntil *string `json:"hop_until"`
	Reason   *string `json:"reason"`
	Proposed bool    `json:"proposed"`
	// False only when the script lets another answerer answer it (#7471).
	PeopleOnly bool     `json:"people_only"`
	Hops       []hopOut `json:"hops"`
}

type runStep struct {
	Seq       int64   `json:"seq"`
	Fn        string  `json:"fn"`
	CallID    string  `json:"call_id"`
	Status    string  `json:"status"`
	SessionID *string `json:"session_id"`
}

// optText is a field that is a string or null: false for anything else.
func optText(m map[string]any, name string) (*string, bool) {
	v := m[name]
	if v == nil {
		return nil, true
	}
	s, ok := v.(string)
	return &s, ok
}

func reqText(m map[string]any, name string) (string, bool) {
	s, ok := m[name].(string)
	return s, ok
}

// waitingOf is a parked call as the agent reads it; false for a malformed one.
func waitingOf(m map[string]any) (waitingCall, bool) {
	var w waitingCall
	good := true
	check := func(ok bool) { good = good && ok }
	var ok bool
	w.CallID, ok = reqText(m, "call_id")
	check(ok)
	w.Kind, ok = reqText(m, "kind")
	check(ok && (w.Kind == "approval" || w.Kind == "answer"))
	w.Fn, ok = reqText(m, "fn")
	check(ok)
	for name, field := range map[string]**string{
		"prompt": &w.Prompt, "since": &w.Since, "hop": &w.Hop, "hop_until": &w.HopUntil, "reason": &w.Reason,
	} {
		*field, ok = optText(m, name)
		check(ok)
	}
	check(w.Hop == nil || *w.Hop == "parent" || *w.Hop == "person")
	w.Proposed = m["proposed"] == true
	w.PeopleOnly = m["people_only"] != false
	w.Hops = []hopOut{}
	for _, h := range objects(m["hops"]) {
		var out hopOut
		for name, field := range map[string]**string{
			"hop": &out.Hop, "action": &out.Action, "actor": &out.Actor, "at": &out.At, "reason": &out.Reason,
		} {
			*field, ok = optText(h, name)
			check(ok)
		}
		w.Hops = append(w.Hops, out)
	}
	return w, good
}

var integral = regexp.MustCompile(`^[+-]?[0-9]+(\.0*)?$`)

// laxInt is an int as pydantic's lax mode reads one from Python.
func laxInt(v any) (int64, bool) {
	switch v := v.(type) {
	case bool:
		if v {
			return 1, true
		}
		return 0, true
	case json.Number:
		if n, err := v.Int64(); err == nil {
			return n, true
		}
		f, err := v.Float64()
		if err != nil || f != math.Trunc(f) {
			return 0, false
		}
		return int64(f), true
	case string:
		s := strings.TrimSpace(v)
		if !integral.MatchString(s) {
			return 0, false
		}
		whole, _, _ := strings.Cut(s, ".")
		n, err := strconv.ParseInt(whole, 10, 64)
		return n, err == nil
	}
	return 0, false
}

func stepOf(m map[string]any) (runStep, bool) {
	var s runStep
	var ok, all bool
	s.Seq, all = laxInt(m["seq"])
	s.Fn, ok = reqText(m, "fn")
	all = all && ok
	s.CallID, ok = reqText(m, "call_id")
	all = all && ok
	s.Status, ok = reqText(m, "status")
	all = all && ok
	s.SessionID, ok = optText(m, "session_id")
	return s, all && ok
}

// kept are the entries that are well formed; a malformed one is logged and left out, so one
// bad entry of a run's projection never fails the whole answer.
func kept[T any](entries []map[string]any, of func(map[string]any) (T, bool), kind, run string) []T {
	out := []T{}
	for _, e := range entries {
		if v, ok := of(e); ok {
			out = append(out, v)
		} else {
			slog.Warn("left out a malformed entry of a flow run", "kind", kind, "run", run)
		}
	}
	return out
}

func waitingCalls(v any, run string) []waitingCall {
	return kept(objects(v), waitingOf, "WaitingCall", run)
}

func steps(v any, run string) []runStep { return kept(objects(v), stepOf, "RunStep", run) }

type runFields struct {
	ID             string        `json:"id"`
	Name           string        `json:"name"`
	Status         string        `json:"status"`
	WorkflowRunID  string        `json:"workflow_run_id"`
	StarterSession *string       `json:"starter_session"`
	WaitingOn      []waitingCall `json:"waiting_on"`
	Steps          []runStep     `json:"steps"`
}

func fieldsOf(id, name, status, execution string, by, waiting, stepsRaw json.RawMessage) runFields {
	return runFields{
		ID: id, Name: name, Status: status, WorkflowRunID: execution,
		StarterSession: text(dict(decode(by))["session"]),
		WaitingOn:      waitingCalls(decode(waiting), id),
		Steps:          steps(decode(stepsRaw), id),
	}
}

type requestedGrant struct {
	ID        string    `json:"id"`
	CreatedAt crud.Time `json:"created_at"`
}

func (a *Reads) runQueue(r *http.Request, p crud.Params) (any, error) {
	id, err := runID(p)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	conn, err := a.Pool.Acquire(ctx)
	if err != nil {
		return nil, err
	}
	defer conn.Release()
	var rid, name, status, execution string
	var by, waiting, stepsRaw json.RawMessage
	err = conn.QueryRow(ctx, `SELECT id, name, status, workflow_run_id, started_by, waiting_on, steps
		FROM workflow_runs WHERE id = $1`, id).Scan(&rid, &name, &status, &execution, &by, &waiting, &stepsRaw)
	if errors.Is(err, pgx.ErrNoRows) {
		return struct {
			Run *struct{} `json:"run"`
		}{}, nil
	}
	if err != nil {
		return nil, err
	}
	type queueRun struct {
		runFields
		RequestedGrant *requestedGrant `json:"requested_grant"`
	}
	out := queueRun{runFields: fieldsOf(rid, name, status, execution, by, waiting, stepsRaw)}
	var grant requestedGrant
	err = conn.QueryRow(ctx, `SELECT id, created_at FROM flow_grants WHERE run_id = $1 AND status = 'requested'`,
		id).Scan(&grant.ID, &grant.CreatedAt)
	switch {
	case err == nil:
		out.RequestedGrant = &grant
	case !errors.Is(err, pgx.ErrNoRows):
		return nil, err
	}
	return struct {
		Run *queueRun `json:"run"`
	}{&out}, nil
}

func (a *Reads) runSteps(r *http.Request, p crud.Params) (any, error) {
	id, err := runID(p)
	if err != nil {
		return nil, err
	}
	var raw json.RawMessage
	err = a.Pool.QueryRow(r.Context(), `SELECT steps FROM workflow_runs WHERE id = $1`, id).Scan(&raw)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	return struct {
		Steps []runStep `json:"steps"`
	}{steps(decode(raw), id)}, nil
}

func (a *Reads) waitingRuns(r *http.Request, _ crud.Params) (any, error) {
	rows, err := a.Pool.Query(r.Context(), `SELECT id, name, status, workflow_run_id, started_by,
		waiting_on, steps, updated_at FROM workflow_runs WHERE status IN ('running', 'waiting')
		AND jsonb_array_length(waiting_on) > 0 ORDER BY updated_at DESC LIMIT $1`, waitingRunsMax)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	type waitingRun struct {
		runFields
		UpdatedAt crud.Time `json:"updated_at"`
	}
	runs := []waitingRun{}
	for rows.Next() {
		var id, name, status, execution string
		var by, waiting, stepsRaw json.RawMessage
		var at time.Time
		if err := rows.Scan(&id, &name, &status, &execution, &by, &waiting, &stepsRaw, &at); err != nil {
			return nil, err
		}
		runs = append(runs, waitingRun{fieldsOf(id, name, status, execution, by, waiting, stepsRaw), crud.Time(at)})
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return struct {
		Runs []waitingRun `json:"runs"`
	}{runs}, nil
}

// queryTime is a datetime query parameter as pydantic reads one; nil when not given.
func queryTime(r *http.Request, name string) (*time.Time, error) {
	values := r.URL.Query()[name]
	if len(values) == 0 {
		return nil, nil
	}
	value := strings.Replace(values[len(values)-1], " ", "T", 1)
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999", "2006-01-02"} {
		if t, err := time.Parse(layout, value); err == nil {
			return &t, nil
		}
	}
	return nil, httpx.Invalid(httpx.Issue{Loc: []string{"query", name}, Msg: "Input should be a valid datetime"})
}

type parentHopRun struct {
	ID            string        `json:"id"`
	WorkflowRunID string        `json:"workflow_run_id"`
	Session       *string       `json:"session"`
	Principal     *string       `json:"principal"`
	Calls         []waitingCall `json:"calls"`
	Steps         []runStep     `json:"steps"`
}

// parentHopApprovals is the waiting runs with a parked approval at hop parent whose hop is
// live, oldest move first, a page after (updated_at, id); each with those calls only.
func (a *Reads) parentHopApprovals(r *http.Request, _ crud.Params) (any, error) {
	afterAt, err := queryTime(r, "after_updated_at")
	issues := crud.Issues(err)
	afterID, err := crud.QueryString(r, "after_id", 0, idMax, false)
	issues = append(issues, crud.Issues(err)...)
	limit, given, err := crud.QueryInt(r, "limit", 1, parentHopPageMax)
	if issues = append(issues, crud.Issues(err)...); len(issues) > 0 {
		return nil, httpx.Invalid(issues...)
	}
	if !given {
		limit = parentHopPageMax
	}
	if (afterAt == nil) != (afterID == nil) {
		return nil, crud.Invalid("after_updated_at and after_id come together")
	}
	rows, err := a.Pool.Query(r.Context(), `SELECT id, workflow_run_id, started_by, steps, updated_at,
		(SELECT coalesce(jsonb_agg(w), '[]'::jsonb) FROM jsonb_array_elements(r.waiting_on) w
		  WHERE `+parentApprovalLive+`) AS calls
		FROM workflow_runs r
		WHERE status = 'waiting' AND waiting_on @> '[{"hop": "parent"}]'::jsonb
		AND EXISTS (SELECT 1 FROM jsonb_array_elements(r.waiting_on) w
		  WHERE `+parentApprovalLive+`)
		AND ($1::timestamptz IS NULL OR (updated_at, id) > ($1, $2))
		ORDER BY updated_at, id LIMIT $3`, afterAt, afterID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	runs := []parentHopRun{}
	var lastAt time.Time
	for rows.Next() {
		var id, execution string
		var by, stepsRaw, calls json.RawMessage
		if err := rows.Scan(&id, &execution, &by, &stepsRaw, &lastAt, &calls); err != nil {
			return nil, err
		}
		m := dict(decode(by))
		runs = append(runs, parentHopRun{
			ID: id, WorkflowRunID: execution, Session: text(m["session"]), Principal: text(m["principal"]),
			Calls: waitingCalls(decode(calls), id), Steps: steps(decode(stepsRaw), id),
		})
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	type cursor struct {
		AfterUpdatedAt crud.Time `json:"after_updated_at"`
		AfterID        string    `json:"after_id"`
	}
	out := struct {
		Runs []parentHopRun `json:"runs"`
		Next *cursor        `json:"next"`
	}{Runs: runs}
	if int64(len(runs)) == limit {
		out.Next = &cursor{crud.Time(lastAt), runs[len(runs)-1].ID}
	}
	return out, nil
}

// stillWaiting is which of the request ids still wait at hop parent of a waiting run, in
// the order asked.
func (a *Reads) stillWaiting(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "request_ids")
	if err != nil {
		return nil, err
	}
	ids := []string{}
	for _, v := range body.Get("request_ids").List(0, stillWaitingMax) {
		ids = append(ids, v.String(0, requestIDMax, nil))
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	waiting := []string{}
	if len(ids) > 0 {
		rows, err := a.Pool.Query(r.Context(), `SELECT x.id FROM unnest($1::text[]) WITH ORDINALITY x(id, n)
			WHERE EXISTS (
			SELECT 1 FROM workflow_runs r CROSS JOIN LATERAL jsonb_array_elements(r.waiting_on) w
			WHERE r.id = split_part(x.id, ':', 2) AND r.status = 'waiting'
			AND w->>'hop' = 'parent'
			AND x.id = 'flow:' || r.id || ':' || r.workflow_run_id || ':' || (w->>'call_id'))
			ORDER BY x.n`, ids)
		if err != nil {
			return nil, err
		}
		found, err := pgx.CollectRows(rows, pgx.RowTo[string])
		if err != nil {
			return nil, err
		}
		if found != nil {
			waiting = found
		}
	}
	return struct {
		Waiting []string `json:"waiting"`
	}{waiting}, nil
}
