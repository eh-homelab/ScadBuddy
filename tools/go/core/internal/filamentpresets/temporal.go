package filamentpresets

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
)

// WorkflowName is the workflow that runs one preset write (plan ruling P21). Core's own
// worker runs it on the area's task queue. Its one step is the Python activity on the
// library queue, which the API process serves (#5295: only the API writes the rows).
const WorkflowName = "FilamentPresetWrite"

// timeoutFailure is a Failure's Type when Temporal timed the activity out: the write may
// have run, so the same key is sent again.
const timeoutFailure = "temporal_timeout"

// WriteRun is the workflow's input: the activity by name, its queue, its one JSON object
// argument, and its start-to-close timeout in seconds.
type WriteRun struct {
	Activity      string          `json:"activity"`
	Queue         string          `json:"queue"`
	Input         json.RawMessage `json:"input"`
	StartToCloseS int64           `json:"start_to_close_s"`
}

// WriteWorkflow runs in.Activity once (no retry, R11) on in.Queue and gives its result as
// it came. The activity's failure is the workflow's.
func WriteWorkflow(ctx workflow.Context, in WriteRun) (json.RawMessage, error) {
	ctx = workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		TaskQueue:           in.Queue,
		StartToCloseTimeout: time.Duration(in.StartToCloseS) * time.Second,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 1},
	})
	var out json.RawMessage
	err := workflow.ExecuteActivity(ctx, in.Activity, in.Input).Get(ctx, &out)
	return out, err
}

// TemporalStarter is the Starter over a Temporal client: it starts WriteWorkflow on Queue,
// whose activity runs on LibraryQueue.
//
// The start's id conflict policy is USE_EXISTING, so a send while a run of the id is open
// joins that run (R12). Its reuse policy is ALLOW_DUPLICATE: a send after the run closed
// starts a new run. That run writes nothing twice. The activity keeps each reply in the
// idempotency ledger under the actor, the key and the preset, and answers the kept reply
// (crud/idempotency.py idempotent_write). The ledger also refuses the key sent again with
// another body (idempotency_key_reused), which the old run's result could not. A refused
// write keeps no reply, so a new run checks it again, as a Python route does. A send again
// (the mirror) has no key: a new run sends again, as asked.
type TemporalStarter struct {
	Client              client.Client
	Queue, LibraryQueue string
}

// Run starts or joins w's run and waits for its result until ctx ends.
func (s TemporalStarter) Run(ctx context.Context, w Write) (json.RawMessage, error) {
	input, err := json.Marshal(w.Input)
	if err != nil {
		return nil, err
	}
	run, err := s.Client.ExecuteWorkflow(ctx, client.StartWorkflowOptions{
		ID:                       w.WorkflowID,
		TaskQueue:                s.Queue,
		WorkflowIDConflictPolicy: enumspb.WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING,
		WorkflowIDReusePolicy:    enumspb.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE,
	}, WorkflowName, WriteRun{Activity: w.Activity, Queue: s.LibraryQueue, Input: input,
		StartToCloseS: int64(w.Timeout / time.Second)})
	if err != nil {
		return nil, err
	}
	var out json.RawMessage
	if err := run.Get(ctx, &out); err != nil {
		if f := failureOf(err); f != nil {
			return nil, f
		}
		return nil, err
	}
	return out, nil
}

// failureOf is the activity's failure in err: its type, message and first detail, or a
// timeoutFailure. Nil for any other error (the wait's own end, a Temporal out of reach).
func failureOf(err error) *Failure {
	var app *temporal.ApplicationError
	if errors.As(err, &app) {
		f := &Failure{Type: app.Type(), Message: app.Message()}
		if app.HasDetails() {
			var detail json.RawMessage
			if app.Details(&detail) == nil {
				f.Detail = detail
			}
		}
		return f
	}
	var timeout *temporal.TimeoutError
	if errors.As(err, &timeout) {
		return &Failure{Type: timeoutFailure, Message: timeout.Error()}
	}
	return nil
}
