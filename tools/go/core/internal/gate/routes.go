package gate

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/flowgrants"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

const (
	// RequestIDMax is the longest request id (the agent's gate/crudStore.ts REQUEST_ID_MAX).
	RequestIDMax = 2048
	// NameMax is the longest name, id or hash a write carries.
	NameMax = 1024
	// TextMax is the longest summary, prompt or reason.
	TextMax = 64 * 1024
	// CardJSONMax is the largest JSON of a question card, an attention request or a record.
	CardJSONMax = 1024 * 1024
	// responseJSONMax is twice the validator's cap, so one past the cap reaches the validator.
	responseJSONMax = 2 * ResponseMax
)

// SessionPattern is a session id: a UUID, any case.
var SessionPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

var hashPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

// Outcomes are a resolution's outcomes.
var Outcomes = []string{"approved", "denied", "expired", "answered", "cancelled", "timed_out"}

// Gate is the area's routes.
type Gate struct{ Pool *pgxpool.Pool }

// Routes are the area's routes.
func (g *Gate) Routes() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/gate/open", Handle: g.open},
		{Method: "POST", Path: "/gate/grant", Handle: g.grant},
		{Method: "POST", Path: "/gate/deny", Handle: g.deny},
		{Method: "POST", Path: "/gate/resolve", Handle: g.resolve},
		{Method: "POST", Path: "/gate/decide", Handle: g.decide},
		{Method: "GET", Path: "/gate/response", Handle: g.read},
	}
}

// Principal is the required object name, {kind, id, label}, each a name.
func Principal(b *crud.Body, name string) jsonv.Object {
	n := b.Need(name, "kind", "id", "label")
	if n == nil {
		return nil
	}
	defer n.Done()
	return jsonv.Object{M("kind", n.Text("kind", 0, NameMax)), M("id", n.Text("id", 0, NameMax)),
		M("label", n.Text("label", 0, NameMax))}
}

// Nullable is the string name, required, but null is allowed (nil).
func Nullable(b *crud.Body, name string, most int) *string {
	if !b.Present(name) {
		b.Text(name, 0, most)
		return nil
	}
	return b.OptText(name, 0, most, nil)
}

// Value is name's JSON value, nil when absent, and the 422 of one that is no JSON.
func Value(b *crud.Body, name string) any {
	raw, ok := b.Raw(name)
	if !ok {
		return nil
	}
	v, err := jsonv.Parse(raw)
	if err != nil {
		b.FailAt([]string{name}, "Input should be valid JSON")
	}
	return v
}

// Card is name's optional list (list) or object, at most CardJSONMax characters of JSON.
func Card(b *crud.Body, name string, list bool) any {
	v := Value(b, name)
	switch v.(type) {
	case nil:
		return nil
	case []any:
		if !list {
			b.FailAt([]string{name}, "Input should be a valid dictionary")
			return nil
		}
	case jsonv.Object:
		if list {
			b.FailAt([]string{name}, "Input should be a valid list")
			return nil
		}
	default:
		if list {
			b.FailAt([]string{name}, "Input should be a valid list")
		} else {
			b.FailAt([]string{name}, "Input should be a valid dictionary")
		}
		return nil
	}
	if jsonv.ASCIILen(v) > CardJSONMax {
		b.FailAt([]string{name}, fmt.Sprintf("Value error, a question card or an entry's record is larger than %d characters of JSON", CardJSONMax))
	}
	return v
}

func boundedResponse(b *crud.Body, name string) any {
	v := Value(b, name)
	if v != nil && ResponseBytes(v) > responseJSONMax {
		b.FailAt([]string{name}, fmt.Sprintf("Value error, a response is larger than %d bytes", responseJSONMax))
	}
	return v
}

func optHash(b *crud.Body, name string) *string {
	h := b.OptText(name, 0, 1<<20, nil)
	if h != nil {
		b.Pattern(name, *h, hashPattern)
	}
	return h
}

func sessionID(b *crud.Body, name string) string {
	s := b.Text(name, 0, 1<<20)
	if b.Present(name) {
		b.Pattern(name, s, SessionPattern)
	}
	return s
}

func optSessionID(b *crud.Body, name string) *string {
	s := b.OptText(name, 0, 1<<20, nil)
	if s != nil {
		b.Pattern(name, *s, SessionPattern)
	}
	return s
}

type opened struct {
	Opened bool    `json:"opened"`
	Logged Batches `json:"logged"`
}

