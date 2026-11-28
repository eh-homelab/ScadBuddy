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
// argument, its start-to-close timeout in seconds, and its schedule-to-start timeout in
// seconds: the time the route waits (plan ruling P23). The run's own execution timeout is
// that time too (P26), so the activity does not start later than a moment after the route answered (the
// timeout counts from the moment Temporal records the start): see
// TemporalStarter.
type WriteRun struct {
	Activity         string          `json:"activity"`
	Queue            string          `json:"queue"`
	Input            json.RawMessage `json:"input"`
	StartToCloseS    int64           `json:"start_to_close_s"`
	ScheduleToStartS int64           `json:"schedule_to_start_s"`
}

// activityOptions are the options of the write's one activity: no retry (R11).
func activityOptions(in WriteRun) workflow.ActivityOptions {
	return workflow.ActivityOptions{
		TaskQueue:              in.Queue,
		StartToCloseTimeout:    time.Duration(in.StartToCloseS) * time.Second,
		ScheduleToStartTimeout: time.Duration(in.ScheduleToStartS) * time.Second,
		RetryPolicy:            &temporal.RetryPolicy{MaximumAttempts: 1},
	}
}

// runOf is w's workflow input: its schedule-to-start is the route's wait, w's timeout plus
// waitMargin.
func runOf(w Write, queue string) WriteRun {
	return WriteRun{Activity: w.Activity, Queue: queue, StartToCloseS: int64(w.Timeout / time.Second),
		ScheduleToStartS: int64((w.Timeout + waitMargin) / time.Second)}
}

// WriteWorkflow runs in.Activity once (no retry, R11) on in.Queue and gives its result as
// it came. The activity's failure is the workflow's.
func WriteWorkflow(ctx workflow.Context, in WriteRun) (json.RawMessage, error) {
	ctx = workflow.WithActivityOptions(ctx, activityOptions(in))
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
//
// The id also hashes the body (plan ruling P24), so one key with another body runs apart and
// the ledger answers it idempotency_key_reused.
//
// The run's execution timeout is the route's wait (plan ruling P26). The schedule-to-start
// timeout of P23 starts only when the workflow task schedules the activity, and that needs
// core's own worker. When no core worker takes the workflow task in time, the run closes as
// the route answers 503 (its timer starts when Temporal records the start, a moment after
// the route's), and Temporal gives a closed run's activity task to no worker. So a write
// does not start after that moment. An activity that started in time may still end after
// the run closed: its write stands, and a copy or an edit keeps its reply in the ledger.
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
	in := runOf(w, s.LibraryQueue)
	in.Input = input
	run, err := s.Client.ExecuteWorkflow(ctx, client.StartWorkflowOptions{
		ID:                       w.WorkflowID,
		TaskQueue:                s.Queue,
		WorkflowIDConflictPolicy: enumspb.WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING,
		WorkflowIDReusePolicy:    enumspb.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE,
		WorkflowExecutionTimeout: w.Timeout + waitMargin,
	}, WorkflowName, in)
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
