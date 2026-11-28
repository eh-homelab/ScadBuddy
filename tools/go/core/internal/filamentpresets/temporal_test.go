package filamentpresets

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/converter"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

func writeEnv(t *testing.T, fn any) (*testsuite.TestWorkflowEnvironment, *[]string) {
	t.Helper()
	env := (&testsuite.WorkflowTestSuite{}).NewTestWorkflowEnvironment()
	env.RegisterWorkflowWithOptions(WriteWorkflow, workflow.RegisterOptions{Name: WorkflowName})
	env.RegisterActivityWithOptions(fn, activity.RegisterOptions{Name: "filament_preset_copy"})
	queues := &[]string{}
	env.SetOnActivityStartedListener(func(info *activity.Info, _ context.Context, _ converter.EncodedValues) {
		*queues = append(*queues, info.TaskQueue)
	})
	return env, queues
}

func TestTheWorkflowRunsTheActivityOnLibraryAndGivesItsResult(t *testing.T) {
	var got json.RawMessage
	env, queues := writeEnv(t, func(_ context.Context, in json.RawMessage) (json.RawMessage, error) {
		got = in
		return json.RawMessage(`{"preset_id":8,"version":1,"changed":true,"mirror":"off"}`), nil
	})
	env.ExecuteWorkflow(WorkflowName, WriteRun{Activity: "filament_preset_copy", Queue: "library",
		Input: json.RawMessage(`{"preset_id":7,"name":"Mine"}`), StartToCloseS: 60})
	if err := env.GetWorkflowError(); err != nil {
		t.Fatal(err)
	}
	var out json.RawMessage
	if err := env.GetWorkflowResult(&out); err != nil || string(out) != `{"preset_id":8,"version":1,"changed":true,"mirror":"off"}` {
		t.Fatalf("%s %v", out, err)
	}
	if string(got) != `{"preset_id":7,"name":"Mine"}` {
		t.Errorf("the activity got %s", got)
	}
	if len(*queues) != 1 || (*queues)[0] != "library" {
		t.Errorf("queues %v", *queues)
	}
}

func TestTheWorkflowTriesTheActivityOnceAndKeepsItsType(t *testing.T) {
	tries := 0
	env, _ := writeEnv(t, func(context.Context, json.RawMessage) (json.RawMessage, error) {
		tries++
		return nil, temporal.NewApplicationError("at version 4", "version_moved", 4)
	})
	env.ExecuteWorkflow(WorkflowName, WriteRun{Activity: "filament_preset_copy", Queue: "library",
		Input: json.RawMessage(`{}`), StartToCloseS: 60})
	err := env.GetWorkflowError()
	failure := failureOf(err)
	if failure == nil || failure.Type != "version_moved" || string(failure.Detail) != "4" || failure.Message != "at version 4" {
		t.Fatalf("%v %+v", err, failure)
	}
	if tries != 1 {
		t.Errorf("%d tries", tries)
	}
	// A retryable error of the activity is tried once too.
	tries = 0
	env, _ = writeEnv(t, func(context.Context, json.RawMessage) (json.RawMessage, error) {
		tries++
		return nil, errors.New("a bug")
	})
	env.ExecuteWorkflow(WorkflowName, WriteRun{Activity: "filament_preset_copy", Queue: "library",
		Input: json.RawMessage(`{}`), StartToCloseS: 60})
	if env.GetWorkflowError() == nil || tries != 1 {
		t.Errorf("%d tries", tries)
	}
}

func TestATimedOutActivityIsATimeout(t *testing.T) {
	env, _ := writeEnv(t, func(ctx context.Context, _ json.RawMessage) (json.RawMessage, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	})
	env.SetTestTimeout(time.Minute)
	env.ExecuteWorkflow(WorkflowName, WriteRun{Activity: "filament_preset_copy", Queue: "library",
		Input: json.RawMessage(`{}`), StartToCloseS: 1})
	failure := failureOf(env.GetWorkflowError())
	if failure == nil || failure.Type != timeoutFailure {
		t.Fatalf("%+v", failure)
	}
}

func TestTheActivityNeverStartsLateOrAfterTheRouteAnswered(t *testing.T) {
	run := runOf(Write{Activity: "filament_preset_copy", Timeout: 60 * time.Second}, "library")
	if run.StartToCloseS != 60 || run.ScheduleToStartS != 70 {
		t.Errorf("%+v", run)
	}
	options := activityOptions(run)
	if options.TaskQueue != "library" || options.StartToCloseTimeout != 60*time.Second ||
		options.ScheduleToStartTimeout != 70*time.Second || options.RetryPolicy.MaximumAttempts != 1 {
		t.Errorf("%+v", options)
	}
}

