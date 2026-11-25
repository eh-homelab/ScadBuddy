// Package tasks is the agent's background tasks and the reads of its command record:
// backend/scadbuddy/api/crud_agent_tasks.py over agent_data/background.py and
// agent_data/operations.py, statement for statement (area background-tasks, and
// operations:read). An operation's insert and end stay Python's: they write its activity
// row in the same transaction.
package tasks

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	maxActive       = 10
	maxEvents       = 200
	noticeMaxChars  = 16384
	deliverMaxChars = 64_000
	detailMax       = 64 * 1024
	nameMax         = 512
	occurrenceMax   = 1<<31 - 1
	bodyMax         = 8 * 1024 * 1024
)

var (
	taskID       = regexp.MustCompile(`^[0-9a-f]{8}$`)
	uuidPattern  = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	callKey      = regexp.MustCompile(`^[0-9a-f]{64}$`)
	operationKey = regexp.MustCompile(`^[0-9a-f]{64}$`)
	seqText      = regexp.MustCompile(`^[0-9]{1,19}$`)
	kinds        = []string{"cron", "wakeup", "monitor", "workflow"}
	ownerKinds   = []string{"browser", "bearer", "oidc", "anonymous", "flow"}
)

const taskColumns = `id, session_id::text AS session_id, kind, description, spec, created_by, state,
 next_at, fired, last_fired_at, expires_at, detail, created_at, updated_at`

// Tasks is the areas' routes.
type Tasks struct{ Pool *pgxpool.Pool }

// Areas are the routes of areas background-tasks, background-tasks:read and operations:read.
func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route {
	t := &Tasks{Pool: pool}
	return map[string][]crud.Route{
		"background-tasks:read": t.Reads(),
		"background-tasks":      append(t.Reads(), t.Writes()...),
		"operations:read":       t.OperationReads(),
	}
}

// Reads are area background-tasks' GET routes.
func (t *Tasks) Reads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/background-tasks", Handle: t.list},
		{Method: "GET", Path: "/background-tasks/recurring-crons", Handle: t.recurringCrons},
		{Method: "GET", Path: "/background-tasks/{task_id}", Handle: t.get},
		{Method: "GET", Path: "/background-tasks/{task_id}/events/pending", Handle: t.pending},
		{Method: "GET", Path: "/background-tasks/{task_id}/events/count", Handle: t.count},
		{Method: "GET", Path: "/background-tasks/{task_id}/calls", Handle: t.call},
		{Method: "GET", Path: "/background-tasks/{task_id}/children/open", Handle: t.openChildren},
	}
}

// Writes are area background-tasks' POST routes.
func (t *Tasks) Writes() []crud.Route {
	post := func(path string, h crud.Handler) crud.Route {
		return crud.Route{Method: "POST", Path: path, Handle: h, MaxBody: bodyMax}
	}
	return []crud.Route{
		post("/background-tasks", t.create),
		post("/background-tasks/{task_id}/scheduled", t.scheduled),
		post("/background-tasks/{task_id}/fired", t.fired),
		post("/background-tasks/{task_id}/finish", t.finish),
		post("/background-tasks/{task_id}/spec", t.spec),
		post("/background-tasks/{task_id}/progress", t.progress),
		post("/background-tasks/{task_id}/events", t.event),
		post("/background-tasks/{task_id}/events/delivered", t.delivered),
		post("/background-tasks/{task_id}/calls", t.journal),
		post("/background-tasks/{task_id}/children", t.child),
	}
}

// OperationReads are area operations' GET routes.
func (t *Tasks) OperationReads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/operations/by-key/{operation_key}", Handle: t.findOperation},
		{Method: "GET", Path: "/operations/{operation_id}", Handle: t.getOperation},
	}
}

type task struct {
	ID          string          `json:"id"`
	SessionID   string          `json:"session_id"`
	Kind        string          `json:"kind"`
	Description string          `json:"description"`
	Spec        json.RawMessage `json:"spec"`
	CreatedBy   json.RawMessage `json:"created_by"`
	State       string          `json:"state"`
	NextAt      *crud.Time      `json:"next_at"`
	Fired       int64           `json:"fired"`
	LastFiredAt *crud.Time      `json:"last_fired_at"`
	ExpiresAt   *crud.Time      `json:"expires_at"`
	Detail      *string         `json:"detail"`
	CreatedAt   crud.Time       `json:"created_at"`
	UpdatedAt   crud.Time       `json:"updated_at"`
}

