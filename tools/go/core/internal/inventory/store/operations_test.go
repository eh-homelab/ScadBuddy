package store

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

// fakeTemporal answers a Describe as the test says, and counts the calls.
type fakeTemporal struct {
	closed    bool
	completed bool
	detail    string
	err       error
	calls     int
	asked     [2]string
}

func (f *fakeTemporal) Closed(_ context.Context, id, workflowID string) (Closure, error) {
	f.calls++
	f.asked = [2]string{id, workflowID}
	c := Closure{State: Open}
	switch {
	case f.completed:
		c.State = Completed
	case f.closed:
		c.State = Failed
	}
	c.Detail, c.At = f.detail, time.Date(2026, 11, 26, 12, 0, 0, 0, time.UTC)
	return c, f.err
}

func TestAnOperationStartsRunsAndSucceedsWithItsWrite(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	op := Operation{ID: "browser::k1", Kind: "inventory.Test", Subject: "item:x", Attempt: 1, Actor: Actor(browser)}
	if err := StartOperation(ctx, pool, op); err != nil {
		t.Fatal(err)
	}
	got, found, err := ReadOperation(ctx, pool, op.ID, nil)
	if err != nil || !found {
		t.Fatal(found, err)
	}
	if got.Status != "running" || got.Kind != "inventory.Test" || got.Subject != "item:x" || got.Result != nil || got.FinishedAt != nil {
		t.Fatalf("%+v", got)
	}

	runs := 0
	if _, err := Write(WithOperation(ctx, op.ID), pool, req(op.ID, `{}`), insert(ctx, &runs)); err != nil {
		t.Fatal(err)
	}
	got, _, err = ReadOperation(ctx, pool, op.ID, nil)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != "succeeded" || got.FinishedAt == nil || got.Error != nil {
		t.Fatalf("%+v", got)
	}
	var result map[string]any
	if err := json.Unmarshal(got.Result, &result); err != nil || result["n"] != 1.0 || result["word"] != "made" {
		t.Fatalf("result %s", got.Result)
	}
}

func TestARefusedWriteFailsItsOperation(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	op := Operation{ID: "k2", Kind: "inventory.Test", Attempt: 1, Actor: Actor(browser)}
	if err := StartOperation(ctx, pool, op); err != nil {
		t.Fatal(err)
	}
	_, err := Write(WithOperation(ctx, op.ID), pool, req(op.ID, `{}`), func(pgx.Tx) (int, any, error) {
		return 0, nil, &Refused{Status: 409, Code: "lot_empty", Detail: "no stock", Fields: map[string]any{"lot": "l1"}}
	})
	if err != nil {
		t.Fatal(err)
	}
	got, _, err := ReadOperation(ctx, pool, op.ID, nil)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != "failed" || got.Error == nil || got.Result != nil {
		t.Fatalf("%+v", got)
	}
	e := got.Error
	if e.Type != "https://scadbuddy.dev/problems/inventory/lot_empty" || e.Status != 409 || e.Title != "Conflict" ||
		e.Detail != "no stock" || e.Extensions["code"] != "lot_empty" || e.Extensions["lot"] != "l1" {
		t.Fatalf("%+v", e)
	}
	if _, ok := e.Extensions["type"]; ok {
		t.Fatalf("the type is in the extensions: %+v", e)
	}
}

func TestAnUnknownOperationIsNotFound(t *testing.T) {
	pool := setup(t)
	if _, found, err := ReadOperation(context.Background(), pool, "nope", nil); found || err != nil {
		t.Fatal(found, err)
	}
}

