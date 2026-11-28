package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/log"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/activities"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/store"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// fakeWorker is a worker.Worker whose Start fails until the test lets it succeed. As the
// SDK's, a failed Start is final for that worker: it fails again on every call.
type fakeWorker struct {
	worker.Worker
	ok              bool
	starts, stopped atomic.Int32
	failed          bool
}

func (w *fakeWorker) Start() error {
	w.starts.Add(1)
	if w.failed || !w.ok {
		w.failed = true
		return errors.New("unreachable")
	}
	return nil
}

func (w *fakeWorker) Stop() { w.stopped.Add(1) }

func setWorkerRetry(t *testing.T, d time.Duration) {
	t.Helper()
	before := workerRetry
	workerRetry = d
	t.Cleanup(func() { workerRetry = before })
}

func TestAWorkerThatCouldNotStartIsBuiltAgain(t *testing.T) {
	setWorkerRetry(t, time.Millisecond)
	var mu sync.Mutex
	var built []*fakeWorker
	reachable := atomic.Bool{}
	health := func(context.Context) error {
		if !reachable.Load() {
			return errors.New("unreachable")
		}
		return nil
	}
	build := func() worker.Worker {
		mu.Lock()
		defer mu.Unlock()
		w := &fakeWorker{ok: reachable.Load()}
		built = append(built, w)
		return w
	}
	stop := startWorker(context.Background(), "test", health, build)
	time.Sleep(50 * time.Millisecond)
	mu.Lock()
	if len(built) != 0 {
		t.Fatalf("a worker was built while Temporal was out of reach: %d", len(built))
	}
	mu.Unlock()
	reachable.Store(true)
	deadline := time.Now().Add(5 * time.Second)
	for {
		mu.Lock()
		n := len(built)
		mu.Unlock()
		if n > 0 && built[n-1].starts.Load() > 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("no worker started once Temporal was reachable")
		}
		time.Sleep(5 * time.Millisecond)
	}
	stop(context.Background())
	mu.Lock()
	defer mu.Unlock()
	last := built[len(built)-1]
	if last.failed || last.stopped.Load() != 1 {
		t.Fatalf("the started worker: failed %v, stopped %d", last.failed, last.stopped.Load())
	}
}

func TestAFailedStartBuildsANewWorker(t *testing.T) {
	setWorkerRetry(t, time.Millisecond)
	var count atomic.Int32
	var first *fakeWorker
	build := func() worker.Worker {
		w := &fakeWorker{ok: count.Add(1) > 1}
		if first == nil {
			first = w
		}
		return w
	}
	done := make(chan struct{})
	var stop func(context.Context)
	go func() {
		stop = startWorker(context.Background(), "test", func(context.Context) error { return nil }, build)
		close(done)
	}()
	<-done
	deadline := time.Now().Add(5 * time.Second)
	for count.Load() < 2 {
		if time.Now().After(deadline) {
			t.Fatal("no second worker was built")
		}
		time.Sleep(5 * time.Millisecond)
	}
	stop(context.Background())
	if first.starts.Load() != 1 {
		t.Fatalf("the failed worker was started %d times", first.starts.Load())
	}
	if first.stopped.Load() != 1 {
		t.Fatalf("the failed worker was stopped %d times, want 1", first.stopped.Load())
	}
}

func TestAStopAfterTheDrainStillStopsAStartedWorker(t *testing.T) {
	setWorkerRetry(t, time.Millisecond)
	w := &fakeWorker{ok: true}
	stop := startWorker(context.Background(), "test", func(context.Context) error { return nil }, func() worker.Worker { return w })
	for w.starts.Load() == 0 {
		time.Sleep(time.Millisecond)
	}
	// The worker is in the channel once Start has returned; give the send its moment.
	time.Sleep(20 * time.Millisecond)
	drain, cancel := context.WithCancel(context.Background())
	cancel()
	stop(drain)
	for deadline := time.Now().Add(5 * time.Second); w.stopped.Load() == 0; {
		if time.Now().After(deadline) {
			t.Fatal("a worker that had started was not stopped")
		}
		time.Sleep(time.Millisecond)
	}
}

func TestAWorkerThatStartsAfterCtxEndsIsStopped(t *testing.T) {
	setWorkerRetry(t, time.Millisecond)
	ctx, cancel := context.WithCancel(context.Background())
	w := &fakeWorker{ok: true}
	stop := startWorker(ctx, "test", func(context.Context) error { return nil }, func() worker.Worker {
		cancel()
		return w
	})
	stop(context.Background())
	if w.starts.Load() != 1 || w.stopped.Load() != 1 {
		t.Fatalf("starts %d, stopped %d, want 1 and 1", w.starts.Load(), w.stopped.Load())
	}
}