func scanTask(row pgx.CollectableRow) (task, error) {
	var t task
	var next, last, expires *time.Time
	var created, updated time.Time
	err := row.Scan(&t.ID, &t.SessionID, &t.Kind, &t.Description, &t.Spec, &t.CreatedBy, &t.State,
		&next, &t.Fired, &last, &expires, &t.Detail, &created, &updated)
	t.NextAt, t.LastFiredAt, t.ExpiresAt = crud.TimeOf(next), crud.TimeOf(last), crud.TimeOf(expires)
	t.CreatedAt, t.UpdatedAt = crud.Time(created), crud.Time(updated)
	return t, err
}

type oneTask struct {
	Task *task `json:"task"`
}

type taskList struct {
	Tasks []task `json:"tasks"`
}

type ok struct {
	OK bool `json:"ok"`
}

var done = ok{true}

func one(rows pgx.Rows, err error) (oneTask, error) {
	if err != nil {
		return oneTask{}, err
	}
	found, err := pgx.CollectExactlyOneRow(rows, scanTask)
	if errors.Is(err, pgx.ErrNoRows) {
		return oneTask{}, nil
	}
	return oneTask{&found}, err
}

func many(rows pgx.Rows, err error) (taskList, error) {
	if err != nil {
		return taskList{}, err
	}
	found, err := pgx.CollectRows(rows, scanTask)
	return taskList{append([]task{}, found...)}, err
}

