// Package questions is the agent's questions and done summaries:
// backend/scadbuddy/agent_data/questions.py and api/crud_agent_questions.py, statement for
// statement (area questions). Each write is one transaction with its session events and its
// audit rows; a done post writes only while the turn's placement holds the session, and the
// attention count is read only while its caller holds the lease attention-rate.
package questions

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
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
	pendingCap           = 500
	donePostsPerTurn     = 3
	attentionRateWindowS = 600
	attentionLease       = "attention-rate"
	waitForUser          = "wait_for_user"
	attentionTool        = "mcp__scadbuddy_questions__request_user_attention"
	replaced             = "replaced by a newer request for the same reason"
	answersMax           = 16
	leaseLost            = "lease-lost"
	// done is the agent's isDoneSummary.
	done = "(q.attention_reason IS NOT DISTINCT FROM 'done' AND q.expires_at IS NULL)"
)

// Questions is the area's routes.
type Questions struct{ Pool *pgxpool.Pool }

// Routes are the area's routes.
func (q *Questions) Routes() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/questions/entry", Handle: q.entry},
		{Method: "POST", Path: "/questions/pending", Handle: q.pending},
		{Method: "GET", Path: "/questions/gate-answer", Handle: q.gateAnswer},
		{Method: "POST", Path: "/questions/answer", Handle: q.answer},
		{Method: "POST", Path: "/questions/attention-count", Handle: q.attentionCount},
		{Method: "POST", Path: "/questions/done-facts", Handle: q.doneFacts},
		{Method: "POST", Path: "/questions/done", Handle: q.done},
	}
}

func parsed(text *string) any {
	if text == nil {
		return nil
	}
	return jsonv.MustParse(*text)
}

func (q *Questions) entry(r *http.Request, _ crud.Params) (any, error) {
	id, err := crud.QueryText(r, "id", gate.NameMax)
	if err != nil {
		return nil, err
	}
	type entry struct {
		SessionID string  `json:"session_id"`
		Kind      string  `json:"kind"`
		Questions any     `json:"questions"`
		Outcome   *string `json:"outcome"`
		Reason    *string `json:"reason"`
		Tool      string  `json:"tool"`
	}
	out := struct {
		Entry *entry `json:"entry"`
	}{}
	if !audit.IsUUID(id) {
		return out, nil
	}
	e := &entry{}
	var questions string
	err = q.Pool.QueryRow(r.Context(),
		`SELECT session_id::text AS session_id, kind, questions::text, outcome, reason, tool
		 FROM ai_questions WHERE id = $1::text::uuid`, id).Scan(
		&e.SessionID, &e.Kind, &questions, &e.Outcome, &e.Reason, &e.Tool)
	if errors.Is(err, pgx.ErrNoRows) {
		return out, nil
	}
	if err != nil {
		return nil, err
	}
	e.Questions = jsonv.MustParse(questions)
	out.Entry = e
	return out, nil
}

type summary struct {
	ID              string      `json:"id"`
	SessionID       string      `json:"session_id"`
	Kind            string      `json:"kind"`
	Tool            string      `json:"tool"`
	ToolUseID       string      `json:"tool_use_id"`
	Questions       any         `json:"questions"`
	AttentionReason *string     `json:"attention_reason"`
	OnTimeout       *string     `json:"on_timeout"`
	Summary         *string     `json:"summary"`
	CreatedAt       jsonv.Time  `json:"created_at"`
	ExpiresAt       *jsonv.Time `json:"expires_at"`
}