// fakeClient is a Temporal client whose ExecuteWorkflow keeps what it got and answers run
// and err. The embedded nil Client panics on any other call.
type fakeClient struct {
	client.Client
	options client.StartWorkflowOptions
	name    any
	args    []any
	run     client.WorkflowRun
	err     error
}

func (c *fakeClient) ExecuteWorkflow(_ context.Context, options client.StartWorkflowOptions, workflow any, args ...any) (client.WorkflowRun, error) {
	c.options, c.name, c.args = options, workflow, args
	return c.run, c.err
}

// fakeRun is a started run whose Get answers out or err.
type fakeRun struct {
	client.WorkflowRun
	out json.RawMessage
	err error
}

func (r fakeRun) Get(_ context.Context, valuePtr any) error {
	if r.err != nil {
		return r.err
	}
	*valuePtr.(*json.RawMessage) = r.out
	return nil
}

func TestRunStartsOrJoinsTheRunAndClosesItWhenTheRouteStopsWaiting(t *testing.T) {
	c := &fakeClient{run: fakeRun{out: json.RawMessage(`{"preset_id":8}`)}}
	s := TemporalStarter{Client: c, Queue: "filament-presets", LibraryQueue: "library"}
	out, err := s.Run(context.Background(), Write{WorkflowID: "filament-preset-copy-x", Activity: "filament_preset_copy",
		Input: jsonv.Object{{Key: "preset_id", Value: int64(7)}}, Timeout: 60 * time.Second})
	if err != nil || string(out) != `{"preset_id":8}` {
		t.Fatalf("%s %v", out, err)
	}
	o := c.options
	if o.ID != "filament-preset-copy-x" || o.TaskQueue != "filament-presets" ||
		o.WorkflowIDConflictPolicy != enumspb.WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING ||
		o.WorkflowIDReusePolicy != enumspb.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE {
		t.Errorf("%+v", o)
	}
	// The run closes when the route stops waiting (P26), so a workflow task that no core
	// worker took by then never schedules the activity late.
	if o.WorkflowExecutionTimeout != 70*time.Second {
		t.Errorf("execution timeout %s", o.WorkflowExecutionTimeout)
	}
	if c.name != WorkflowName || len(c.args) != 1 {
		t.Fatalf("%v %v", c.name, c.args)
	}
	in := c.args[0].(WriteRun)
	if in.Activity != "filament_preset_copy" || in.Queue != "library" || string(in.Input) != `{"preset_id":7}` ||
		in.StartToCloseS != 60 || in.ScheduleToStartS != 70 {
		t.Errorf("%+v", in)
	}
}

func TestRunGivesTheActivityFailureOfAClosedRun(t *testing.T) {
	// The error of a closed run, as a client gives it: a WorkflowExecutionError around the
	// ActivityError around the activity's ApplicationError.
	env, _ := writeEnv(t, func(context.Context, json.RawMessage) (json.RawMessage, error) {
		return nil, temporal.NewApplicationError("at version 4", "version_moved", 4)
	})
	env.ExecuteWorkflow(WorkflowName, WriteRun{Activity: "filament_preset_copy", Queue: "library",
		Input: json.RawMessage(`{}`), StartToCloseS: 60})
	closed := env.GetWorkflowError()
	var execution *temporal.WorkflowExecutionError
	var activityErr *temporal.ActivityError
	if !errors.As(closed, &execution) || !errors.As(closed, &activityErr) {
		t.Fatalf("not a client's error: %T %v", closed, closed)
	}
	s := TemporalStarter{Client: &fakeClient{run: fakeRun{err: closed}}, Queue: "filament-presets", LibraryQueue: "library"}
	_, err := s.Run(context.Background(), Write{WorkflowID: "w", Activity: "filament_preset_copy", Input: jsonv.Object{}, Timeout: time.Minute})
	var f *Failure
	if !errors.As(err, &f) || f.Type != "version_moved" || string(f.Detail) != "4" || f.Message != "at version 4" {
		t.Fatalf("%v", err)
	}
}

func TestRunGivesAnyOtherErrorAsItCame(t *testing.T) {
	refused := errors.New("temporal is out of reach")
	s := TemporalStarter{Client: &fakeClient{err: refused}, Queue: "filament-presets", LibraryQueue: "library"}
	if _, err := s.Run(context.Background(), Write{WorkflowID: "w", Input: jsonv.Object{}, Timeout: time.Minute}); err != refused {
		t.Errorf("start: %v", err)
	}
	s.Client = &fakeClient{run: fakeRun{err: context.DeadlineExceeded}}
	_, err := s.Run(context.Background(), Write{WorkflowID: "w", Input: jsonv.Object{}, Timeout: time.Minute})
	var f *Failure
	if err != context.DeadlineExceeded || errors.As(err, &f) {
		t.Errorf("wait: %v", err)
	}
}