func (g *Gate) open(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "request_id", "session_id", "workflow_id", "workflow_run_id",
		"kind", "tool", "tool_use_id", "summary", "input_hash", "prompt", "requested_by",
		"responders", "expires_at", "questions", "attention", "flow_run_id", "hop", "hop_until",
		"parent_session", "parent_principal", "outside_reason", "gate")
	if err != nil {
		return nil, err
	}
	rid := b.Text("request_id", 1, RequestIDMax)
	sid := sessionID(b, "session_id")
	wid := b.Text("workflow_id", 0, NameMax)
	run := b.Text("workflow_run_id", 0, NameMax)
	kind := b.Choice("kind", "approval", "answer")
	tool := b.Text("tool", 0, NameMax)
	toolUse := b.Text("tool_use_id", 0, NameMax)
	summary := b.Text("summary", 0, TextMax)
	hash := Nullable(b, "input_hash", NameMax)
	prompt := b.Text("prompt", 0, TextMax)
	by := Principal(b, "requested_by")
	responders := b.ChoiceItems("responders", b.List("responders", false, 2), "browser", "grant")
	expires := b.Datetime("expires_at", false)
	questions := Card(b, "questions", true)
	attention := Card(b, "attention", false)
	flow := b.OptText("flow_run_id", 0, NameMax, nil)
	hop := b.OptChoice("hop", "parent", "person")
	hopUntil := b.Datetime("hop_until", true)
	parent := optSessionID(b, "parent_session")
	principal := b.OptText("parent_principal", 0, NameMax, nil)
	outside := b.OptText("outside_reason", 0, TextMax, nil)
	record := Card(b, "gate", false)
	if err := b.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	conn, err := g.Pool.Acquire(ctx)
	if err != nil {
		return nil, err
	}
	defer conn.Release()
	var logged Batches
	err = pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
		// The session first, as every resolution takes it (LockSessionOf).
		if _, err := tx.Exec(ctx, `SELECT 1 FROM ai_sessions WHERE id = $1::text::uuid FOR UPDATE`, sid); err != nil {
			return err
		}
		rows, err := tx.Query(ctx, `DELETE FROM ai_pending_input WHERE session_id = $1::text::uuid
			AND workflow_run_id <> $2 RETURNING `+RowColumns, sid, run)
		if err != nil {
			return err
		}
		stale, err := ScanRows(rows, false)
		if err != nil {
			return err
		}
		for _, old := range stale {
			reason := "reset"
			if err := Resolved(ctx, tx, &logged, old, "cancelled", System(), nil, &reason, nil); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	done := false
	err = pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
		hops := []any{}
		if hop != nil {
			hops = append(hops, Hop(*hop, "parked", "flow", outside))
		}
		rows, err := tx.Query(ctx,
			`INSERT INTO ai_pending_input (request_id, session_id, workflow_id, workflow_run_id,
			 kind, tool, summary, input_hash, prompt, requested_by, responders, attention,
			 expires_at, flow_run_id, hop, hop_until, parent_session, parent_principal,
			 outside_reason, hops, gate)
			 SELECT $1, $2::text::uuid, $3, $4, $5, $6, $7, $8, $9, $10::text::jsonb, $11::text[],
			 $12::text::jsonb, $13::text::timestamptz, $14, $15, $16::text::timestamptz,
			 $17::text::uuid, $18, $19, $20::text::jsonb, $21::text::jsonb
			 WHERE NOT EXISTS (SELECT 1 FROM ai_input_responses WHERE request_id = $1)
			 ON CONFLICT (request_id) DO NOTHING RETURNING `+RowColumns,
			rid, sid, wid, run, kind, tool, summary, hash, prompt, jsonv.Dumps(by), responders,
			dumped(attention), *expires, flow, hop, hopUntil, parent, principal, outside,
			jsonv.Dumps(hops), dumped(record))
		if err != nil {
			return err
		}
		found, err := ScanRows(rows, false)
		if err != nil || len(found) == 0 {
			return err
		}
		row := found[0]
		done = true
		var evs []jsonv.Object
		if kind == "approval" {
			evs = append(evs, Event(M("type", "approval.required"), M("sessionId", sid),
				M("id", rid), M("tool", toolUse),
				M("summary", Cap(tool+" "+summary, ApprovalSummaryMax)), M("risk", "outward")))
		} else {
			if questions == nil {
				questions = []any{}
			}
			asked := Event(M("type", "question.asked"), M("sessionId", sid), M("id", rid),
				M("tool", toolUse), M("questions", questions))
			if a, ok := attention.(jsonv.Object); ok {
				reason, _ := a.Get("reason")
				onTimeout, _ := a.Get("on_timeout")
				asked = append(asked, M("attention", jsonv.Object{M("reason", reason),
					M("onTimeout", onTimeout), M("expiresAt", jsonv.ISO(row.ExpiresAt))}))
			}
			evs = append(evs, asked)
		}
		evs = append(evs, Event(M("type", "input.requested"), M("sessionId", sid), M("entry", entryOf(row))))
		status := "waiting_input"
		if kind == "approval" {
			status = "waiting_approval"
		}
		tag, err := tx.Exec(ctx, `UPDATE ai_sessions SET status = $1, updated_at = now()
			WHERE id = $2::text::uuid AND status <> $1`, status, sid)
		if err != nil {
			return err
		}
		if tag.RowsAffected() > 0 {
			evs = append(evs, Event(M("type", "session.status"), M("sessionId", sid), M("status", status)))
		}
		return Append(ctx, tx, &logged, sid, evs)
	})
	if err != nil {
		return nil, err
	}
	return opened{done, logged}, nil
}

