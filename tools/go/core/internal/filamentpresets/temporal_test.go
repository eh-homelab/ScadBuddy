package filamentpresets

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/converter"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"
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
