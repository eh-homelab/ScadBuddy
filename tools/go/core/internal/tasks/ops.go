package tasks

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/activity"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

const (
	// operationRetention is operations.py OPERATION_RETENTION_SECONDS: the namespace's 168 h.
	operationRetention    = 7 * 24 * 3600
	operationRetentionMin = 3600
	operationRetentionMax = 10 * 365 * 86400
)

// OperationWrites are area operations' POST routes: an operation recorded and ended, each
// with its activity row in the same transaction (agent_data/operations.py).
func (t *Tasks) OperationWrites() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/operations", Handle: t.insertOperation, MaxBody: bodyMax},
		{Method: "POST", Path: "/operations/{operation_id}/finish", Handle: t.finishOperation, MaxBody: bodyMax},
	}
}

// pyJSON is a value as Python's json.loads read it and psycopg's Jsonb writes it: the text
// handed to a jsonb parameter.
func pyJSON(raw json.RawMessage) (string, error) {
	if raw == nil {
		return "null", nil
	}
	value, err := jsonv.Loads(raw)
	if err != nil {
		return "", err
	}
	return jsonv.Dumps(value), nil
}

func scanOperation(row pgx.Row) (*operation, error) {
	var o operation
	var created time.Time
	var finished *time.Time
	err := row.Scan(&o.ID, &o.Kind, &o.Subject, &o.Status, &o.Result, &o.Error, &created,
		&finished, &o.WorkflowID, &o.WorkflowRunID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	o.CreatedAt, o.FinishedAt = crud.Time(created), crud.TimeOf(finished)
	return &o, nil
}

type oneOperation struct {
	Operation *operation `json:"operation"`
}

// insertOperation records an accepted operation, or answers the execution's row when it has
// one (a retried activity), after pruning those finished more than the retention ago.
func (t *Tasks) insertOperation(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "id", "kind", "subject", "operation_key", "request",
		"workflow_id", "workflow_run_id", "actor", "retention_seconds")
	if err != nil {
		return nil, err
	}
	id := body.Get("id").String(1, nameMax, nil)
	kind := body.Get("kind").String(1, nameMax, nil)
	subject := body.Get("subject").String(0, nameMax, nil)
	key := body.Get("operation_key").String(0, 1<<30, operationKey)
	request, _ := body.Raw("request")
	workflowID := body.Get("workflow_id").String(1, nameMax, nil)
	runID := body.Get("workflow_run_id").String(1, nameMax, nil)
	var principal, session, viaTool *string
	if actor := body.Get("actor").OptObject("principal", "session", "via_tool"); actor != nil {
		p := actor.Get("principal").String(0, nameMax, nil)
		principal = &p
		session = actor.Get("session").OptString(0, nameMax, nil)
		viaTool = actor.Get("via_tool").OptString(0, nameMax, nil)
		actor.Close()
	}
	retention := int64(operationRetention)
	if v := body.Get("retention_seconds"); v.Given() {
		retention = v.Int(operationRetentionMin, operationRetentionMax, false)
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	requestJSON, err := pyJSON(request)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var found *operation
	err = pgx.BeginFunc(ctx, t.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `DELETE FROM ai_operations
			WHERE finished_at < now() - make_interval(secs => $1)`, retention); err != nil {
			return err
		}
		inserted, err := scanOperation(tx.QueryRow(ctx, `INSERT INTO ai_operations (id, kind,
			subject, operation_key, status, request, workflow_id, workflow_run_id, principal,
			session, via_tool) VALUES ($1, $2, $3, $4, 'running', $5::jsonb, $6, $7, $8, $9, $10)
			ON CONFLICT (workflow_id, workflow_run_id) DO NOTHING RETURNING `+operationColumns,
			id, kind, subject, key, requestJSON, workflowID, runID, principal, session, viaTool))
		if err != nil {
			return err
		}
		if inserted != nil {
			activity.IngestOperation(ctx, tx, inserted.ID)
			found = inserted
			return nil
		}
		found, err = scanOperation(tx.QueryRow(ctx, `SELECT `+operationColumns+` FROM ai_operations
			WHERE workflow_id = $1 AND workflow_run_id = $2`, workflowID, runID))
		if err == nil && found == nil {
			err = errors.New("the conflicting execution's row is not there")
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	return oneOperation{found}, nil
}

// operationError is the problem a failed operation's route answered with, as the agent sent
// it: status, title and detail, type and extensions when set, and any other member.
func operationError(body *crud.Body) jsonv.Object {
	raw, _ := body.Raw("error")
	var members map[string]json.RawMessage
	names := []string{}
	if json.Unmarshal(raw, &members) == nil {
		for name := range members {
			names = append(names, name)
		}
	}
	failure := body.Get("error").OptObject(names...)
	if failure == nil {
		return nil
	}
	known := []string{"status", "title", "detail", "type", "extensions"}
	out := jsonv.Object{}
	status := failure.Get("status").Int(-1<<62, 1<<62, false)
	title := failure.Get("title").String(0, 1<<30, nil)
	detail := failure.Get("detail").String(0, 1<<30, nil)
	out = append(out, jsonv.Member{Key: "status", Value: status},
		jsonv.Member{Key: "title", Value: title}, jsonv.Member{Key: "detail", Value: detail})
	if v := failure.Get("type"); v.Given() {
		out = append(out, jsonv.Member{Key: "type", Value: nullable(v.OptString(0, 1<<30, nil))})
	}
	if v := failure.Get("extensions"); v.Given() {
		var ext any
		if !v.Null() {
			ext = loaded(v.Dict())
		}
		out = append(out, jsonv.Member{Key: "extensions", Value: ext})
	}
	parsed, err := jsonv.Loads(raw)
	if object, ok := parsed.(jsonv.Object); ok && err == nil {
		for _, m := range object {
			extra := true
			for _, k := range known {
				extra = extra && m.Key != k
			}
			if extra {
				out = append(out, m)
			}
		}
	}
	failure.Close()
	return out
}

func nullable(s *string) any {
	if s == nil {
		return nil
	}
	return *s
}

func loaded(raw json.RawMessage) any {
	if raw == nil {
		return nil
	}
	v, err := jsonv.Loads(raw)
	if err != nil {
		return nil
	}
	return v
}

// finishOperation ends a running operation (error for a failure); a retried end finds it
// ended and changes nothing. 404 when there is none.
func (t *Tasks) finishOperation(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Text("operation_id", 1, nameMax)
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "result", "error")
	if err != nil {
		return nil, err
	}
	result, _ := body.Raw("result")
	failure := operationError(body)
	if err := body.Err(); err != nil {
		return nil, err
	}
	if failure != nil && result != nil && string(result) != "null" {
		body.Fail(nil, "an operation ends with a result or an error, not both")
		return nil, body.Err()
	}
	status, resultJSON, errorJSON := "succeeded", (*string)(nil), (*string)(nil)
	if failure != nil {
		status = "failed"
		text := jsonv.Dumps(failure)
		errorJSON = &text
	} else {
		text, err := pyJSON(result)
		if err != nil {
			return nil, err
		}
		resultJSON = &text
	}
	ctx := r.Context()
	var found *operation
	err = pgx.BeginFunc(ctx, t.Pool, func(tx pgx.Tx) error {
		var ended string
		err := tx.QueryRow(ctx, `UPDATE ai_operations SET status = $1, result = $2::jsonb,
			error = $3::jsonb, finished_at = now() WHERE id = $4 AND status = 'running'
			RETURNING id`, status, resultJSON, errorJSON, id).Scan(&ended)
		switch {
		case err == nil:
			activity.IngestOperation(ctx, tx, id)
		case !errors.Is(err, pgx.ErrNoRows):
			return err
		}
		found, err = scanOperation(tx.QueryRow(ctx, `SELECT `+operationColumns+`
			FROM ai_operations WHERE id = $1`, id))
		return err
	})
	if err != nil {
		return nil, err
	}
	if found == nil {
		return nil, &httpx.Problem{Status: http.StatusNotFound, Detail: fmt.Sprintf("There is no operation %s.", id)}
	}
	return oneOperation{found}, nil
}