func (q *Questions) pending(r *http.Request, _ crud.Params) (any, error) {
	var session *string
	if values := r.URL.Query()["session_id"]; len(values) > 0 {
		s := values[len(values)-1]
		if !gate.SessionPattern.MatchString(s) {
			return nil, httpx.Invalid(httpx.Issue{Loc: []string{"query", "session_id"},
				Msg: fmt.Sprintf("String should match pattern '%s'", gate.SessionPattern)})
		}
		session = &s
	}
	b, err := crud.ReadBody(r, "kind", "id")
	if err != nil {
		return nil, err
	}
	kind, id := b.Text("kind", 0, gate.NameMax), b.Text("id", 0, gate.NameMax)
	if err := b.Err(); err != nil {
		return nil, err
	}
	rows, err := q.Pool.Query(r.Context(),
		`SELECT q.id::text AS id, q.session_id::text AS session_id, q.kind, q.tool, q.tool_use_id,
		 q.questions::text, q.attention_reason, q.on_timeout, q.summary, q.created_at, q.expires_at
		 FROM ai_questions q JOIN ai_sessions s ON s.id = q.session_id
		 WHERE s.owner_kind = $1 AND (s.owner_id = $2 OR s.owner_kind = 'browser')
		 AND ($3::text::uuid IS NULL OR q.session_id = $3::text::uuid)
		 AND q.outcome IS NULL AND `+done+`
		 ORDER BY q.created_at DESC, q.id DESC LIMIT $4`, kind, id, session, pendingCap+1)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []summary{}
	for rows.Next() {
		var s summary
		var questions string
		if err := rows.Scan(&s.ID, &s.SessionID, &s.Kind, &s.Tool, &s.ToolUseID, &questions,
			&s.AttentionReason, &s.OnTimeout, &s.Summary, &s.CreatedAt, &s.ExpiresAt); err != nil {
			return nil, err
		}
		s.Questions = jsonv.MustParse(questions)
		out = append(out, s)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return struct {
		Questions []summary `json:"questions"`
	}{out}, nil
}

func (q *Questions) gateAnswer(r *http.Request, _ crud.Params) (any, error) {
	rid, err := crud.QueryText(r, "request_id", gate.RequestIDMax)
	if err != nil {
		return nil, err
	}
	type answer struct {
		SessionID   string      `json:"session_id"`
		Tool        *string     `json:"tool"`
		RequestedAt *jsonv.Time `json:"requested_at"`
		Response    any         `json:"response"`
	}
	out := struct {
		Answer *answer `json:"answer"`
	}{}
	a := &answer{}
	var response *string
	err = q.Pool.QueryRow(r.Context(),
		`SELECT session_id::text AS session_id, tool, requested_at, response::text
		 FROM ai_input_responses WHERE request_id = $1 AND kind = 'answer'
		 AND outcome = 'answered'`, rid).Scan(&a.SessionID, &a.Tool, &a.RequestedAt, &response)
	if errors.Is(err, pgx.ErrNoRows) {
		return out, nil
	}
	if err != nil {
		return nil, err
	}
	a.Response = parsed(response)
	out.Answer = a
	return out, nil
}

// pyStrip is Python's str.strip(): what str.isspace() names.
func pyStrip(s string) string {
	return strings.Trim(s, "\t\n\v\f\r\u001c\u001d\u001e\u001f \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000")
}

type questionAudit struct {
	action, surface, outcome, sessionID, detail string
	actor                                       jsonv.Object
	startedAt                                   any
	turnID, toolUseID, inputHash, clientIP      *string
}

func (a questionAudit) insert(ctx context.Context, tx pgx.Tx) error {
	get := func(key string) string {
		v, _ := a.actor.Get(key)
		return gate.Str(v)
	}
	return audit.Insert(ctx, tx, audit.Entry{
		Kind: "question", Action: a.action, Surface: a.surface,
		ActorKind: audit.Cap(get("kind"), 50), ActorID: audit.Cap(get("id"), 200),
		ActorLabel: audit.Cap(get("label"), 200), Outcome: a.outcome, ClientIP: a.clientIP,
		SessionID: &a.sessionID, TurnID: a.turnID, ToolUseID: a.toolUseID,
		Tier: audit.Ptr("read"), InputHash: a.inputHash,
		Detail:    audit.Ptr(audit.Cap(a.detail, 500)),
		StartedAt: a.startedAt, FinishedAt: time.Now(),
	})
}

func refused(code, msg string) error { return httpx.WithReason(http.StatusConflict, msg, code) }

func plural(n int) string {
	if n == 1 {
		return fmt.Sprintf("%d answer", n)
	}
	return fmt.Sprintf("%d answers", n)
}

func (q *Questions) answer(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "id", "session_id", "answers", "by", "input_hash", "surface",
		"client_ip")
	if err != nil {
		return nil, err
	}
	id := b.Text("id", 0, gate.NameMax)
	sid := b.Text("session_id", 0, gate.NameMax)
	answers := b.TextItems("answers", b.List("answers", false, answersMax), 0, gate.TextMax)
	by := gate.Principal(b, "by")
	hash := b.OptText("input_hash", 0, gate.NameMax)
	http := "http"
	surface := b.Enum("surface", &http, audit.Surfaces...)
	ip := b.OptText("client_ip", 0, 200)
	if err := b.Err(); err != nil {
		return nil, err
	}
	if !audit.IsUUID(id) || !audit.IsUUID(sid) {
		return nil, refused("not_found", "no question "+id+" in this session")
	}
	var logged gate.Batches
	err = pgx.BeginFunc(r.Context(), q.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		if _, err := tx.Exec(ctx, `SELECT 1 FROM ai_sessions WHERE id = $1::text::uuid FOR UPDATE`, sid); err != nil {
			return err
		}
		var questions string
		var outcome *string
		err := tx.QueryRow(ctx, `SELECT questions::text, outcome FROM ai_questions
			WHERE id = $1::text::uuid AND session_id = $2::text::uuid`, id, sid).Scan(&questions, &outcome)
		if errors.Is(err, pgx.ErrNoRows) {
			return refused("not_found", "no question "+id+" in this session")
		}
		if err != nil {
			return err
		}
		if outcome != nil {
			return refused("conflict", "question "+id+" is no longer waiting for an answer")
		}
		asked, _ := jsonv.MustParse(questions).([]any)
		blank := false
		for _, a := range answers {
			blank = blank || pyStrip(a) == ""
		}
		if len(answers) != len(asked) || blank {
			return refused("invalid", fmt.Sprintf("question %s needs one answer for each of its"+
				" %d questions", id, len(asked)))
		}
		var turn, tool, toolUse string
		var created time.Time
		err = tx.QueryRow(ctx,
			`UPDATE ai_questions SET outcome = 'answered', answers = $1::text::jsonb,
			 resolved_at = now(), answered_by_kind = $2, answered_by_id = $3, answered_by_label = $4
			 WHERE id = $5::text::uuid AND session_id = $6::text::uuid AND outcome IS NULL
			 RETURNING turn_id::text AS turn_id, tool, tool_use_id, created_at`,
			jsonv.Dumps(answers), get(by, "kind"), get(by, "id"), get(by, "label"), id, sid).Scan(
			&turn, &tool, &toolUse, &created)
		if errors.Is(err, pgx.ErrNoRows) {
			return refused("conflict", "question "+id+" is no longer waiting for an answer")
		}
		if err != nil {
			return err
		}
		evs := []jsonv.Object{
			gate.Event(gate.M("type", "question.resolved"), gate.M("sessionId", sid), gate.M("id", id),
				gate.M("answered", true), gate.M("answers", answers), gate.M("by", by)),
			gate.Event(gate.M("type", "input.resolved"), gate.M("sessionId", sid),
				gate.M("id", "question:"+id), gate.M("kind", "answer"), gate.M("outcome", "answered")),
		}
		if err := gate.Append(ctx, tx, &logged, sid, evs); err != nil {
			return err
		}
		return questionAudit{
			action: "answered", surface: surface, actor: by, outcome: "ok", sessionID: sid,
			turnID: &turn, toolUseID: &toolUse, inputHash: hash, clientIP: ip,
			detail:    fmt.Sprintf("%s question %s: %s", tool, id, plural(len(answers))),
			startedAt: created,
		}.insert(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Logged gate.Batches `json:"logged"`
	}{logged}, nil
}

func get(o jsonv.Object, key string) string {
	v, _ := o.Text(key)
	return v
}

func (q *Questions) attentionCount(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "own", "lease_token")
	if err != nil {
		return nil, err
	}
	own := b.Bool("own", nil)
	token := b.Text("lease_token", 1, gate.NameMax)
	if err := b.Err(); err != nil {
		return nil, err
	}
	lost := httpx.WithReason(http.StatusConflict, "The lease is not held by this token.", leaseLost)
	if !audit.IsUUID(token) {
		return nil, lost
	}
	var n int64
	err = pgx.BeginFunc(r.Context(), q.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		var one int
		err := tx.QueryRow(ctx, `SELECT 1 FROM agent_leases WHERE key = $1 AND token = $2::text::uuid
			AND expires_at > now() FOR SHARE`, attentionLease, token).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return lost
		}
		if err != nil {
			return err
		}
		return tx.QueryRow(ctx,
			`SELECT (SELECT count(*) FROM ai_pending_input WHERE tool = $1
			 AND created_at > now() - make_interval(secs => $2)
			 AND (request_id LIKE $3) <> $4)
			 + (SELECT count(*) FROM ai_input_responses WHERE tool = $1
			 AND requested_at > now() - make_interval(secs => $2)
			 AND (request_id LIKE $3) <> $4) AS n`,
			waitForUser, attentionRateWindowS, "%:tab:%", own).Scan(&n)
	})
	if err != nil {
		return nil, err
	}
	return struct {
		N int64 `json:"n"`
	}{n}, nil
}

