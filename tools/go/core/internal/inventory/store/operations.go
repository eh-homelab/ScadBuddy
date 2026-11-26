package store

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/problem"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/publish"
)

// The record of each inventory activity (R-K1-22, design §6.1a "Visibility"), as the
// agent's ai_operations is: a row in inventory_operations that Go alone writes, and the
// notice inventory_operation.changed on the bus after each change, from which Python's
// activity index writes the Activities tab's row.

const (
	// OperationChanged is the notice's kind (Python's InventoryOperationEvent).
	OperationChanged = "inventory_operation.changed"
	// staleAfter is how long a running row waits before a read asks Temporal about it.
	staleAfter = 60 * time.Second
	// lastErrorMax is the most characters of an attempt's error that the row keeps.
	lastErrorMax = 2000
)

// Operation is one attempt of an activity: its id (the idempotency key), its name, what it
// acts on (item:<id>, lot:<id>, unit:<id>, item_kind:<id>, printer:<id>), the workflow that
// runs it (empty for a standalone activity), the attempt (from 1), and who it is by.
type Operation struct {
	ID, Kind, Subject, WorkflowID string
	Attempt                       int
	Actor                         Actor
}

type operationKey struct{}

// WithOperation is ctx with the operation id that Write ends (FinishOperation) in the
// write's own transaction.
func WithOperation(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, operationKey{}, id)
}

// OperationFrom is the operation id WithOperation put in ctx, and whether there is one.
func OperationFrom(ctx context.Context) (string, bool) {
	id, ok := ctx.Value(operationKey{}).(string)
	return id, ok
}

// StartOperation writes op as running, in a short transaction of its own, and sends the
// notice. A running row of the id (an earlier attempt) takes this attempt; its last_error
// stays. A finished row is never opened again: a replay or a reuse of its key changes
// nothing and sends nothing.
func StartOperation(ctx context.Context, pool *pgxpool.Pool, op Operation) error {
	var actor any
	if len(op.Actor) > 0 {
		actor = string(op.Actor)
	}
	return pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `INSERT INTO inventory_operations
			(id, kind, subject, status, workflow_id, attempt, actor)
			VALUES ($1, $2, $3, 'running', NULLIF($4, ''), $5, $6::jsonb)
			ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind, subject = EXCLUDED.subject,
				status = 'running', workflow_id = EXCLUDED.workflow_id, attempt = EXCLUDED.attempt,
				actor = EXCLUDED.actor, updated_at = clock_timestamp()
			WHERE inventory_operations.status = 'running'`,
			op.ID, op.Kind, op.Subject, op.WorkflowID, op.Attempt, actor)
		if err != nil || tag.RowsAffected() == 0 {
			return err
		}
		return notifyOperation(ctx, tx, op.ID)
	})
}

// FinishOperation ends the operation id with the answer a, in tx, the write's own
// transaction: succeeded, or failed with the problem when a.Status is 400 or more. So the
// row's end commits with the stock write, or not at all. The notice goes at the commit. An
// id with no running row (an activity started with no record, or a finished one) changes
// nothing and sends nothing.
func FinishOperation(ctx context.Context, tx pgx.Tx, id string, a Answer) error {
	status, problem := "succeeded", []byte(nil)
	if a.Status >= 400 {
		status, problem = "failed", a.Body
	}
	tag, err := tx.Exec(ctx, `UPDATE inventory_operations SET status = $2, problem = $3::jsonb,
		finished_at = clock_timestamp(), updated_at = clock_timestamp()
		WHERE id = $1 AND status = 'running'`,
		id, status, nullJSON(problem))
	if err != nil || tag.RowsAffected() == 0 {
		return err
	}
	return notifyOperation(ctx, tx, id)
}

// NoteError records that an attempt failed with an infrastructure error: the row stays
// running, Temporal retries the activity, and the row keeps the error and the attempt.
func NoteError(ctx context.Context, pool *pgxpool.Pool, id string, attempt int, cause error) error {
	text := []rune(cause.Error())
	if len(text) > lastErrorMax {
		text = text[:lastErrorMax]
	}
	return pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `UPDATE inventory_operations SET last_error = $2, attempt = $3,
			updated_at = clock_timestamp() WHERE id = $1 AND status = 'running'`, id, string(text), attempt)
		if err != nil || tag.RowsAffected() == 0 {
			return err
		}
		return notifyOperation(ctx, tx, id)
	})
}