// slowWorker's Stop blocks until released, as the SDK's waits for running activities.
type slowWorker struct {
	fakeWorker
	release chan struct{}
}

func (w *slowWorker) Stop() { <-w.release }

func TestTheWorkersStopEndsAtTheDrainDeadline(t *testing.T) {
	setWorkerRetry(t, time.Millisecond)
	w := &slowWorker{fakeWorker: fakeWorker{ok: true}, release: make(chan struct{})}
	defer close(w.release)
	stop := startWorker(context.Background(), "test", func(context.Context) error { return nil }, func() worker.Worker { return w })
	for w.starts.Load() == 0 {
		time.Sleep(time.Millisecond)
	}
	drain, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	begun := time.Now()
	stop(drain)
	if waited := time.Since(begun); waited > 2*time.Second {
		t.Fatalf("stop waited %v past the drain deadline", waited)
	}
}

// TestTheWorkerRunsOnceTemporalComesUp starts core with Temporal out of reach, then starts
// a Temporal dev server (the temporal CLI; the test skips without it, but fails under
// GitHub Actions, whose go job installs it, #8256) on that address: the inventory worker
// must then run an activity. A workflow on a queue of the test's own runs
// inventory.KindCreate on the queue inventory, as a workflow step does (Task 16).
func TestTheWorkerRunsOnceTemporalComesUp(t *testing.T) {
	cli, err := exec.LookPath("temporal")
	if err != nil {
		if os.Getenv("GITHUB_ACTIONS") == "true" {
			t.Fatal("the temporal CLI is not on PATH; go-module-ci.yml installs it")
		}
		t.Skip("the temporal CLI is not on PATH")
	}
	setWorkerRetry(t, 100*time.Millisecond)
	pool := testdb.New(t)
	temporalAddress := freeAddress(t)
	env := map[string]string{
		"SCADBUDDY_CORE_UPSTREAM":    "http://127.0.0.1:1",
		"SCADBUDDY_DATABASE_URL":     pool.Config().ConnString(),
		"SCADBUDDY_CORE_LISTEN":      freeAddress(t),
		"SCADBUDDY_CORE_AREAS":       "inventory",
		"SCADBUDDY_TEMPORAL_ADDRESS": temporalAddress,
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- run(ctx, func(k string) (string, bool) { v, ok := env[k]; return v, ok }, nil) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(30 * time.Second):
			t.Error("run did not stop")
		}
	})
	// Let the worker fail against no Temporal at least once.
	time.Sleep(500 * time.Millisecond)

	_, port, _ := net.SplitHostPort(temporalAddress)
	server := exec.Command(cli, "server", "start-dev", "--headless", "--ip", "127.0.0.1", "--port", port,
		"--ui-port", strings.Split(freeAddress(t), ":")[1], "--namespace", "scadbuddy", "--log-level", "error")
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = server.Process.Kill()
		_ = server.Wait()
	})

	c, err := client.Dial(client.Options{HostPort: temporalAddress, Namespace: "scadbuddy",
		Logger: log.NewStructuredLogger(slog.New(slog.DiscardHandler))})
	for deadline := time.Now().Add(30 * time.Second); err != nil; {
		if time.Now().After(deadline) {
			t.Fatal(err)
		}
		time.Sleep(200 * time.Millisecond)
		c, err = client.Dial(client.Options{HostPort: temporalAddress, Namespace: "scadbuddy",
			Logger: log.NewStructuredLogger(slog.New(slog.DiscardHandler))})
	}
	defer c.Close()
	step := func(ctx workflow.Context, in activities.Input) (store.Answer, error) {
		ctx = workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
			ActivityID: in.Key, TaskQueue: "inventory", StartToCloseTimeout: 30 * time.Second,
		})
		var answer store.Answer
		err := workflow.ExecuteActivity(ctx, "inventory.KindCreate", in).Get(ctx, &answer)
		return answer, err
	}
	w := worker.New(c, "test", worker.Options{})
	w.RegisterWorkflowWithOptions(step, workflow.RegisterOptions{Name: "Step"})
	if err := w.Start(); err != nil {
		t.Fatal(err)
	}
	defer w.Stop()
	run, err := c.ExecuteWorkflow(context.Background(), client.StartWorkflowOptions{TaskQueue: "test"}, "Step",
		activities.Input{Key: "browser::late", Actor: []byte(`{"kind":"browser"}`),
			Body: []byte(`{"id":"late_kind","name":"Late","parent_id":"consumable","counting":"lot"}`)})
	if err != nil {
		t.Fatal(err)
	}
	wait, stop := context.WithTimeout(context.Background(), 60*time.Second)
	defer stop()
	var answer store.Answer
	if err := run.Get(wait, &answer); err != nil {
		t.Fatal(err)
	}
	if answer.Status != 201 {
		t.Fatalf("%d %s", answer.Status, answer.Body)
	}
}