type window struct {
	ID        string      `json:"id"`
	AwayFrom  jsonv.Time  `json:"away_from"`
	AwayUntil *jsonv.Time `json:"away_until"`
}

type touch struct {
	At           jsonv.Time `json:"at"`
	InTurn       bool       `json:"in_turn"`
	InAway       bool       `json:"in_away"`
	BeforeAsked  bool       `json:"before_asked"`
	Tool         string     `json:"tool"`
	ResourceType string     `json:"resource_type"`
	ResourceID   *string    `json:"resource_id"`
	Action       string     `json:"action"`
	ModelSlug    *string    `json:"model_slug"`
}

func (q *Questions) doneFacts(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "session_id", "turn_id", "since")
	if err != nil {
		return nil, err
	}
	sid := b.Text("session_id", 0, gate.NameMax)
	turn := b.Text("turn_id", 0, gate.NameMax)
	since := b.Text("since", 1, 64)
	if err := b.Err(); err != nil {
		return nil, err
	}
	type facts struct {
		Windows []window `json:"windows"`
		Touches []touch  `json:"touches"`
	}
	out := facts{[]window{}, []touch{}}
	if !audit.IsUUID(sid) || !audit.IsUUID(turn) {
		return out, nil
	}
	err = pgx.BeginFunc(r.Context(), q.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		rows, err := tx.Query(ctx,
			`WITH RECURSIVE turns (id) AS (
			 SELECT $1::text::uuid UNION
			 SELECT a.turn_id FROM ai_approvals a JOIN turns t ON a.resume_turn_id = t.id
			 WHERE a.session_id = $2::text::uuid AND a.turn_id IS NOT NULL)
			 SELECT id, away_from, away_until, away_from::text AS from_text,
			 away_until::text AS until_text FROM (
			 SELECT q.id::text AS id, q.created_at AS away_from, reply.at AS away_until
			 FROM ai_questions q CROSS JOIN LATERAL (
			 SELECT min(at) AS at FROM (
			 SELECT resolved_at AS at FROM ai_questions WHERE session_id = $2::text::uuid
			 AND turn_id = q.turn_id AND outcome = 'answered' AND resolved_at > q.created_at
			 UNION ALL SELECT decided_at FROM ai_approvals WHERE session_id = $2::text::uuid
			 AND turn_id = q.turn_id AND decision IN ('approved', 'denied')
			 AND decided_by_kind = 'browser' AND decided_at > q.created_at) replies) reply
			 WHERE q.session_id = $2::text::uuid AND q.turn_id IN (SELECT id FROM turns)
			 AND q.kind = 'attention' AND q.outcome = 'timed_out'
			 UNION ALL
			 SELECT right(substring(r.request_id FROM '[^:]*$'), 8), r.requested_at, reply.at
			 FROM ai_input_responses r CROSS JOIN LATERAL (
			 SELECT min(created_at) AS at FROM ai_input_responses WHERE session_id = $2::text::uuid
			 AND requested_at >= $3::text::timestamptz AND created_at > r.requested_at
			 AND (outcome = 'answered' OR (kind = 'approval' AND outcome IN ('approved',
			 'denied') AND responder->>'kind' = 'browser'))) reply
			 WHERE r.session_id = $2::text::uuid AND r.tool = $4 AND r.outcome = 'timed_out'
			 AND r.requested_at >= $3::text::timestamptz
			 ) windows ORDER BY away_from, id`, turn, sid, since, waitForUser)
		if err != nil {
			return err
		}
		bounds := []any{}
		var earliest *time.Time
		for rows.Next() {
			var w window
			var from string
			var until *string
			if err := rows.Scan(&w.ID, &w.AwayFrom, &w.AwayUntil, &from, &until); err != nil {
				rows.Close()
				return err
			}
			var u any
			if until != nil {
				u = *until
			}
			bounds = append(bounds, jsonv.Object{{Key: "f", Value: from}, {Key: "u", Value: u}})
			if earliest == nil || w.AwayFrom.Before(*earliest) {
				at := w.AwayFrom.Time
				earliest = &at
			}
			out.Windows = append(out.Windows, w)
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return err
		}
		var first, earliestText *string
		if len(bounds) > 0 {
			f, _ := bounds[0].(jsonv.Object).Text("f")
			first = &f
		}
		if earliest != nil {
			s := earliest.Format(time.RFC3339Nano)
			earliestText = &s
		}
		rows, err = tx.Query(ctx,
			`SELECT at, at >= $1::text::timestamptz AS in_turn,
			 EXISTS (SELECT 1 FROM jsonb_to_recordset($2::text::jsonb) AS w(f timestamptz,
			 u timestamptz) WHERE at >= w.f AND (w.u IS NULL OR at < w.u)) AS in_away,
			 coalesce(at < $3::text::timestamptz, false) AS before_asked,
			 tool, resource_type, resource_id, action, model_slug
			 FROM ai_session_resources WHERE session_id = $4::text::uuid
			 AND (at >= $1::text::timestamptz OR at >= $5::text::timestamptz)
			 ORDER BY id`, since, jsonv.Dumps(bounds), first, sid, earliestText)
		if err != nil {
			return err
		}
		touches, err := pgx.CollectRows(rows, pgx.RowToStructByPos[touch])
		if err != nil {
			return err
		}
		if touches != nil {
			out.Touches = touches
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

type posted struct {
	Asked   bool         `json:"asked"`
	Limited *string      `json:"limited"`
	Logged  gate.Batches `json:"logged"`
}

func (q *Questions) done(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "id", "session_id", "turn_id", "attempt", "tool", "tool_use_id",
		"questions", "summary", "unattended")
	if err != nil {
		return nil, err
	}
	id := b.Text("id", 0, 1<<20)
	if b.Has("id") {
		b.Pattern("id", id, gate.SessionPattern)
	}
	sid := b.Text("session_id", 0, gate.NameMax)
	turn := b.Text("turn_id", 0, gate.NameMax)
	attempt := b.Text("attempt", 0, gate.NameMax)
	tool := b.Text("tool", 0, gate.NameMax)
	toolUse := b.Text("tool_use_id", 0, gate.NameMax)
	if !b.Has("questions") {
		b.Text("questions", 0, 0)
	}
	card := gate.Card(b, "questions", true)
	questions, _ := card.([]any)
	for i, item := range questions {
		if _, ok := item.(jsonv.Object); !ok {
			b.Fail("questions", fmt.Sprintf("item %d: Input should be a valid dictionary", i))
		}
	}
	if b.Has("questions") && card == nil {
		b.Fail("questions", "Input should be a valid list")
	}
	text := b.Text("summary", 0, gate.TextMax)
	unattended := b.Bool("unattended", nil)
	if err := b.Err(); err != nil {
		return nil, err
	}
	none := posted{}
	if !audit.IsUUID(sid) || !audit.IsUUID(turn) || !audit.IsUUID(id) {
		return none, nil
	}
	out := none
	err = pgx.BeginFunc(r.Context(), q.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		var owner string
		err := tx.QueryRow(ctx, `SELECT owner_kind FROM ai_sessions WHERE id = $1::text::uuid
			AND durable_turn_id = $2::text::uuid AND turn_attempt = $3 FOR UPDATE`,
			sid, turn, attempt).Scan(&owner)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil || owner != "browser" {
			return err
		}
		var count int
		if err := tx.QueryRow(ctx, `SELECT count(*)::int AS n FROM ai_questions
			WHERE session_id = $1::text::uuid AND turn_id = $2::text::uuid
			AND kind = 'attention' AND attention_reason = 'done'`, sid, turn).Scan(&count); err != nil {
			return err
		}
		if count >= donePostsPerTurn {
			limited := fmt.Sprintf("The summary was not posted: this turn already posted %d,"+
				" the most a turn may.", donePostsPerTurn)
			out = posted{Limited: &limited}
			return questionAudit{
				action: "rate_limited", surface: "system", actor: gate.System(), outcome: "refused",
				sessionID: sid, turnID: &turn, toolUseID: &toolUse,
				detail: tool + " done summary refused: " + limited, startedAt: time.Now(),
			}.insert(ctx, tx)
		}
		rows, err := tx.Query(ctx,
			`UPDATE ai_questions SET outcome = 'cancelled', reason = $1, resolved_at = now()
			 WHERE session_id = $2::text::uuid AND kind = 'attention' AND attention_reason = 'done'
			 AND outcome IS NULL AND (NOT unattended OR turn_id = $3::text::uuid)
			 AND (tool = $4) = $5
			 RETURNING id::text AS id, turn_id::text AS turn_id, tool, tool_use_id, created_at`,
			replaced, sid, turn, attentionTool, tool == attentionTool)
		if err != nil {
			return err
		}
		type gone struct {
			ID, TurnID, Tool, ToolUseID string
			CreatedAt                   time.Time
		}
		superseded, err := pgx.CollectRows(rows, pgx.RowToStructByPos[gone])
		if err != nil {
			return err
		}
		var evs []jsonv.Object
		for _, g := range superseded {
			evs = append(evs,
				gate.Event(gate.M("type", "question.resolved"), gate.M("sessionId", sid),
					gate.M("id", g.ID), gate.M("answered", false), gate.M("reason", replaced)),
				gate.Event(gate.M("type", "input.resolved"), gate.M("sessionId", sid),
					gate.M("id", "question:"+g.ID), gate.M("kind", "answer"),
					gate.M("outcome", "cancelled"), gate.M("reason", replaced)))
		}
		var created time.Time
		if err := tx.QueryRow(ctx,
			`INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind,
			 attention_reason, summary, unattended) VALUES ($1::text::uuid, $2::text::uuid,
			 $3::text::uuid, $4, $5, $6::text::jsonb, 'attention', 'done', $7, $8)
			 RETURNING created_at`,
			id, sid, turn, tool, toolUse, jsonv.Dumps(questions), text, unattended).Scan(&created); err != nil {
			return err
		}
		prompts := make([]string, len(questions))
		for i, item := range questions {
			v, ok := item.(jsonv.Object).Get("question")
			if ok {
				prompts[i] = gate.Str(v)
			}
		}
		evs = append(evs,
			gate.Event(gate.M("type", "question.asked"), gate.M("sessionId", sid), gate.M("id", id),
				gate.M("tool", toolUse), gate.M("questions", questions),
				gate.M("attention", jsonv.Object{gate.M("reason", "done"), gate.M("summary", text)})),
			gate.Event(gate.M("type", "input.requested"), gate.M("sessionId", sid),
				gate.M("entry", jsonv.Object{
					gate.M("id", "question:"+id), gate.M("kind", "answer"), gate.M("session_id", sid),
					gate.M("tool", tool), gate.M("summary", ""), gate.M("input_hash", nil),
					gate.M("prompt", strings.Join(prompts, "\n")), gate.M("requested_by", nil),
					gate.M("responders", []string{"browser"}),
					gate.M("created_at", jsonv.ISO(created)), gate.M("expires_at", nil),
					gate.M("attention", jsonv.Object{gate.M("reason", "done"),
						gate.M("on_timeout", nil), gate.M("summary", text)}),
				})))
		var logged gate.Batches
		if err := gate.Append(ctx, tx, &logged, sid, evs); err != nil {
			return err
		}
		for _, g := range superseded {
			if err := (questionAudit{
				action: "cancelled", surface: "system", actor: gate.System(), outcome: "refused",
				sessionID: sid, turnID: &g.TurnID, toolUseID: &g.ToolUseID,
				detail:    fmt.Sprintf("%s attention request %s: %s", g.Tool, g.ID, replaced),
				startedAt: g.CreatedAt,
			}).insert(ctx, tx); err != nil {
				return err
			}
		}
		out = posted{Asked: true, Logged: logged}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}
