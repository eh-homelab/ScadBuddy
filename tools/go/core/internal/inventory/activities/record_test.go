package activities

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/interceptor"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/store"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// recorded is the activities over a fresh database with the record's interceptor, and a
// connection that listens on the bus.
func recorded(t *testing.T) (*fixture, *pgxpool.Conn) {
	t.Helper()
	pool := testdb.New(t)
	env := (&testsuite.WorkflowTestSuite{}).NewTestActivityEnvironment()
	env.SetWorkerOptions(worker.Options{Interceptors: []interceptor.WorkerInterceptor{Record(pool, nil)}})
	Register(env, New(pool, "USD"))
	listener, err := pool.Acquire(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(listener.Release)
	if _, err := listener.Exec(context.Background(), "LISTEN scadbuddy_events"); err != nil {
		t.Fatal(err)
	}
	return &fixture{t: t, pool: pool, env: env, answers: map[string]json.RawMessage{}}, listener
}

// notices are the operation ids of the notices sent within a short wait.
func notices(t *testing.T, listener *pgxpool.Conn) []string {
	t.Helper()
	var out []string
	for {
		wait, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
		n, err := listener.Conn().WaitForNotification(wait)
		cancel()
		if err != nil {
			return out
		}
		var event struct {
			Kind        string `json:"kind"`
			OperationID string `json:"operation_id"`
		}
		if err := json.Unmarshal([]byte(n.Payload), &event); err != nil {
			t.Fatal(err)
		}
		if event.Kind == "inventory_operation.changed" {
			out = append(out, event.OperationID)
		}
	}
}

type row struct {
	status, kind, subject string
	attempt               int
	workflowID, lastError *string
	problem               map[string]any
	finished              bool
}

func operation(t *testing.T, pool *pgxpool.Pool, id string) row {
	t.Helper()
	var r row
	var problem []byte
	err := pool.QueryRow(context.Background(), `SELECT status, kind, subject, attempt, workflow_id, last_error,
		problem, finished_at IS NOT NULL FROM inventory_operations WHERE id = $1`, id).
		Scan(&r.status, &r.kind, &r.subject, &r.attempt, &r.workflowID, &r.lastError, &problem, &r.finished)
	if err != nil {
		t.Fatal(err)
	}
	if problem != nil {
		if err := json.Unmarshal(problem, &r.problem); err != nil {
			t.Fatal(err)
		}
	}
	return r
}

func TestTheRecordWritesStartAndEnd(t *testing.T) {
	f, listener := recorded(t)

	key := newKey()
	answer, err := f.tryKey("inventory.ItemCreate", key, nil, map[string]any{"kind_id": "hardware", "name": "M3 bolt"})
	if err != nil || answer.Status != 201 {
		t.Fatal(answer.Status, err)
	}
	if got := notices(t, listener); len(got) != 2 || got[0] != key || got[1] != key {
		t.Fatalf("notices %v", got)
	}
	r := operation(t, f.pool, key)
	// The SDK's test environment runs an activity inside a test workflow, so workflow_id
	// is that one's here; a standalone start has none (activity.Info.WorkflowExecution).
	if r.status != "succeeded" || r.kind != "inventory.ItemCreate" || r.subject != "item_kind:hardware" || !r.finished ||
		r.attempt != 1 {
		t.Fatalf("%+v", r)
	}
	var made struct{ ID string }
	if err := json.Unmarshal(answer.Body, &made); err != nil {
		t.Fatal(err)
	}

	// A refusal ends the record failed, with its problem.
	refusedKey := newKey()
	if answer, err := f.tryKey("inventory.ReceiveUnits", refusedKey, p("item_id", made.ID), map[string]any{"count": 1}); err != nil || answer.Status != 409 {
		t.Fatal(answer.Status, err)
	}
	r = operation(t, f.pool, refusedKey)
	if r.status != "failed" || r.problem["code"] != "wrong_counting" || r.subject != "item:"+made.ID {
		t.Fatalf("%+v", r)
	}
	notices(t, listener)

	// The end and the stock write commit together: a commit that fails leaves the record
	// running, and the stock unwritten.
	if _, err := f.pool.Exec(context.Background(), `
		CREATE FUNCTION refuse_the_end() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN IF NEW.status <> 'running' THEN RAISE EXCEPTION 'no end'; END IF; RETURN NULL; END $$;
		CREATE CONSTRAINT TRIGGER refuse_the_end AFTER UPDATE ON inventory_operations
		DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION refuse_the_end()`); err != nil {
		t.Fatal(err)
	}
	rolledBack := newKey()
	if _, err := f.tryKey("inventory.ReceiveLot", rolledBack, p("item_id", made.ID), map[string]any{"qty": "5"}); err == nil {
		t.Fatal("a failed commit answered")
	}
	r = operation(t, f.pool, rolledBack)
	if r.status != "running" || r.finished || r.lastError == nil {
		t.Fatalf("%+v", r)
	}
	if lots := f.read("/items/"+made.ID+"/lots", 200)["lots"].([]any); len(lots) != 0 {
		t.Fatalf("a lot was written: %v", lots)
	}
}

func TestAFailedAttemptStaysRunning(t *testing.T) {
	pool := testdb.New(t)
	key := newKey()
	var second row
	flaky := func(ctx context.Context, in Input) (store.Answer, error) {
		if activity.GetInfo(ctx).Attempt == 1 {
			return store.Answer{}, errors.New("the database is out of reach")
		}
		second = operation(t, pool, in.Key)
		return store.Write(ctx, pool, store.WriteReq{Caller: store.Caller, Key: in.Key, Route: "inventory.Flaky",
			Input: []byte(`{}`), Actor: store.Actor(in.Actor)}, func(pgx.Tx) (int, any, error) {
			return 200, map[string]any{}, nil
		})
	}
	run := func(ctx workflow.Context, in Input) (store.Answer, error) {
		ctx = workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
			ActivityID:          in.Key,
			StartToCloseTimeout: time.Minute,
			RetryPolicy:         &temporal.RetryPolicy{InitialInterval: time.Millisecond, MaximumAttempts: 3},
		})
		var answer store.Answer
		err := workflow.ExecuteActivity(ctx, "inventory.Flaky", in).Get(ctx, &answer)
		return answer, err
	}
	env := (&testsuite.WorkflowTestSuite{}).NewTestWorkflowEnvironment()
	env.SetWorkerOptions(worker.Options{Interceptors: []interceptor.WorkerInterceptor{Record(pool, nil)}})
	env.RegisterActivityWithOptions(flaky, activity.RegisterOptions{Name: "inventory.Flaky"})
	env.RegisterWorkflowWithOptions(run, workflow.RegisterOptions{Name: "Flaky"})
	env.ExecuteWorkflow("Flaky", Input{Key: key, Actor: json.RawMessage(browser), Params: p("item_id", "i1")})
	if err := env.GetWorkflowError(); err != nil {
		t.Fatal(err)
	}

	if second.status != "running" || second.attempt != 2 || second.lastError == nil || *second.lastError != "the database is out of reach" {
		t.Fatalf("before the second attempt's write: %+v", second)
	}
	r := operation(t, pool, key)
	if r.status != "succeeded" || r.attempt != 2 || r.subject != "item:i1" || r.workflowID == nil || *r.workflowID == "" {
		t.Fatalf("%+v", r)
	}
}