func dumped(v any) *string {
	if v == nil {
		return nil
	}
	s := jsonv.Dumps(v)
	return &s
}

// RunGrantResponder is a run grant as a responder.
func RunGrantResponder(grant string) jsonv.Object {
	return jsonv.Object{M("kind", "run_grant"), M("id", grant), M("label", "Run grant")}
}

func flowResponder(run string) jsonv.Object {
	return jsonv.Object{M("kind", "flow"), M("id", run), M("label", "Flow")}
}

// stand is the audit's view of a call no entry was opened for: the grant and deny paths'.
func stand(rid, sid string, by jsonv.Object, tool, hash, summary string) *Row {
	return &Row{RequestID: rid, SessionID: sid, RequestedBy: by, Tool: tool, InputHash: &hash,
		Summary: summary, CreatedAt: time.Now()}
}

func (g *Gate) grant(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "request_id", "session_id", "tool", "summary", "input_hash",
		"grant_id", "requested_by")
	if err != nil {
		return nil, err
	}
	rid := b.Text("request_id", 1, RequestIDMax)
	sid := sessionID(b, "session_id")
	tool := b.Text("tool", 0, NameMax)
	summary := b.Text("summary", 0, TextMax)
	hash := b.Text("input_hash", 0, NameMax)
	grant := b.Text("grant_id", 0, NameMax)
	by := Principal(b, "requested_by")
	if err := b.Err(); err != nil {
		return nil, err
	}
	responder := RunGrantResponder(grant)
	granted := false
	err = pgx.BeginFunc(r.Context(), g.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		var one int
		err := tx.QueryRow(ctx, `SELECT 1 FROM flow_grants g JOIN flow_grant_uses u ON u.grant_id = g.id
			WHERE g.id = $1 AND u.request_id = $2 AND u.session_id = $3 AND u.input_hash = $4
			AND g.status = 'approved' AND g.revoked_at IS NULL AND now() < g.expires_at
			FOR SHARE OF g`, grant, rid, sid, hash).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		// Inherited, or seeded from a standing grant: only while its whole chain is in
		// force, as the API's check of the call requires (#5685).
		if held, err := flowgrants.ChainInForce(ctx, tx, grant); err != nil || !held {
			return err
		}
		tag, err := tx.Exec(ctx, `INSERT INTO ai_input_responses (request_id, session_id, kind,
			outcome, responder) VALUES ($1, $2::text::uuid, 'approval', 'approved', $3::text::jsonb)
			ON CONFLICT (request_id) DO NOTHING`, rid, sid, jsonv.Dumps(responder))
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			// A retried activity: the same grant's approval is there already.
			err := tx.QueryRow(ctx, `SELECT outcome = 'approved' AND responder = $2::text::jsonb
				FROM ai_input_responses WHERE request_id = $1`, rid, jsonv.Dumps(responder)).Scan(&granted)
			if errors.Is(err, pgx.ErrNoRows) {
				return nil
			}
			return err
		}
		granted = true
		return Audit(ctx, tx, stand(rid, sid, by, tool, hash, summary), "approved", responder, nil)
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Granted bool `json:"granted"`
	}{granted}, nil
}