func notifyOperation(ctx context.Context, tx pgx.Tx, id string) error {
	return publish.Notify(ctx, tx, OperationChanged, map[string]any{"operation_id": id})
}

func nullJSON(raw []byte) any {
	if raw == nil {
		return nil
	}
	return string(raw)
}

// Temporal tells whether an activity that its row says is running has closed without a
// result in Temporal (failed, timed out, cancelled or terminated): the activity id, or the
// workflow that runs it when workflowID is not empty. It gives the last failure's message
// and the close time.
type Temporal interface {
	Closed(ctx context.Context, id, workflowID string) (closed bool, detail string, at time.Time, err error)
}

// ReadOperation is the operation id as Python's Operation, and whether there is one. A row
// running for more than staleAfter is checked with temporal (when not nil): an activity that
// Temporal closed without a result is answered as failed with inventory_activity_failed.
// The read writes nothing; Temporal is the source, and no row is repaired. A Temporal out
// of reach leaves the row as it reads.
func ReadOperation(ctx context.Context, q Querier, id string, temporal Temporal) (wire.Operation, bool, error) {
	var op wire.Operation
	var workflowID *string
	var problemBody, result []byte
	var stale bool
	err := q.QueryRow(ctx, `SELECT o.id, o.kind, o.subject, o.status, o.workflow_id, o.problem,
			o.created_at, o.finished_at,
			o.status = 'running' AND o.updated_at < now() - make_interval(secs => $2),
			CASE WHEN o.status = 'succeeded' THEN r.body END
		FROM inventory_operations o
		LEFT JOIN crud_requests r ON r.caller = $3 AND r.key = o.id
		WHERE o.id = $1`, id, staleAfter.Seconds(), Caller).
		Scan(&op.ID, &op.Kind, &op.Subject, &op.Status, &workflowID, &problemBody,
			&op.CreatedAt, &op.FinishedAt, &stale, &result)
	if errors.Is(err, pgx.ErrNoRows) {
		return wire.Operation{}, false, nil
	}
	if err != nil {
		return wire.Operation{}, false, err
	}
	if result != nil {
		op.Result = result
	}
	if problemBody != nil {
		if op.Error, err = printRunError(problemBody); err != nil {
			return wire.Operation{}, false, err
		}
	}
	if stale && temporal != nil {
		closed, detail, at, err := temporal.Closed(ctx, op.ID, deref(workflowID))
		if err != nil {
			slog.WarnContext(ctx, "could not ask Temporal about a running inventory activity",
				"operation_id", op.ID, "error", err.Error())
		} else if closed {
			failed := &problem.Refused{Status: 500, Code: "inventory_activity_failed", Detail: detail}
			body, err := httpx.Marshal(failed.Body())
			if err != nil {
				return wire.Operation{}, false, err
			}
			if op.Error, err = printRunError(body); err != nil {
				return wire.Operation{}, false, err
			}
			op.Status = "failed"
			if !at.IsZero() {
				op.FinishedAt = &at
			}
		}
	}
	return op, true, nil
}

// printRunError is a problem body as PrintRunError: type, status, title and detail, and
// every other member in extensions.
func printRunError(body []byte) (*wire.PrintRunError, error) {
	var members map[string]any
	if err := json.Unmarshal(body, &members); err != nil {
		return nil, err
	}
	e := &wire.PrintRunError{Type: "about:blank", Extensions: map[string]any{}}
	for k, v := range members {
		switch k {
		case "type":
			e.Type, _ = v.(string)
		case "status":
			n, _ := v.(float64)
			e.Status = int(n)
		case "title":
			e.Title, _ = v.(string)
		case "detail":
			e.Detail, _ = v.(string)
		default:
			e.Extensions[k] = v
		}
	}
	return e, nil
}