func TestAFailedRecordNeverFailsTheActivity(t *testing.T) {
	f, _ := recorded(t)
	// A key past the record's id bound: the record cannot start, the activity still runs.
	long := newKey()
	for len(long) <= 300 {
		long += "x"
	}
	answer, err := f.tryKey("inventory.ItemCreate", long, nil, map[string]any{"kind_id": "hardware", "name": "M3 bolt"})
	if err != nil || answer.Status != 201 {
		t.Fatal(answer.Status, err)
	}
	var n int
	if err := f.pool.QueryRow(context.Background(), `SELECT count(*) FROM inventory_operations`).Scan(&n); err != nil || n != 0 {
		t.Fatal(n, err)
	}
}

func TestAReusedKeyLeavesTheFirstRecordAsItIs(t *testing.T) {
	f, listener := recorded(t)
	key := newKey()
	if answer, err := f.tryKey("inventory.ItemCreate", key, nil, map[string]any{"kind_id": "hardware", "name": "M3 bolt"}); err != nil || answer.Status != 201 {
		t.Fatal(answer.Status, err)
	}
	notices(t, listener)
	before := operation(t, f.pool, key)

	// The same key with another activity and input: the ledger's 422, and the first record
	// keeps its kind, its status and its result.
	other, err := f.tryKey("inventory.KindCreate", key, nil, map[string]any{"id": "other", "name": "O", "parent_id": "consumable"})
	if err != nil || other.Status != 422 {
		t.Fatal(other.Status, err)
	}
	// A plain replay (same activity, same input) leaves it as it is too.
	if again, err := f.tryKey("inventory.ItemCreate", key, nil, map[string]any{"kind_id": "hardware", "name": "M3 bolt"}); err != nil || again.Status != 201 {
		t.Fatal(again.Status, err)
	}
	if got := notices(t, listener); len(got) != 0 {
		t.Fatalf("notices %v", got)
	}
	after := operation(t, f.pool, key)
	if after.status != "succeeded" || after.kind != "inventory.ItemCreate" || after.subject != before.subject || after.problem != nil {
		t.Fatalf("%+v", after)
	}
	op, _, err := store.ReadOperation(context.Background(), f.pool, key, nil)
	if err != nil || op.Result == nil {
		t.Fatalf("%+v %v", op, err)
	}
}