func (g *Gate) deny(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "request_id", "session_id", "run_id", "tool", "summary",
		"input_hash", "reason", "requested_by")
	if err != nil {
		return nil, err
	}
	rid := b.Text("request_id", 1, RequestIDMax)
	sid := sessionID(b, "session_id")
	run := b.Text("run_id", 0, NameMax)
	tool := b.Text("tool", 0, NameMax)
	summary := b.Text("summary", 0, TextMax)
	hash := b.Text("input_hash", 0, NameMax)
	reason := Nullable(b, "reason", TextMax)
	by := Principal(b, "requested_by")
	if err := b.Err(); err != nil {
		return nil, err
	}
	responder := flowResponder(run)
	denied := false
	err = pgx.BeginFunc(r.Context(), g.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		tag, err := tx.Exec(ctx, `INSERT INTO ai_input_responses
			(request_id, session_id, kind, outcome, responder, reason, hops)
			VALUES ($1, $2::text::uuid, 'approval', 'denied', $3::text::jsonb, $4, $5::text::jsonb)
			ON CONFLICT (request_id) DO NOTHING`, rid, sid, jsonv.Dumps(responder), reason,
			jsonv.Dumps([]any{Hop("person", "deny", "flow", reason)}))
		if err != nil || tag.RowsAffected() == 0 {
			return err
		}
		denied = true
		return Audit(ctx, tx, stand(rid, sid, by, tool, hash, summary), "denied", responder, reason)
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Denied bool `json:"denied"`
	}{denied}, nil
}

type resolved struct {
	Resolved bool    `json:"resolved"`
	Logged   Batches `json:"logged"`
}

func (g *Gate) resolve(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "request_id", "outcome", "responder", "response",
		"response_hash", "reason")
	if err != nil {
		return nil, err
	}
	rid := b.Text("request_id", 1, RequestIDMax)
	outcome := b.Choice("outcome", Outcomes...)
	responder := Principal(b, "responder")
	response := boundedResponse(b, "response")
	digest := optHash(b, "response_hash")
	reason := b.OptText("reason", 0, TextMax, nil)
	if err := b.Err(); err != nil {
		return nil, err
	}
	var logged Batches
	done := false
	err = pgx.BeginFunc(r.Context(), g.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		if err := LockSessionOf(ctx, tx, rid); err != nil {
			return err
		}
		rows, err := tx.Query(ctx, `DELETE FROM ai_pending_input WHERE request_id = $1 RETURNING `+RowColumns, rid)
		if err != nil {
			return err
		}
		found, err := ScanRows(rows, false)
		if err != nil || len(found) == 0 {
			return err
		}
		done = true
		return Resolved(ctx, tx, &logged, found[0], outcome, responder, response, reason, digest)
	})
	if err != nil {
		return nil, err
	}
	if !done {
		logged = nil
	}
	return resolved{done, logged}, nil
}

func who(v any) *principal {
	o, ok := v.(jsonv.Object)
	if !ok {
		return nil
	}
	kind, _ := o.Get("kind")
	id, _ := o.Get("id")
	return &principal{Str(kind), Str(id)}
}

// gateEntry is the validator's entry of a row and its record (the agent's GateRecord), in
// state resolving once the row is past its timer.
func gateEntry(row *Row, record jsonv.Object) *entry {
	e := &entry{id: row.RequestID, kind: row.Kind, state: "pending"}
	if row.Expired {
		e.state = "resolving"
	}
	if h, _ := record.Get("inputHash"); h != nil {
		s := Str(h)
		e.inputHash = &s
	}
	by, _ := record.Get("requestedBy")
	owner, _ := record.Get("sessionOwner")
	creator, _ := record.Get("sessionCreator")
	e.requestedBy, e.sessionOwner, e.sessionCreator = who(by), who(owner), who(creator)
	if qs, _ := record.Get("questions"); qs != nil {
		e.hasQuestions = true
		for _, q := range list(qs) {
			o, _ := q.(jsonv.Object)
			text, _ := o.Get("question")
			multi, _ := o.Get("multiSelect")
			e.questions = append(e.questions, question{Str(text), Truthy(multi)})
		}
	}
	if opts, _ := record.Get("options"); opts != nil {
		e.hasOptions = true
		e.options = []string{}
		for _, o := range list(opts) {
			e.options = append(e.options, Str(o))
		}
	}
	inRun, _ := record.Get("inRun")
	e.inRun = Truthy(inRun)
	return e
}

type decidedAs struct {
	Kind    string `json:"kind"`
	Outcome string `json:"outcome"`
}

type liveCheck struct {
	WorkflowID string `json:"workflow_id"`
	RunID      string `json:"run_id"`
}

type decideAnswer struct {
	Refused   *Refused   `json:"refused"`
	Workflow  bool       `json:"workflow"`
	LiveCheck *liveCheck `json:"live_check"`
	Decided   *decidedAs `json:"decided"`
	Logged    Batches    `json:"logged"`
}

