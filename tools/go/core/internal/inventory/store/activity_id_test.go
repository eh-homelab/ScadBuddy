package store

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// TestActivityIDIsPythons reads the ids of testdata/activity_ids.json, which backend
// tests/inventory/test_core_client.py test_the_activity_id_is_gos reads too, so Python's
// core_client.activity_id and ActivityID cannot drift.
func TestActivityIDIsPythons(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "activity_ids.json"))
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Cases []struct{ Kind, Key, ID string } `json:"cases"`
	}
	if err := json.Unmarshal(raw, &file); err != nil || len(file.Cases) == 0 {
		t.Fatal(len(file.Cases), err)
	}
	for _, c := range file.Cases {
		if got := ActivityID(c.Kind, c.Key); got != c.ID {
			t.Errorf("ActivityID(%q, %q) = %q, want %q", c.Kind, c.Key, got, c.ID)
		}
	}
}

// A stale running row of a standalone activity asks Temporal for the id Python started it
// under (R1), not for the operation id.
func TestAStaleStandaloneRowAsksForItsActivityID(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	op := Operation{ID: "browser:p1:k6", Kind: "inventory.ReceiveLot", Subject: "item:x", Attempt: 1, Actor: Actor(browser)}
	if err := StartOperation(ctx, pool, op); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `UPDATE inventory_operations SET updated_at = now() - interval '61 seconds'`); err != nil {
		t.Fatal(err)
	}
	temporal := &fakeTemporal{closed: false}
	got, _, err := ReadOperation(ctx, pool, op.ID, temporal)
	if err != nil || got.Status != "running" {
		t.Fatalf("%+v %v", got, err)
	}
	if temporal.asked != [2]string{"inventory.ReceiveLot:browser:p1:k6", ""} {
		t.Fatalf("asked %v", temporal.asked)
	}
}

// A key reused on another write starts a second activity with the same operation id: its
// start leaves the first, still running, row as it is (R2).
func TestAStartOfAnotherKindLeavesARunningRow(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	first := Operation{ID: "browser:p1:k7", Kind: "inventory.ReceiveLot", Subject: "item:a", Attempt: 1, Actor: Actor(browser)}
	if err := StartOperation(ctx, pool, first); err != nil {
		t.Fatal(err)
	}
	other := Operation{ID: first.ID, Kind: "inventory.Take", Subject: "item:b", WorkflowID: "wf", Attempt: 3, Actor: Actor(browser)}
	if err := StartOperation(ctx, pool, other); err != nil {
		t.Fatal(err)
	}
	var kind, subject string
	var workflowID *string
	var attempt int
	if err := pool.QueryRow(ctx, `SELECT kind, subject, workflow_id, attempt FROM inventory_operations WHERE id = $1`, first.ID).
		Scan(&kind, &subject, &workflowID, &attempt); err != nil {
		t.Fatal(err)
	}
	if kind != "inventory.ReceiveLot" || subject != "item:a" || workflowID != nil || attempt != 1 {
		t.Fatal(kind, subject, workflowID, attempt)
	}
}
