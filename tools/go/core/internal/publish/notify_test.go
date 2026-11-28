package publish

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

// fixedID and fixedAt make the golden file the same on every run.
var (
	fixedID = "0123456789abcdef0123456789abcdef"
	fixedAt = time.Date(2026, 11, 26, 12, 0, 0, 0, time.UTC)
)

// golden is the file Python's test decodes with core/events.py decode_event (Task 13).
const golden = "testdata/inventory_operation_changed.json"

func TestTheNoticeIsAnEventPythonReads(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	listener, err := pool.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Release()
	if _, err := listener.Exec(ctx, "LISTEN "+EventsChannel); err != nil {
		t.Fatal(err)
	}

	err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		return Notify(ctx, tx, "inventory_operation.changed", map[string]any{"operation_id": "browser:é:k"})
	})
	if err != nil {
		t.Fatal(err)
	}

	wait, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	n, err := listener.Conn().WaitForNotification(wait)
	if err != nil {
		t.Fatal(err)
	}
	// Python's json.dumps with ensure_ascii: no byte past ASCII.
	for _, c := range []byte(n.Payload) {
		if c >= 0x80 {
			t.Fatalf("a byte past ASCII: %s", n.Payload)
		}
	}
	var got map[string]any
	if err := json.Unmarshal([]byte(n.Payload), &got); err != nil {
		t.Fatal(err)
	}
	var keys []string
	for k := range got {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	if !slices.Equal(keys, []string{"at", "id", "kind", "operation_id"}) {
		t.Fatalf("keys %v", keys)
	}
	if got["kind"] != "inventory_operation.changed" || got["operation_id"] != "browser:é:k" {
		t.Fatalf("%s", n.Payload)
	}
	if id, _ := got["id"].(string); len(id) != 32 {
		t.Fatalf("id %q", got["id"])
	}
	if _, err := time.Parse(time.RFC3339Nano, got["at"].(string)); err != nil {
		t.Fatal(err)
	}

	// The golden file: the same encoder with a fixed id and time.
	fixed, err := payload(fixedID, fixedAt, "inventory_operation.changed", map[string]any{"operation_id": "browser:op:k"})
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.FromSlash(golden)
	want, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		if err := os.WriteFile(path, []byte(fixed+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	if err != nil {
		t.Fatal(err)
	}
	if string(want) != fixed+"\n" {
		t.Fatalf("%s is\n%s\nwant\n%s", golden, want, fixed)
	}
}

func TestANoticeRollsBackWithItsTransaction(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	listener, err := pool.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Release()
	if _, err := listener.Exec(ctx, "LISTEN "+EventsChannel); err != nil {
		t.Fatal(err)
	}
	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := Notify(ctx, tx, "inventory_operation.changed", map[string]any{"operation_id": "x"}); err != nil {
		t.Fatal(err)
	}
	if err := tx.Rollback(ctx); err != nil {
		t.Fatal(err)
	}
	wait, cancel := context.WithTimeout(ctx, 500*time.Millisecond)
	defer cancel()
	if n, err := listener.Conn().WaitForNotification(wait); err == nil {
		t.Fatalf("a notice was sent: %s", n.Payload)
	}
}
