// Package libraryrun runs one Python activity on the library queue from a core route
// (#7204 P21, #8489 U1). Go writes no row: the API process serves the library queue, and it
// is the only process with a database secret (#5295). A route makes a Write, and a Starter
// runs it as one run of core's workflow LibraryRun on core's own worker. The workflow
// schedules the activity by name with one attempt, and its failure comes back as a *Failure.
// The filament presets' writes (internal/filamentpresets) and the resolve of an unknown tray
// (internal/unknowntrays) use it.
package libraryrun

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

// WorkflowName is the workflow that runs one activity. Core's own worker runs it on its task
// queue (temporalx.Config.FilamentPresetsQueue). It was FilamentPresetWrite until #8489; a
// run lasts less than 100 s, so a run open at the deploy that renamed it fails once, as a
// write that did not answer.
const WorkflowName = "LibraryRun"

// TimeoutFailure is a Failure's Type when Temporal timed the activity out: the activity may
// have run.
const TimeoutFailure = "temporal_timeout"

// WaitMargin is how much longer than a Write's timeout a route waits for it. The run's
// schedule-to-start and execution timeouts are that wait too (P23, P26).
const WaitMargin = 10 * time.Second

// Write is one activity to run: its run's id, the activity by name, the activity's one JSON
// object argument, and its start-to-close timeout.
type Write struct {
	WorkflowID string
	Activity   string
	Input      jsonv.Object
	Timeout    time.Duration
}

// Starter runs a Write: it starts the write's run, or joins the open run with its id, and
// waits for the activity's result until ctx ends. An activity failure is a *Failure; any
// other error (a start Temporal refused, the wait's end) means the activity may still run.
type Starter interface {
	Run(ctx context.Context, w Write) (json.RawMessage, error)
}

// Failure is an activity's failure: its type (one of the problem codes), its message and
// its first detail.
type Failure struct {
	Type, Message string
	Detail        json.RawMessage
}

func (f *Failure) Error() string { return f.Type + ": " + f.Message }

// Run is the workflow's input: the activity by name, its queue, its one JSON object
// argument, its start-to-close timeout in seconds, and its schedule-to-start timeout in
// seconds: the time the route waits (plan ruling P23). The run's own execution timeout is
// that time too (P26), so the activity does not start later than a moment after the route
// answered (the timeout counts from the moment Temporal records the start): see
// TemporalStarter.
type Run struct {
	Activity         string          `json:"activity"`
	Queue            string          `json:"queue"`
	Input            json.RawMessage `json:"input"`
	StartToCloseS    int64           `json:"start_to_close_s"`
	ScheduleToStartS int64           `json:"schedule_to_start_s"`
}

// activityOptions are the options of the run's one activity: one attempt (R11).
func activityOptions(in Run) workflow.ActivityOptions {
	return workflow.ActivityOptions{
		TaskQueue:              in.Queue,
		StartToCloseTimeout:    time.Duration(in.StartToCloseS) * time.Second,
		ScheduleToStartTimeout: time.Duration(in.ScheduleToStartS) * time.Second,
		RetryPolicy:            &temporal.RetryPolicy{MaximumAttempts: 1},
	}
}

// runOf is w's workflow input: its schedule-to-start is the route's wait, w's timeout plus
// WaitMargin.
func runOf(w Write, queue string) Run {
	return Run{Activity: w.Activity, Queue: queue, StartToCloseS: int64(w.Timeout / time.Second),
		ScheduleToStartS: int64((w.Timeout + WaitMargin) / time.Second)}
}

// Workflow runs in.Activity once (no retry, R11) on in.Queue and gives its result as it
// came. The activity's failure is the workflow's.
func Workflow(ctx workflow.Context, in Run) (json.RawMessage, error) {
	ctx = workflow.WithActivityOptions(ctx, activityOptions(in))
	var out json.RawMessage
	err := workflow.ExecuteActivity(ctx, in.Activity, in.Input).Get(ctx, &out)
	return out, err
}

// TemporalStarter is the Starter over a Temporal client: it starts Workflow on Queue, whose
// activity runs on LibraryQueue.
//
// The start's id conflict policy is USE_EXISTING, so a send while a run of the id is open
// joins that run (R12). Its reuse policy is ALLOW_DUPLICATE: a send after the run closed
// starts a new run. Each caller's activity makes a second run safe in its own way (the
// presets' idempotency ledger; the unknown tray's seen_at check and tray lock).
//
// The run's execution timeout is the route's wait (plan ruling P26). The schedule-to-start
// timeout of P23 starts only when the workflow task schedules the activity, and that needs
// core's own worker. When no core worker takes the workflow task in time, the run closes as
// the route answers 503 (its timer starts when Temporal records the start, a moment after
// the route's), and Temporal gives a closed run's activity task to no worker. So an activity
// does not start after that moment. An activity that started in time may still end after
// the run closed: its write stands.
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
		WorkflowExecutionTimeout: w.Timeout + WaitMargin,
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
// TimeoutFailure. Nil for any other error (the wait's own end, a Temporal out of reach).
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
		return &Failure{Type: TimeoutFailure, Message: timeout.Error()}
	}
	return nil
}