func TestNoteErrorKeepsTheOperationRunning(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	op := Operation{ID: "k3", Kind: "inventory.Test", Attempt: 1, Actor: Actor(browser)}
	if err := StartOperation(ctx, pool, op); err != nil {
		t.Fatal(err)
	}
	if err := NoteError(ctx, pool, op.ID, 1, errors.New("connection refused")); err != nil {
		t.Fatal(err)
	}
	var status, lastError string
	var attempt int
	if err := pool.QueryRow(ctx, `SELECT status, attempt, last_error FROM inventory_operations WHERE id = $1`, op.ID).
		Scan(&status, &attempt, &lastError); err != nil {
		t.Fatal(err)
	}
	if status != "running" || attempt != 1 || lastError != "connection refused" {
		t.Fatal(status, attempt, lastError)
	}
	op.Attempt = 2
	if err := StartOperation(ctx, pool, op); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT status, attempt FROM inventory_operations WHERE id = $1`, op.ID).
		Scan(&status, &attempt); err != nil {
		t.Fatal(err)
	}
	if status != "running" || attempt != 2 {
		t.Fatal(status, attempt)
	}
}

func TestAStaleRunningRowReadsTemporal(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	op := Operation{ID: "k4", Kind: "inventory.Take", WorkflowID: "order_item:oi1:parts:1", Attempt: 1, Actor: Actor(browser)}
	if err := StartOperation(ctx, pool, op); err != nil {
		t.Fatal(err)
	}

	// A fresh running row is answered as it is: Temporal is not asked.
	temporal := &fakeTemporal{closed: true, detail: "activity timed out"}
	got, _, err := ReadOperation(ctx, pool, op.ID, temporal)
	if err != nil || got.Status != "running" || temporal.calls != 0 {
		t.Fatalf("%+v %v %d", got, err, temporal.calls)
	}

	if _, err := pool.Exec(ctx, `UPDATE inventory_operations SET updated_at = now() - interval '61 seconds'`); err != nil {
		t.Fatal(err)
	}
	got, _, err = ReadOperation(ctx, pool, op.ID, temporal)
	if err != nil {
		t.Fatal(err)
	}
	if temporal.asked != [2]string{"k4", "order_item:oi1:parts:1"} {
		t.Fatalf("asked %v", temporal.asked)
	}
	if got.Status != "failed" || got.Error == nil || got.Error.Extensions["code"] != "inventory_activity_failed" ||
		got.Error.Detail != "activity timed out" || got.FinishedAt == nil {
		t.Fatalf("%+v %+v", got, got.Error)
	}
	// The read writes nothing.
	if n := count(t, pool, `SELECT count(*) FROM inventory_operations WHERE status = 'running'`); n != 1 {
		t.Fatalf("%d running", n)
	}

	// An activity still open, or a Temporal out of reach, leaves the row running.
	for _, f := range []*fakeTemporal{{closed: false}, {err: errors.New("unavailable")}} {
		got, _, err = ReadOperation(ctx, pool, op.ID, f)
		if err != nil || got.Status != "running" || got.Error != nil {
			t.Fatalf("%+v %v", got, err)
		}
	}
}

func TestTheOperationReadsAsPythonsOperation(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	op := Operation{ID: "k5", Kind: "inventory.Test", Subject: "lot:l", Attempt: 1, Actor: Actor(browser)}
	if err := StartOperation(ctx, pool, op); err != nil {
		t.Fatal(err)
	}
	got, _, err := ReadOperation(ctx, pool, op.ID, nil)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]any
	if err := json.Unmarshal(raw, &fields); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"id", "kind", "subject", "status", "result", "error", "created_at", "finished_at"} {
		if _, ok := fields[k]; !ok {
			t.Fatalf("no %s in %s", k, raw)
		}
	}
	if len(fields) != 8 {
		t.Fatalf("%s", raw)
	}
}

func TestTheShopCurrencyIsTheSettingOrTheFallback(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	for _, c := range []struct{ row, want string }{
		{"", "USD"},
		{`null`, "USD"},
		{`"EUR"`, "EUR"},
	} {
		if _, err := pool.Exec(ctx, `DELETE FROM settings WHERE name = 'orders_currency'`); err != nil {
			t.Fatal(err)
		}
		if c.row != "" {
			if _, err := pool.Exec(ctx, `INSERT INTO settings (name, value) VALUES ('orders_currency', $1::jsonb)`, c.row); err != nil {
				t.Fatal(err)
			}
		}
		got, err := ShopCurrency(ctx, pool, "USD")
		if err != nil || got != c.want {
			t.Fatalf("%s: %q %v", c.row, got, err)
		}
	}
}

func TestACompletedActivityWhoseRowRunsReadsItsLedger(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	stale := func(id string) {
		t.Helper()
		if err := StartOperation(ctx, pool, Operation{ID: id, Kind: "inventory.Test", Attempt: 1, Actor: Actor(browser)}); err != nil {
			t.Fatal(err)
		}
		if _, err := pool.Exec(ctx, `UPDATE inventory_operations SET updated_at = now() - interval '2 minutes' WHERE id = $1`, id); err != nil {
			t.Fatal(err)
		}
	}
	completed := &fakeTemporal{completed: true}
	// The write committed with no record of its end (a record that failed to start).
	runs := 0
	stale("done")
	if _, err := Write(ctx, pool, req("done", `{}`), insert(ctx, &runs)); err != nil {
		t.Fatal(err)
	}
	got, _, err := ReadOperation(ctx, pool, "done", completed)
	if err != nil || got.Status != "succeeded" || got.Result == nil || got.Error != nil {
		t.Fatalf("%+v %v", got, err)
	}
	// A refusal in the ledger.
	stale("refused")
	if _, err := Write(ctx, pool, req("refused", `{}`), func(pgx.Tx) (int, any, error) {
		return 0, nil, &Refused{Status: 409, Code: "lot_empty", Detail: "no stock"}
	}); err != nil {
		t.Fatal(err)
	}
	got, _, err = ReadOperation(ctx, pool, "refused", completed)
	if err != nil || got.Status != "failed" || got.Error == nil || got.Error.Extensions["code"] != "lot_empty" || got.Result != nil {
		t.Fatalf("%+v %v", got, err)
	}
	// Nothing in the ledger.
	stale("lost")
	got, _, err = ReadOperation(ctx, pool, "lost", completed)
	if err != nil || got.Status != "failed" || got.Error.Extensions["code"] != "inventory_activity_failed" {
		t.Fatalf("%+v %v", got, err)
	}
}