func (g *Gate) decide(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "request_id", "response", "responder", "role", "check_live",
		"live_run", "response_hash")
	if err != nil {
		return nil, err
	}
	rid := b.Text("request_id", 1, RequestIDMax)
	if !b.Present("response") {
		b.Text("response", 0, 0)
	}
	response := boundedResponse(b, "response")
	responder := Principal(b, "responder")
	role := b.Choice("role", "browser", "grant", "owner", "parent", "run_grant")
	checkLive := b.Bool("check_live", false)
	liveRun := b.OptText("live_run", 0, NameMax, nil)
	digest := optHash(b, "response_hash")
	if err := b.Err(); err != nil {
		return nil, err
	}
	var answer decideAnswer
	err = pgx.BeginFunc(r.Context(), g.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		answer, err = decideIn(ctx, tx, rid, response, responder, role, liveRun, checkLive, digest)
		return err
	})
	var refused *Refused
	if errors.As(err, &refused) {
		return decideAnswer{Refused: refused}, nil
	}
	if err != nil {
		return nil, err
	}
	return answer, nil
}

func decideIn(ctx context.Context, tx pgx.Tx, rid string, response any, responder jsonv.Object,
	role string, liveRun *string, checkLive bool, digest *string) (decideAnswer, error) {
	if err := LockSessionOf(ctx, tx, rid); err != nil {
		return decideAnswer{}, err
	}
	rows, err := tx.Query(ctx, `SELECT `+RowColumns+`, expires_at <= now() AS expired
		FROM ai_pending_input WHERE request_id = $1 FOR UPDATE`, rid)
	if err != nil {
		return decideAnswer{}, err
	}
	found, err := ScanRows(rows, true)
	if err != nil {
		return decideAnswer{}, err
	}
	if len(found) == 0 {
		return decideAnswer{}, &Refused{"stale", fmt.Sprintf("no pending input %s: it is stale or was never asked", rid)}
	}
	row := found[0]
	record, ok := row.Gate.(jsonv.Object)
	if !ok {
		return decideAnswer{Workflow: true}, nil
	}
	kind, _ := responder.Get("kind")
	id, _ := responder.Get("id")
	v, err := validate(gateEntry(row, record), rid, response, principal{Str(kind), Str(id)}, role)
	if err != nil {
		return decideAnswer{}, err
	}
	if checkLive && (liveRun == nil || row.WorkflowRunID != *liveRun) {
		return decideAnswer{LiveCheck: &liveCheck{row.WorkflowID, row.WorkflowRunID}}, nil
	}
	outcome, recorded := "answered", jsonv.Object{}
	if v.decision != "" {
		outcome = "denied"
		if v.decision == "approve" {
			outcome = "approved"
		}
		recorded = append(recorded, M("decision", v.decision))
	} else {
		answers := make([]any, len(v.answers))
		for i, a := range v.answers {
			answers[i] = a
		}
		recorded = append(recorded, M("answers", answers))
	}
	if _, err := tx.Exec(ctx, `DELETE FROM ai_pending_input WHERE request_id = $1`, rid); err != nil {
		return decideAnswer{}, err
	}
	if digest == nil {
		h, err := ResponseHash(response)
		if err != nil {
			return decideAnswer{}, err
		}
		digest = &h
	}
	var logged Batches
	if err := Resolved(ctx, tx, &logged, row, outcome, responder, recorded, nil, digest); err != nil {
		return decideAnswer{}, err
	}
	return decideAnswer{Decided: &decidedAs{row.Kind, outcome}, Logged: logged}, nil
}

func (g *Gate) read(r *http.Request, _ crud.Params) (any, error) {
	rid, err := crud.QueryText(r, "request_id", RequestIDMax)
	if err != nil {
		return nil, err
	}
	var outcome string
	var response *string
	var responder string
	var reason *string
	err = g.Pool.QueryRow(r.Context(), `SELECT outcome, response::text, responder::text, reason
		FROM ai_input_responses WHERE request_id = $1`, rid).Scan(&outcome, &response, &responder, &reason)
	type record struct {
		Outcome   string  `json:"outcome"`
		Response  any     `json:"response"`
		Responder any     `json:"responder"`
		Reason    *string `json:"reason"`
	}
	out := struct {
		Record *record `json:"record"`
	}{}
	if errors.Is(err, pgx.ErrNoRows) {
		return out, nil
	}
	if err != nil {
		return nil, err
	}
	out.Record = &record{outcome, parsed(response), jsonv.MustParse(responder), reason}
	return out, nil
}