func newTaskID() string {
	b := make([]byte, 4)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// create inserts an active task, or task null when the session already has maxActive,
// counted under the session's lock.
func (t *Tasks) create(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "session_id", "kind", "description", "spec", "created_by",
		"next_at", "expires_at")
	if err != nil {
		return nil, err
	}
	session := body.Match("session_id", uuidPattern)
	kind := body.Enum("kind", kinds...)
	description := body.Text("description", 1, 500)
	spec := body.Dict("spec")
	var createdBy []byte
	if _, given := body.Required("created_by"); given {
		if o := body.Object("created_by", "kind", "id", "label"); o == nil {
			body.Fail([]string{"created_by"}, "Input should be a valid dictionary or object to extract fields from")
		} else {
			owner := struct {
				Kind  string `json:"kind"`
				ID    string `json:"id"`
				Label string `json:"label"`
			}{o.Enum("kind", ownerKinds...), o.Text("id", 0, nameMax), o.Text("label", 0, nameMax)}
			o.Done()
			createdBy, _ = json.Marshal(owner)
		}
	}
	nextAt, expiresAt := body.OptTime("next_at"), body.OptTime("expires_at")
	if err := body.Err(); err != nil {
		return nil, err
	}
	var out oneTask
	err = pgx.BeginFunc(r.Context(), t.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`, "bgtask:"+session); err != nil {
			return err
		}
		var n int
		if err := tx.QueryRow(ctx, `SELECT count(*)::int AS n FROM ai_background_tasks
			WHERE session_id = $1::text::uuid AND state = 'active'`, session).Scan(&n); err != nil {
			return err
		}
		if n >= maxActive {
			return nil
		}
		var err error
		out, err = one(tx.Query(ctx, `INSERT INTO ai_background_tasks
			(id, session_id, kind, description, spec, created_by, next_at, expires_at)
			VALUES ($1, $2::text::uuid, $3, $4, $5::jsonb, $6::jsonb, $7, $8) RETURNING `+taskColumns,
			newTaskID(), session, kind, description, spec, string(createdBy), nextAt, expiresAt))
		return err
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

func (t *Tasks) list(r *http.Request, _ crud.Params) (any, error) {
	session, err := crud.QueryMatch(r, "session_id", uuidPattern)
	if err != nil {
		return nil, err
	}
	all, err := crud.QueryBool(r, "all")
	if err != nil {
		return nil, err
	}
	active := " AND state = 'active'"
	if all {
		active = ""
	}
	return many(t.Pool.Query(r.Context(), `SELECT `+taskColumns+` FROM ai_background_tasks
		WHERE session_id = $1::text::uuid`+active+` ORDER BY created_at DESC, id`, session))
}

func (t *Tasks) recurringCrons(r *http.Request, _ crud.Params) (any, error) {
	return many(t.Pool.Query(r.Context(), `SELECT `+taskColumns+` FROM ai_background_tasks
		WHERE state = 'active' AND kind = 'cron'
		AND coalesce(spec->>'recurring', 'true') <> 'false' ORDER BY created_at, id`))
}

func (t *Tasks) get(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Match("task_id", taskID)
	if err != nil {
		return nil, err
	}
	return one(t.Pool.Query(r.Context(),
		`SELECT `+taskColumns+` FROM ai_background_tasks WHERE id = $1`, id))
}

// write runs statement with the task id and the body's value.
func (t *Tasks) write(r *http.Request, p crud.Params, statement string, read func(*crud.Body) any, names ...string) (any, error) {
	id, err := p.Match("task_id", taskID)
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, names...)
	if err != nil {
		return nil, err
	}
	value := read(body)
	if err := body.Err(); err != nil {
		return nil, err
	}
	if _, err := t.Pool.Exec(r.Context(), statement, value, id); err != nil {
		return nil, err
	}
	return done, nil
}

func at(b *crud.Body) any { return b.Time("at") }

func (t *Tasks) scheduled(r *http.Request, p crud.Params) (any, error) {
	return t.write(r, p, `UPDATE ai_background_tasks SET next_at = $1, updated_at = now()
		WHERE id = $2 AND state = 'active'`, at, "at")
}

func (t *Tasks) fired(r *http.Request, p crud.Params) (any, error) {
	return t.write(r, p, `UPDATE ai_background_tasks SET fired = fired + 1, last_fired_at = $1,
		next_at = NULL, updated_at = now() WHERE id = $2 AND state = 'active'`, at, "at")
}

func (t *Tasks) spec(r *http.Request, p crud.Params) (any, error) {
	return t.write(r, p, `UPDATE ai_background_tasks SET spec = $1::jsonb, updated_at = now() WHERE id = $2`,
		func(b *crud.Body) any { return b.Dict("spec") }, "spec")
}

func (t *Tasks) progress(r *http.Request, p crud.Params) (any, error) {
	return t.write(r, p, `UPDATE ai_background_tasks SET detail = $1, updated_at = now()
		WHERE id = $2 AND state = 'active'`, func(b *crud.Body) any { return b.Text("detail", 0, detailMax) }, "detail")
}

// finish ends an active task; finished is false when it had ended already.
func (t *Tasks) finish(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Match("task_id", taskID)
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "state", "detail")
	if err != nil {
		return nil, err
	}
	state := body.Enum("state", "done", "cancelled", "failed")
	detail := body.OptText("detail", 0, detailMax)
	if err := body.Err(); err != nil {
		return nil, err
	}
	var gone string
	err = t.Pool.QueryRow(r.Context(), `UPDATE ai_background_tasks SET state = $1, detail = $2,
		next_at = NULL, updated_at = now() WHERE id = $3 AND state = 'active' RETURNING id`,
		state, detail, id).Scan(&gone)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	return struct {
		Finished bool `json:"finished"`
	}{err == nil}, nil
}

// event records one event of a monitor (false once it has maxEvents), or with notice its
// last notice, past the cap.
func (t *Tasks) event(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Match("task_id", taskID)
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "text", "notice")
	if err != nil {
		return nil, err
	}
	text := body.Text("text", 0, noticeMaxChars)
	notice := body.OptBool("notice", false)
	if err := body.Err(); err != nil {
		return nil, err
	}
	appended := true
	if notice {
		_, err = t.Pool.Exec(r.Context(),
			`INSERT INTO ai_background_events (task_id, text) VALUES ($1, $2)`, id, text)
	} else {
		var seq int64
		err = t.Pool.QueryRow(r.Context(),
			`INSERT INTO ai_background_events (task_id, text) SELECT $1, $2
			 WHERE (SELECT count(*) FROM ai_background_events WHERE task_id = $1) < $3 RETURNING seq`,
			id, text, maxEvents).Scan(&seq)
		if errors.Is(err, pgx.ErrNoRows) {
			appended, err = false, nil
		}
	}
	if err != nil {
		return nil, err
	}
	return struct {
		Appended bool `json:"appended"`
	}{appended}, nil
}

type pendingEvent struct {
	Seq  string `json:"seq"`
	Text string `json:"text"`
}

// pending is a task's undelivered events, oldest first, as many as fit in max_chars of
// text, and always the oldest one.
func (t *Tasks) pending(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Match("task_id", taskID)
	if err != nil {
		return nil, err
	}
	most, given, err := crud.QueryInt(r, "max_chars", 1, 10_000_000)
	if err != nil {
		return nil, err
	}
	if !given {
		most = deliverMaxChars
	}
	rows, err := t.Pool.Query(r.Context(), `SELECT seq::text AS seq, text FROM (
		 SELECT seq, text, sum(length(text)) OVER (ORDER BY seq) AS through,
		 row_number() OVER (ORDER BY seq) AS n
		 FROM ai_background_events WHERE task_id = $1 AND delivered_at IS NULL
		) pending WHERE n = 1 OR through <= $2 ORDER BY pending.seq`, id, most)
	if err != nil {
		return nil, err
	}
	events, err := pgx.CollectRows(rows, pgx.RowToStructByPos[pendingEvent])
	if err != nil {
		return nil, err
	}
	return struct {
		Events []pendingEvent `json:"events"`
	}{append([]pendingEvent{}, events...)}, nil
}

func (t *Tasks) count(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Match("task_id", taskID)
	if err != nil {
		return nil, err
	}
	pending, err := crud.QueryBool(r, "pending")
	if err != nil {
		return nil, err
	}
	undelivered := ""
	if pending {
		undelivered = " AND delivered_at IS NULL"
	}
	var n int64
	if err := t.Pool.QueryRow(r.Context(), `SELECT count(*)::int AS n FROM ai_background_events
		WHERE task_id = $1`+undelivered, id).Scan(&n); err != nil {
		return nil, err
	}
	return struct {
		Count int64 `json:"count"`
	}{n}, nil
}

func (t *Tasks) delivered(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Match("task_id", taskID)
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "up_to")
	if err != nil {
		return nil, err
	}
	upTo := body.Match("up_to", seqText)
	if err := body.Err(); err != nil {
		return nil, err
	}
	if _, err := t.Pool.Exec(r.Context(), `UPDATE ai_background_events SET delivered_at = now()
		WHERE task_id = $1 AND seq <= $2::text::numeric AND delivered_at IS NULL`, id, upTo); err != nil {
		return nil, err
	}
	return done, nil
}

type call struct {
	Result json.RawMessage `json:"result"`
	OK     bool            `json:"ok"`
}

// call is a Workflow's journalled agent() result, kept as {value} so a null result is a
// row too; the agent's SQL kept an undefined result as {}, which reads as null.
func (t *Tasks) call(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Match("task_id", taskID)
	if err != nil {
		return nil, err
	}
	key, err := crud.QueryMatch(r, "key", callKey)
	if err != nil {
		return nil, err
	}
	occurrence, given, err := crud.QueryInt(r, "occurrence", 0, occurrenceMax)
	if err == nil && !given {
		err = crud.QueryRequired("occurrence")
	}
	if err != nil {
		return nil, err
	}
	var found call
	err = t.Pool.QueryRow(r.Context(), `SELECT coalesce(result->'value', 'null'::jsonb), ok
		FROM ai_workflow_calls WHERE task_id = $1 AND call_key = $2 AND occurrence = $3`,
		id, key, occurrence).Scan(&found.Result, &found.OK)
	if errors.Is(err, pgx.ErrNoRows) {
		return struct {
			Call *call `json:"call"`
		}{}, nil
	}
	if err != nil {
		return nil, err
	}
	return struct {
		Call *call `json:"call"`
	}{&found}, nil
}

// journal records one agent() result; a second write of the same call keeps the first.
func (t *Tasks) journal(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Match("task_id", taskID)
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "key", "occurrence", "result", "ok", "child_id")
	if err != nil {
		return nil, err
	}
	key := body.Match("key", callKey)
	occurrence := body.IntIn("occurrence", 0, occurrenceMax, nil)
	result := body.Any("result")
	ok := body.Bool("ok")
	child := body.OptMatch("child_id", uuidPattern)
	if err := body.Err(); err != nil {
		return nil, err
	}
	if _, err := t.Pool.Exec(r.Context(), `INSERT INTO ai_workflow_calls
		(task_id, call_key, occurrence, result, ok, child_id)
		VALUES ($1, $2, $3, jsonb_build_object('value', $4::jsonb), $5, $6::text::uuid)
		ON CONFLICT DO NOTHING`, id, key, occurrence, result, ok, child); err != nil {
		return nil, err
	}
	return done, nil
}

func (t *Tasks) child(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Match("task_id", taskID)
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "child_id")
	if err != nil {
		return nil, err
	}
	child := body.Match("child_id", uuidPattern)
	if err := body.Err(); err != nil {
		return nil, err
	}
	if _, err := t.Pool.Exec(r.Context(), `INSERT INTO ai_workflow_children (task_id, child_id)
		VALUES ($1, $2::text::uuid) ON CONFLICT DO NOTHING`, id, child); err != nil {
		return nil, err
	}
	return done, nil
}

// openChildren is the child chats a Workflow's earlier attempts left open: not done, and
// named by no journal row.
func (t *Tasks) openChildren(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Match("task_id", taskID)
	if err != nil {
		return nil, err
	}
	rows, err := t.Pool.Query(r.Context(), `SELECT c.child_id::text AS child_id FROM ai_workflow_children c
		JOIN ai_sessions s ON s.id = c.child_id AND s.status <> 'done'
		WHERE c.task_id = $1 AND NOT EXISTS (SELECT 1 FROM ai_workflow_calls w
		WHERE w.task_id = c.task_id AND w.child_id = c.child_id)
		ORDER BY c.created_at`, id)
	if err != nil {
		return nil, err
	}
	children, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return nil, err
	}
	return struct {
		Children []string `json:"children"`
	}{append([]string{}, children...)}, nil
}

const operationColumns = `id, kind, subject, status, result, error, created_at, finished_at,
 workflow_id, workflow_run_id`

type operation struct {
	ID            string          `json:"id"`
	Kind          string          `json:"kind"`
	Subject       string          `json:"subject"`
	Status        string          `json:"status"`
	Result        json.RawMessage `json:"result"`
	Error         json.RawMessage `json:"error"`
	CreatedAt     crud.Time       `json:"created_at"`
	FinishedAt    *crud.Time      `json:"finished_at"`
	WorkflowID    string          `json:"workflow_id"`
	WorkflowRunID string          `json:"workflow_run_id"`
}

func (t *Tasks) operation(r *http.Request, query string, arg string) (any, error) {
	var o operation
	var created time.Time
	var finished *time.Time
	err := t.Pool.QueryRow(r.Context(), `SELECT `+operationColumns+` FROM ai_operations `+query, arg).Scan(
		&o.ID, &o.Kind, &o.Subject, &o.Status, &o.Result, &o.Error, &created, &finished,
		&o.WorkflowID, &o.WorkflowRunID)
	if errors.Is(err, pgx.ErrNoRows) {
		return struct {
			Operation *operation `json:"operation"`
		}{}, nil
	}
	if err != nil {
		return nil, err
	}
	o.CreatedAt, o.FinishedAt = crud.Time(created), crud.TimeOf(finished)
	return struct {
		Operation *operation `json:"operation"`
	}{&o}, nil
}

// findOperation is the key's newest operation, whatever its status: one key is one effect.
func (t *Tasks) findOperation(r *http.Request, p crud.Params) (any, error) {
	key, err := p.Match("operation_key", operationKey)
	if err != nil {
		return nil, err
	}
	return t.operation(r, `WHERE operation_key = $1 ORDER BY created_at DESC LIMIT 1`, key)
}

func (t *Tasks) getOperation(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Text("operation_id", 1, nameMax)
	if err != nil {
		return nil, err
	}
	return t.operation(r, `WHERE id = $1`, id)
}
